"""`/upload` — upload big files and share them in the server.

Discord's native upload limit is 25 MB (free) / 50 MB (boosted).  This
command lets members upload larger files by hosting them on external
services:

* **Videos** → Streamable (Discord auto-plays the link inline) when
  credentials are configured, otherwise catbox
* **Everything else** → catbox.moe (permanent direct link, no size drama)
* **Very large files** → file.kiwi (the member finishes the upload in the
  browser; nothing has to pass through the bot)

Every upload is posted in the upload channel with a **thread** for
discussion, and the channel keeps a pinned message explaining the system.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import discord
from discord import app_commands
from discord.ext import commands

from utils import catbox, filehost, file_kiwi, streamable

#: The channel where uploads are posted and the info message is pinned.
UPLOAD_CHANNEL_ID: int = 1548959110467289169

#: Max file size for Discord native uploads (free tier).
MAX_NATIVE_SIZE: int = 25 * 1024 * 1024  # 25 MB

#: Discord's practical attachment ceiling — the biggest file a member can
#: hand to the bot in the first place.
ABSOLUTE_MAX_SIZE: int = 100 * 1024 * 1024  # 100 MB

#: Video MIME types that should go to Streamable.
VIDEO_MIMES: frozenset[str] = frozenset({
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-matroska",
    "video/avi",
    "video/ogg",
    "video/mpeg",
})

#: Video file extensions (fallback when MIME type is missing).
VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".ogv", ".mpeg", ".mpg",
})


def _is_video(filename: str, content_type: str | None) -> bool:
    """Heuristic: should this file go to Streamable?"""
    if content_type and content_type.split(";")[0].strip() in VIDEO_MIMES:
        return True
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in VIDEO_EXTENSIONS)


def _suffix(filename: str) -> str:
    """``.mp4`` for ``clip.mp4``, empty string when there is no extension."""
    return "." + filename.rsplit(".", 1)[-1] if "." in filename else ""


#: Discord rejects an embed whose title is longer than this, with an HTTP 400.
EMBED_TITLE_MAX: int = 256

#: Discord rejects message content longer than this.
CONTENT_MAX: int = 2000


def _label(prefix: str, text: str) -> str:
    """A name that is always short enough for Discord to accept.

    A member-supplied ``title`` (or a long attachment filename) is not bounded
    by anything Discord enforces on the option, so an over-long one used to
    make ``channel.send`` raise and the link the member had just uploaded was
    never delivered. Truncating is the difference between a slightly shortened
    title and no message at all.
    """
    room = EMBED_TITLE_MAX - len(prefix)
    text = " ".join(text.split()) or "upload"
    if len(text) > room:
        text = text[: max(room - 1, 1)].rstrip() + "…"
    return prefix + text


def _reason(exc: object) -> str:
    """One line of failure text, bounded so the error card always fits."""
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text[:300]


def build_failure_text(errors: list[str]) -> str:
    """The "nothing accepted the file" card, always inside Discord's limit.

    A failure here means the member's upload was wasted, so the explanation
    has to arrive.  An unbounded service error would push the message over
    2,000 characters and the follow-up itself would be rejected, leaving the
    member with no answer at all.
    """
    detail = "\n".join(f"• {e}" for e in errors) or "• every service refused the file"
    return (
        f"❌ **Upload failed** — no host accepted the file.\n\n{detail}\n\n"
        "Try again in a minute, or use `/upload-big` for a file.kiwi "
        "link you can upload through yourself."
    )[:CONTENT_MAX]


def _service_order(service: str, is_video: bool) -> list[str]:
    """Which hosts to try, in order, for the requested service."""
    if service == "streamable":
        return ["streamable", "catbox", "filekiwi"]
    if service == "catbox":
        return ["catbox", "filekiwi"]
    if service == "filekiwi":
        return ["filekiwi"]
    # auto: real uploads first, Streamable for video when it is configured.
    order = ["streamable"] if is_video and streamable.available() else []
    return order + ["catbox", "filekiwi"]


def build_info_embed() -> discord.Embed:
    """The pinned "how to upload" embed (shared with the startup sticky post)."""
    embed = discord.Embed(
        title="📎 How to Upload Big Files",
        description=(
            "Discord's native upload limit is **25 MB** (free) / **50 MB** "
            "(boosted). This bot uploads bigger files to a real host and posts "
            "the link here.\n\n"
            "**How to use**\n"
            "1. Run `/upload` and attach your file\n"
            "2. Optionally add a title\n"
            "3. The bot posts the link here and opens a thread for it\n\n"
            "**What the services do**\n"
            "• **catbox** — the file is uploaded for real, link is permanent "
            "(up to 200 MB). Images show inline, videos get a player.\n"
            "• **Streamable** — videos play inline in Discord.\n"
            "• **file.kiwi** — no size limit, but you finish the upload in your "
            "browser (nothing is sent until you drag the file in). Links last "
            "24 hours and the upload is E2E encrypted.\n\n"
            "**Bigger than 100 MB?**\n"
            "• Use `/upload-big` — it hands you a **file.kiwi** link\n"
            "• Open the link and drag the file straight from your device\n"
            "• Nothing is proxied through the bot, so size is no problem\n\n"
            "**Tips**\n"
            "• Add a title so people know what the file is\n"
            "• Each upload gets its own thread — use it for questions\n"
            "• Only upload things you have the right to share"
        ),
        color=0x5865F2,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="🐾 Meow Bot • File Upload System")
    return embed


class Uploads(commands.Cog):
    """Big-file uploads via catbox, Streamable, and file.kiwi."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="upload",
        description="Upload a big file — videos to Streamable, everything else to catbox.",
    )
    @app_commands.describe(
        file="The file to upload (max 100 MB here; use /upload-big for anything larger)",
        title="A short title for the upload (optional)",
        service="Preferred upload service (auto if not specified)",
    )
    @app_commands.choices(service=[
        app_commands.Choice(name="Auto (best option)", value="auto"),
        app_commands.Choice(name="catbox (permanent link)", value="catbox"),
        app_commands.Choice(name="Streamable (video)", value="streamable"),
        app_commands.Choice(name="file.kiwi (browser upload)", value="filekiwi"),
    ])
    async def upload(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        title: str = "",
        service: str = "auto",
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This command only works in a server.", ephemeral=True,
            )

        if file.size > ABSOLUTE_MAX_SIZE:
            return await interaction.response.send_message(
                f"❌ That file is **{filehost.format_size(file.size)}** — the "
                f"most this command can take is "
                f"**{ABSOLUTE_MAX_SIZE // (1024 * 1024)} MB**.\n"
                f"💡 Use `/upload-big` — it gives you a file.kiwi link that has "
                f"no size limit.",
                ephemeral=True,
            )

        channel = self.bot.get_channel(UPLOAD_CHANNEL_ID)
        if channel is None:
            return await interaction.response.send_message(
                "❌ I can't reach the upload channel right now. Please tell an "
                "admin.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        # --- save the attachment to a temp file (streams, never in memory) ---
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=_suffix(file.filename)) as tmp:
                tmp_path = tmp.name
            await file.save(tmp_path)
            actual_size = os.path.getsize(tmp_path)
        except Exception as exc:
            if tmp_path:
                _unlink(tmp_path)
            return await interaction.followup.send(
                f"❌ I couldn't download **{file.filename}** from Discord: {exc}\n"
                "Please try again in a moment.",
                ephemeral=True,
            )

        is_video = _is_video(file.filename, file.content_type)
        errors: list[str] = []
        try:
            link, used_service, manual = await asyncio.to_thread(
                _resolve_link, tmp_path, file.filename, is_video, service, errors,
            )
        finally:
            _unlink(tmp_path)

        if link is None:
            return await interaction.followup.send(
                build_failure_text(errors), ephemeral=True,
            )

        # --- post in the upload channel ---
        embed = discord.Embed(
            title=_label("📎 ", title or file.filename),
            description=(
                f"Uploaded by {interaction.user.mention}\n"
                f"**Service:** {used_service}\n"
                f"**Size:** {filehost.format_size(actual_size)}\n"
                f"**Type:** {file.content_type or 'unknown'}"
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="🔗 Upload link" if manual else "🔗 Download / Watch",
            value=f"[Click here]({link})",
            inline=False,
        )
        if manual:
            embed.add_field(
                name="⚠️ Finish the upload",
                value=(
                    "Open the link and drag your file in — nothing has been "
                    "sent yet. The link expires in 24 hours."
                ),
                inline=False,
            )
        embed.set_footer(text=f"Uploaded by {interaction.user.display_name}")

        try:
            msg = await channel.send(embed=embed)
        except discord.HTTPException as exc:
            # The file is already uploaded.  Whatever Discord objected to, the
            # member must still get the link rather than a silent failure.
            print(f"[UPLOAD] Channel post rejected: {exc}", flush=True)
            return await interaction.followup.send(
                f"✅ The file is uploaded, but I couldn't post it in "
                f"{channel.mention} — here's your link:\n{link}",
                ephemeral=True,
            )

        # --- create a thread for this upload ---
        try:
            thread = await msg.create_thread(
                name=_label("📎 ", title or file.filename)[:100],
                auto_archive_duration=1440,  # 24 hours
                reason=f"Upload by {interaction.user.display_name}",
            )
            await thread.send(
                f"{interaction.user.mention} uploaded **{file.filename}** "
                f"({filehost.format_size(actual_size)}) via {used_service}.\n"
                f"🔗 {link}\n\n"
                "Ask your questions about this file right here."
            )
        except discord.Forbidden:
            pass  # thread creation is nice-to-have
        except discord.HTTPException as exc:
            print(f"[UPLOAD] Thread creation failed: {exc}", flush=True)

        confirm = (
            f"✅ Uploaded **{file.filename}** ({filehost.format_size(actual_size)})\n"
            f"🔗 {link}\n"
            f"📺 Posted in {channel.mention} with a thread."
        )
        if manual:
            confirm += (
                "\n\n⚠️ **Open the link and drag your file in** — the upload "
                "isn't finished until you do."
            )
        await interaction.followup.send(confirm[:CONTENT_MAX], ephemeral=True)

    @app_commands.command(
        name="upload-big",
        description="Get a file.kiwi link for uploading very large files (100MB+).",
    )
    @app_commands.describe(
        title="A short title for the upload (optional)",
    )
    async def upload_big(
        self,
        interaction: discord.Interaction,
        title: str = "",
    ):
        """Create a file.kiwi folder link the member uploads into themselves."""
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This command only works in a server.", ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        folder_title = " ".join((title or f"Upload by {interaction.user.display_name}").split())
        try:
            result = await asyncio.to_thread(
                file_kiwi.create_folder, folder_title[:120], "send",
            )
        except Exception as exc:
            return await interaction.followup.send(
                f"❌ **file.kiwi failed:** {_reason(exc)}\n\n"
                "That is usually a network hiccup. Try again in a moment, or "
                "use `/upload` for files up to 100 MB — those are uploaded for "
                "you straight away.",
                ephemeral=True,
            )

        if result is None:
            return await interaction.followup.send(
                "❌ **file.kiwi** didn't return a link.\n\n"
                "The service may be down or rate-limiting. Try again shortly, "
                "or use `/upload` for files up to 100 MB — those are uploaded "
                "for you straight away.",
                ephemeral=True,
            )

        channel = self.bot.get_channel(UPLOAD_CHANNEL_ID)
        if channel is None:
            return await interaction.followup.send(
                f"✅ Link ready:\n{result.folder_url}\n\n"
                "Open it in your browser and drag your file in (24 hours max).",
                ephemeral=True,
            )

        embed = discord.Embed(
            title=_label("📁 ", folder_title),
            description=(
                f"Requested by {interaction.user.mention}\n"
                f"**Service:** file.kiwi\n\n"
                "## ⚠️ The upload isn't finished yet\n"
                "Open the link and drag your file in. Nothing is sent until you "
                "do."
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="🔗 Upload link",
            value=f"[Click here to upload]({result.folder_url})",
            inline=False,
        )
        embed.add_field(
            name="Details",
            value="🔐 E2E encrypted • ⏰ Expires 24 hours after the upload",
            inline=False,
        )
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")

        try:
            msg = await channel.send(embed=embed)
        except discord.HTTPException as exc:
            print(f"[UPLOAD] Channel post rejected: {exc}", flush=True)
            return await interaction.followup.send(
                f"✅ Link ready:\n{result.folder_url}\n\n"
                "I couldn't post it in the upload channel, so keep this link.",
                ephemeral=True,
            )

        try:
            thread = await msg.create_thread(
                name=_label("📁 ", folder_title)[:100],
                auto_archive_duration=1440,
                reason=f"Upload by {interaction.user.display_name}",
            )
            await thread.send(
                f"{interaction.user.mention} is uploading via **file.kiwi**.\n"
                f"🔗 {result.folder_url}\n\n"
                "Drop the link to the finished file here once it's up."
            )
        except discord.Forbidden:
            pass
        except discord.HTTPException as exc:
            print(f"[UPLOAD] Thread creation failed: {exc}", flush=True)

        await interaction.followup.send(
            (
                f"✅ **file.kiwi** link ready!\n"
                f"🔗 {result.folder_url}\n\n"
                f"⚠️ Open it in your browser and **drag your file in** — the upload "
                f"isn't finished until you do.\n"
                f"📺 Posted in {channel.mention} with a thread."
            )[:CONTENT_MAX],
            ephemeral=True,
        )

    @app_commands.command(
        name="pin-upload-info",
        description="Pin the upload instructions message in the upload channel. (Admin)",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def pin_upload_info(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This command only works in a server.", ephemeral=True,
            )

        channel = self.bot.get_channel(UPLOAD_CHANNEL_ID)
        if channel is None:
            return await interaction.response.send_message(
                "❌ The upload channel is not accessible.", ephemeral=True,
            )

        try:
            msg = await channel.send(embed=build_info_embed())
            await msg.pin()
            await interaction.response.send_message(
                "✅ Pinned the upload instructions!", ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I cannot send or pin messages in the upload channel.",
                ephemeral=True,
            )


def _unlink(path: str) -> None:
    """Best-effort temp-file cleanup."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _resolve_link(
    path: str,
    filename: str,
    is_video: bool,
    service: str,
    errors: list[str],
) -> tuple[str | None, str, bool]:
    """Try each host in order and return ``(link, service_label, manual)``.

    ``manual`` is True when the link is an empty file.kiwi folder the member
    still has to upload into.  Blocking — call it from a worker thread.
    """
    for candidate in _service_order(service, is_video):
        if candidate == "streamable":
            if not streamable.available():
                continue
            try:
                result = streamable.upload(path, filename)
            except Exception as exc:  # noqa: BLE001 - report and fall through
                errors.append(f"Streamable: {_reason(exc)}")
                continue
            if result:
                return result.url, "Streamable", False
            errors.append("Streamable: the upload was rejected")
        elif candidate == "catbox":
            try:
                result = catbox.upload(path, filename)
            except Exception as exc:  # noqa: BLE001 - report and fall through
                errors.append(f"catbox: {_reason(exc)}")
                continue
            if result:
                return result.link, "catbox", False
            errors.append("catbox: the upload was rejected")
        elif candidate == "filekiwi":
            try:
                result = file_kiwi.create_folder(filename, "send")
            except Exception as exc:  # noqa: BLE001 - report and fall through
                errors.append(f"file.kiwi: {_reason(exc)}")
                continue
            if result:
                return result.folder_url, "file.kiwi", True
            errors.append("file.kiwi: no link returned")

    return None, "", False


async def setup(bot: commands.Bot):
    await bot.add_cog(Uploads(bot))
