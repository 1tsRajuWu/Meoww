"""`/upload` — upload big files and share them in the server.

Discord's own attachment limit depends on the server's boost level and is
usually too small for a video.  This command lets members upload larger
files by hosting them on external services:

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

#: The channel the bot has always posted uploads in. Resolution order is:
#: the per-guild ``upload_channel_id`` setting, then ``UPLOAD_CHANNEL_ID`` from
#: the environment, then this. It is deliberately *not* "unset": defaulting to
#: nothing turned a working command into "No upload channel is configured" for
#: every server that had not been configured yet, and the sticky post in
#: ``bot.py`` reads this same constant, so the two cannot disagree.
DEFAULT_UPLOAD_CHANNEL_ID: int = 1548959110467289169

def resolve_default_channel_id(environ=None) -> int:
    """The channel to post uploads in, honouring ``UPLOAD_CHANNEL_ID``.

    A value that is not a channel id is a typo, not a reason to crash the bot
    at import time — it falls back to the default and says so.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get("UPLOAD_CHANNEL_ID", "") or "").strip()
    if not raw:
        return DEFAULT_UPLOAD_CHANNEL_ID
    try:
        return int(raw)
    except ValueError:
        print(
            f"[UPLOAD] UPLOAD_CHANNEL_ID={raw!r} is not a channel id — "
            f"falling back to {DEFAULT_UPLOAD_CHANNEL_ID}",
            flush=True,
        )
        return DEFAULT_UPLOAD_CHANNEL_ID


#: Where /upload posts. Override with the ``UPLOAD_CHANNEL_ID`` env var.
UPLOAD_CHANNEL_ID: int = resolve_default_channel_id()

#: Uploads hold a worker thread for up to five minutes each. They share the
#: default executor with the database backup loop, so a burst of them used to
#: be able to queue every backup behind it — the "my progress did not save"
#: symptom, arriving through a different door. Only this many run at once.
MAX_CONCURRENT_UPLOADS: int = 4
_UPLOAD_SLOTS = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)


async def _in_upload_thread(func, *args):
    """Run a blocking upload off the event loop, on a bounded number of threads."""
    async with _UPLOAD_SLOTS:
        return await asyncio.to_thread(func, *args)

#: Max file size for Discord native uploads (free tier).
MAX_NATIVE_SIZE: int = 25 * 1024 * 1024  # 25 MB

#: The biggest file ``/upload`` will carry itself. This is not a Discord limit:
#: it is what the host the bytes go to will take. catbox needs no credentials
#: and answers range requests, so Discord plays a video it hosts inline — and it
#: stops at 200 MB per file. The old flat 100 MB refused files catbox would have
#: accepted happily: a 123 MB clip was turned away with a pointer at
#: ``/upload-big``, so the member had to drag it into an empty file.kiwi folder
#: by hand and then watch file.kiwi try and fail to preview it.
MAX_HOSTED_SIZE: int = catbox.MAX_SIZE

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
#: Measured in UTF-16 units, so an emoji costs two.
EMBED_TITLE_MAX: int = 256

#: Discord rejects message content longer than this, in the same units.
CONTENT_MAX: int = 2000

#: Discord rejects a thread name longer than this, in the same units.
THREAD_NAME_MAX: int = 100


def _units(text: str) -> int:
    """How Discord counts ``text``: astral characters cost two, not one.

    ``len()`` says a 256-character title is fine, but Discord counts UTF-16
    units, so one emoji makes it 257 and the embed is refused. Measuring the
    same way the API does is what keeps a clamp from sitting exactly on the
    boundary it is supposed to stay under.
    """
    return len(text.encode("utf-16-le")) // 2


def _fit(text: str, limit: int) -> str:
    """``text`` trimmed to ``limit`` UTF-16 units, with an ellipsis if cut."""
    if _units(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        size = _units(char)
        if used + size > limit - 1:  # leave one unit for the ellipsis
            break
        kept.append(char)
        used += size
    return "".join(kept).rstrip() + "…"


def _label(prefix: str, text: str) -> str:
    """A name that is always short enough for Discord to accept.

    A member-supplied ``title`` (or a long attachment filename) is not bounded
    by anything Discord enforces on the option, so an over-long one used to
    make ``channel.send`` raise and the link the member had just uploaded was
    never delivered. Truncating is the difference between a slightly shortened
    title and no message at all.
    """
    return _fit(prefix + (" ".join(text.split()) or "upload"), EMBED_TITLE_MAX)


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
    return _fit(
        f"❌ **Upload failed** — no host accepted the file.\n\n{detail}\n\n"
        "Try again in a minute, or use `/upload-big` for a file.kiwi "
        "link you can upload through yourself.",
        CONTENT_MAX,
    )


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
            "Discord's own attachment limit depends on the server's boost "
            "level, and it is usually too small for a video. Attach whatever "
            "you can and this bot uploads it to a real host and posts the link "
            "here.\n\n"
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
            f"**Bigger than {MAX_HOSTED_SIZE // (1024 * 1024)} MB?**\n"
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

    async def _upload_channel(self, guild: discord.Guild):
        """Resolve the upload channel for this guild.

        Tries the guild setting, then the environment/default, and returns the
        first that resolves to a channel in *this* guild. A stale setting — a
        channel that was deleted or belongs to another server — falls through
        to the next candidate instead of leaving uploads with nowhere to go.
        """
        from utils import db
        saved = await db.get_setting(guild.id, "upload_channel_id")
        for candidate in (saved, UPLOAD_CHANNEL_ID):
            try:
                cid = int(candidate or 0)
            except (TypeError, ValueError):
                cid = 0
            if not cid:
                continue
            ch = self.bot.get_channel(cid)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(cid)
                except Exception:
                    ch = None
            if ch is None:
                continue
            # A channel in another guild is not an upload channel for this one
            if getattr(ch, "guild", None) is not None and ch.guild.id != guild.id:
                continue
            return ch
        return None

    @app_commands.command(
        name="upload",
        description="Upload a big file — videos to Streamable, everything else to catbox.",
    )
    @app_commands.describe(
        file=(
            f"The file to upload (up to {MAX_HOSTED_SIZE // (1024 * 1024)} MB here; "
            "use /upload-big for anything larger)"
        ),
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

        if file.size > MAX_HOSTED_SIZE:
            return await interaction.response.send_message(
                f"❌ That file is **{filehost.format_size(file.size)}**. I can "
                f"upload up to **{MAX_HOSTED_SIZE // (1024 * 1024)} MB** myself "
                "— that is the limit of the free host the bytes go to, not a "
                "Discord limit.\n"
                f"💡 Use `/upload-big` — it gives you a file.kiwi link that has "
                f"no size limit, which you finish in your browser.",
                ephemeral=True,
            )

        channel = await self._upload_channel(interaction.guild)
        if channel is None:
            return await interaction.response.send_message(
                "❌ No upload channel is configured for this server. Set "
                "`UPLOAD_CHANNEL_ID` in the environment, or ask an admin to run "
                "`/updatedb` after creating an `#uploads` channel.",
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
            link, used_service, manual = await _in_upload_thread(
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
                f"✅ Uploaded, but I can't post in {channel.mention} — here's "
                f"your link:\n{link}",
                ephemeral=True,
            )

        # --- create a thread for this upload ---
        try:
            thread = await msg.create_thread(
                name=_fit(_label("📎 ", title or file.filename), THREAD_NAME_MAX),
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
        await interaction.followup.send(_fit(confirm, CONTENT_MAX), ephemeral=True)

    @app_commands.command(
        name="upload-big",
        description=(
            "Get a file.kiwi link for uploading very large files "
            f"({MAX_HOSTED_SIZE // (1024 * 1024)} MB or more)."
        ),
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
            result = await _in_upload_thread(
                file_kiwi.create_folder, folder_title[:120], "send",
            )
        except Exception as exc:
            return await interaction.followup.send(
                f"❌ **file.kiwi failed:** {_reason(exc)}\n\n"
                "That is usually a network hiccup. Try again in a moment, or "
                f"use `/upload` for files up to "
                f"{MAX_HOSTED_SIZE // (1024 * 1024)} MB — those are uploaded "
                "for you straight away.",
                ephemeral=True,
            )

        if result is None:
            return await interaction.followup.send(
                "❌ **file.kiwi** didn't return a link.\n\n"
                "The service may be down or rate-limiting. Try again shortly, "
                f"or use `/upload` for files up to "
                f"{MAX_HOSTED_SIZE // (1024 * 1024)} MB — those are uploaded "
                "for you straight away.",
                ephemeral=True,
            )

        channel = await self._upload_channel(interaction.guild)
        if channel is None:
            return await interaction.followup.send(
                f"✅ Link ready:\n{result.folder_url}\n\n"
                "Open it in your browser and drag your file in (24 hours max).\n\n"
                "⚠️ No upload channel is configured, so nothing was posted in-server. "
                "Set `UPLOAD_CHANNEL_ID` or run `/updatedb` with an `#uploads` channel.",
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
                "I can't post it in the upload channel, so keep this link.",
                ephemeral=True,
            )

        try:
            thread = await msg.create_thread(
                name=_fit(_label("📁 ", folder_title), THREAD_NAME_MAX),
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
            _fit(
                f"✅ **file.kiwi** link ready!\n"
                f"🔗 {result.folder_url}\n\n"
                f"⚠️ Open it in your browser and **drag your file in** — the upload "
                f"isn't finished until you do.\n"
                f"📺 Posted in {channel.mention} with a thread.",
                CONTENT_MAX,
            ),
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

        channel = await self._upload_channel(interaction.guild)
        if channel is None:
            return await interaction.response.send_message(
                "❌ No upload channel is configured for this server. Set "
                "`UPLOAD_CHANNEL_ID` or run `/updatedb` after creating an "
                "`#uploads` channel.",
                ephemeral=True,
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
