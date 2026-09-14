"""`/upload` — upload big files and share them in the server.

Discord's native upload limit is 25 MB (free) / 50 MB (boosted).  This
command lets members upload larger files by hosting them on external
services:

* **Videos** → Streamable (Discord auto-plays the link inline)
* **Other files** → file.io (one-click download link)

Each upload creates a **thread** in the upload channel so discussion
about the file stays organised.

The upload channel has a **pinned message** explaining the system, kept
at the bottom by the bot.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import discord
from discord import app_commands
from discord.ext import commands

from utils import filehost, streamable, file_kiwi

#: The channel where uploads are posted and the info message is pinned.
UPLOAD_CHANNEL_ID: int = 1548959110467289169


#: Max file size for Discord native uploads (free tier).
MAX_NATIVE_SIZE: int = 25 * 1024 * 1024  # 25 MB

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

#: Discord's absolute file size limit (nitro).
ABSOLUTE_MAX_SIZE: int = 100 * 1024 * 1024  # 100 MB

#: Threshold where we suggest file.kiwi instead (100 MB).
FILE_KIWI_THRESHOLD: int = 100 * 1024 * 1024  # 100 MB


def _is_video(filename: str, content_type: str | None) -> bool:
    """Heuristic: should this file go to Streamable?"""
    if content_type and content_type.split(";")[0].strip() in VIDEO_MIMES:
        return True
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in VIDEO_EXTENSIONS)


class Uploads(commands.Cog):
    """Big-file uploads via Streamable, file.io, and file.kiwi."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="upload",
        description="Upload a big file — videos to Streamable, other files to file.io or file.kiwi.",
    )
    @app_commands.describe(
        file="The file to upload (max 100 MB direct, or use /upload-big for larger)",
        title="A short title for the upload (optional)",
        service="Preferred upload service (auto if not specified)",
    )
    @app_commands.choices(service=[
        app_commands.Choice(name="Auto (best option)", value="auto"),
        app_commands.Choice(name="Streamable (video)", value="streamable"),
        app_commands.Choice(name="file.io (other files)", value="fileio"),
        app_commands.Choice(name="file.kiwi (large files)", value="filekiwi"),
    ])
    async def upload(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        title: str = "",
        service: str = "auto",
    ):
        # --- pre-flight checks ---
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This command only works in a server.", ephemeral=True,
            )

        # Size check — for files over 100MB, suggest /upload-big
        if file.size > ABSOLUTE_MAX_SIZE:
            return await interaction.response.send_message(
                f"❌ That file is **{file.size / (1024*1024):.1f} MB** — the "
                f"absolute limit is **{ABSOLUTE_MAX_SIZE / (1024*1024):.0f} MB**.\n"
                f"💡 For files this large, use `/upload-big` to get a file.kiwi link!",
                ephemeral=True,
            )

        # Check the upload channel exists
        channel = self.bot.get_channel(UPLOAD_CHANNEL_ID)
        if channel is None:
            return await interaction.response.send_message(
                "❌ The upload channel is not accessible.", ephemeral=True,
            )

        # Defer — the upload might take a while
        await interaction.response.defer(ephemeral=True)

        # --- download the file ---
        try:
            data = await file.read()
        except Exception as exc:
            return await interaction.followup.send(
                f"❌ Failed to download the file: {exc}", ephemeral=True,
            )

        if len(data) > ABSOLUTE_MAX_SIZE:
            return await interaction.followup.send(
                f"❌ The file is **{len(data) / (1024*1024):.1f} MB** — "
                f"over the **{ABSOLUTE_MAX_SIZE / (1024*1024):.0f} MB** limit.",
                ephemeral=True,
            )

        # --- upload to the right service ---
        is_video = _is_video(file.filename, file.content_type)
        link: str | None = None
        used_service: str = ""

        # Auto mode: pick the best service
        if service == "auto":
            if is_video and streamable.available():
                service = "streamable"
            elif file.size > FILE_KIWI_THRESHOLD:
                service = "filekiwi"
            else:
                service = "fileio"

        # Streamable for videos
        if service == "streamable" and is_video and streamable.available():
            suffix = "." + file.filename.rsplit(".", 1)[-1] if "." in file.filename else ".mp4"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                result = await asyncio.to_thread(
                    streamable.upload, tmp_path, title or file.filename,
                )
                if result:
                    link = result.url
                    used_service = "Streamable"
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        # file.kiwi for large files (creates a folder link)
        if link is None and service == "filekiwi":
            folder_title = title or file.filename
            result = await asyncio.to_thread(
                file_kiwi.create_folder, folder_title, "send"
            )
            if result:
                link = result.folder_url
                used_service = "file.kiwi"

        # Fallback to file.io (non-video, or other services failed/unavailable)
        if link is None and service != "filekiwi":
            suffix = "." + file.filename.rsplit(".", 1)[-1] if "." in file.filename else ""
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                result = await asyncio.to_thread(filehost.upload, tmp_path)
                if result:
                    link = result.link
                    used_service = "file.io"
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        if link is None:
            # Build a helpful error message based on what we tried
            tried_services = []
            if is_video and streamable.available():
                tried_services.append("Streamable")
            if service != "filekiwi" and not is_video:
                tried_services.append("file.io")
            if service == "streamable" or (service == "auto" and is_video and streamable.available()):
                tried_services.append("Streamable")
            if service == "fileio" or (service == "auto" and not is_video and not (file.size > FILE_KIWI_THRESHOLD)):
                tried_services.append("file.io")

            error_msg = (
                "❌ **Upload failed.**\n\n"
            )
            if tried_services:
                error_msg += (
                    f"Tried: **{', '.join(tried_services)}** — all failed.\n"
                )
                if is_video and streamable.available():
                    error_msg += (
                        "• **Streamable**: Service may be down or rate-limiting.\n"
                        "• Try again later, or use `/upload-big` for file.kiwi.\n"
                    )
                else:
                    error_msg += (
                        "• The external service may be temporarily unavailable.\n"
                        "• Try again later, or use `/upload-big` for file.kiwi.\n"
                    )
            else:
                error_msg += (
                    "All upload services are unavailable.\n"
                    "• Try again later, or use `/upload-big` for file.kiwi.\n"
                )
            await interaction.followup.send(error_msg, ephemeral=True)
            return

        # --- post in the upload channel ---
        embed = discord.Embed(
            title=f"📎 {title or file.filename}",
            description=(
                f"Uploaded by {interaction.user.mention}\n"
                f"**Service:** {used_service}\n"
                f"**Size:** {filehost.format_size(len(data))}\n"
                f"**Type:** {file.content_type or 'unknown'}"
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="🔗 Download / Watch", value=f"[Click here]({link})", inline=False)
        if used_service == "file.kiwi":
            embed.add_field(
                name="🔐 file.kiwi",
                value="Open the link to upload. Files are E2E encrypted and auto-expire.",
                inline=False,
            )
        embed.set_footer(text=f"Uploaded by {interaction.user.display_name}")

        try:
            msg = await channel.send(
                embed=embed,
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ I cannot send messages in the upload channel.", ephemeral=True,
            )

        # --- create a thread ---
        thread_name = f"📎 {title or file.filename}"[:100]
        try:
            thread = await msg.create_thread(
                name=thread_name,
                auto_archive_duration=1440,  # 24 hours
                reason=f"Upload by {interaction.user.display_name}",
            )
            await thread.send(
                f"Discussion about this upload. {interaction.user.mention} "
                f"uploaded **{file.filename}** ({filehost.format_size(len(data))}) "
                f"via {service}."
            )
        except discord.Forbidden:
            pass  # thread creation is nice-to-have

        # --- confirm to the uploader ---
        confirm_msg = (
            f"✅ Uploaded **{file.filename}** ({filehost.format_size(len(data))})\n"
            f"🔗 {link}\n"
            f"📺 Posted in {channel.mention} with a thread."
        )
        if used_service == "file.kiwi":
            confirm_msg += "\n\n🔐 **file.kiwi**: Open the link to upload your file. It's E2E encrypted!"
        await interaction.followup.send(confirm_msg, ephemeral=True)

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
        """Create a file.kiwi folder link for uploading large files.
        
        The user opens the link in a browser and uploads directly.
        Files are E2E encrypted and expire after 24 hours.
        """
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This command only works in a server.", ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        folder_title = title or f"Upload by {interaction.user.display_name}"
        try:
            result = await asyncio.to_thread(
                file_kiwi.create_folder, folder_title, "send"
            )
        except Exception as exc:
            return await interaction.followup.send(
                f"❌ **file.kiwi failed:** {exc}\n\n"
                "This is usually a network or service issue. Try again in a moment,"
                " or use `/upload` for smaller files via Streamable or file.io.",
                ephemeral=True,
            )

        if result is None:
            return await interaction.followup.send(
                "❌ **file.kiwi** returned no link.\n\n"
                "The service may be down or rate-limiting. Try again later, "
                "or use `/upload` for smaller files via Streamable or file.io.",
                ephemeral=True,
            )

        # Post in the upload channel
        channel = self.bot.get_channel(UPLOAD_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title=f"📁 {folder_title}",
                description=(
                    f"Uploaded by {interaction.user.mention}\n"
                    f"**Service:** file.kiwi\n"
                    f"**Mode:** {result.mode}\n\n"
                    "🔐 **E2E encrypted** • Auto-expires in 24 hours"
                ),
                color=0x5865F2,
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(
                name="🔗 Upload Link",
                value=f"[Click here to upload]({result.folder_url})",
                inline=False,
            )
            embed.set_footer(text=f"Uploaded by {interaction.user.display_name}")

            try:
                msg = await channel.send(
                    embed=embed,
                )
                # Create thread
                thread_name = f"📁 {folder_title}"[:100]
                try:
                    thread = await msg.create_thread(
                        name=thread_name,
                        auto_archive_duration=1440,
                        reason=f"Upload by {interaction.user.display_name}",
                    )
                    await thread.send(
                        f"Discussion about this upload. {interaction.user.mention} "
                        f"is uploading via **file.kiwi**.\n"
                        f"🔗 {result.folder_url}"
                    )
                except discord.Forbidden:
                    pass
            except discord.Forbidden:
                pass

        await interaction.followup.send(
            f"✅ **file.kiwi** folder created!\n"
            f"🔗 {result.folder_url}\n\n"
            f"🔐 Open the link to upload your file. It's E2E encrypted!\n"
            f"⏰ Files auto-expire in 24 hours.",
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

        embed = discord.Embed(
            title="📎 How to Upload Big Files",
            description=(
                "Discord's native upload limit is **25 MB** (free) / **50 MB** (boosted).\n"
                "This bot lets you upload bigger files using external hosting:\n\n"
                "**🎬 Videos** → Uploaded to **Streamable** — Discord plays them inline!\n"
                "**📁 Other files** → Uploaded to **file.io** — one-click download link\n"
                "**📁 Large files** → Use **file.kiwi** — no size limit, E2E encrypted!\n\n"
                "**How to use:**\n"
                "1. Run `/upload` and attach your file\n"
                "2. Optionally add a title and choose a service\n"
                "3. The bot posts the link here and creates a thread for discussion\n\n"
                "**For very large files (100MB+):**\n"
                "• Use `/upload-big` to get a **file.kiwi** link\n"
                "• Open the link in your browser to upload\n"
                "• Files are E2E encrypted and auto-expire in 24 hours\n\n"
                "**Limits:**\n"
                "• Direct uploads: **100 MB** max\n"
                "• file.kiwi: **No file size limit** (up to 999 GiB!)\n"
                "• Supported video formats: MP4, WebM, MOV, AVI, MKV\n"
                "• All other file types are supported\n"
                f"• File.io links expire in **14 days** (download before then!)\n\n"
                "**Tips:**\n"
                "• Add a title so people know what the file is\n"
                "• Each upload gets its own thread — use it for questions\n"
                "• Videos on Streamable auto-play in Discord — no click needed!\n"
                "• file.kiwi is perfect for huge files — fully encrypted!"
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="🐾 Meow Bot • File Upload System")

        try:
            msg = await channel.send(embed=embed)
            await msg.pin()
            await interaction.response.send_message(
                "✅ Pinned the upload instructions!", ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I cannot send or pin messages in the upload channel.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Uploads(bot))
