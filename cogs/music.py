import os
import shutil
import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import aiohttp
import yt_dlp
import imageio_ffmpeg
import random
import datetime
from utils import db
from utils.style import send_card
from utils.spotify import is_spotify_url, resolve_spotify

# -----------------------------------------------------------------------------
# Audio & FFmpeg Configuration
# -----------------------------------------------------------------------------

system_ffmpeg = shutil.which("ffmpeg")
if system_ffmpeg:
    FFMPEG_EXE = system_ffmpeg
else:
    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

# Ensure binary has execute permissions
if FFMPEG_EXE and os.path.exists(FFMPEG_EXE):
    try:
        os.chmod(FFMPEG_EXE, 0o755)
    except Exception:
        pass

# Ensure Opus shared library is loaded if available on host
try:
    if not discord.opus.is_loaded():
        discord.opus._load_default()
except Exception as _e:
    pass

print(f"[MUSIC] Initialized. FFMPEG={FFMPEG_EXE}, OpusLoaded={discord.opus.is_loaded()}", flush=True)

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "scsearch1",
    "source_address": "0.0.0.0",
}

# -----------------------------------------------------------------------------
# 24/7 Curated Radio Stations (Zero-Lag, High-Uptime Direct Streams)
# -----------------------------------------------------------------------------

RADIO_STATIONS = {
    "lofi": {
        "name": "☕ 24/7 Lo-Fi Chill Beats",
        "url": "http://ice1.somafm.com/groovesalad-128-mp3",
        "desc": "Smooth, relaxing lofi and ambient downtempo beats. Perfect for studying and chilling in VC.",
        "icon": "☕",
        "color": 0xF39C12
    },
    "gaming": {
        "name": "🎮 Gaming EDM & Beats",
        "url": "https://streams.ilovemusic.de/iloveradio2.mp3",
        "desc": "High-octane electronic beats, bass, and gaming anthems for your gaming sessions.",
        "icon": "🎮",
        "color": 0x9B59B6
    },
    "synthwave": {
        "name": "🌃 Nightwave Plaza (Synthwave & Retro)",
        "url": "https://radio.plaza.one/mp3",
        "desc": "Aesthetic 80s neon synthwave, future funk, and vaporwave nostalgia.",
        "icon": "🌃",
        "color": 0xE74C3C
    },
    "anime": {
        "name": "🌸 Anime & J-Pop (Listen.moe)",
        "url": "https://listen.moe/stream",
        "desc": "Crystal-clear direct stream of anime OSTs, J-Pop, and vocaloid hits.",
        "icon": "🌸",
        "color": 0xE91E63
    },
    "jazz": {
        "name": "🎷 Smooth Coffeehouse Jazz",
        "url": "https://stream.zeno.fm/0r0xa792kwzuv",
        "desc": "Classic mellow cafe jazz, acoustic piano, and vintage saxophones.",
        "icon": "🎷",
        "color": 0xD35400
    },
    "pop": {
        "name": "📻 Top 40 Pop Hits",
        "url": "https://streams.ilovemusic.de/iloveradio1.mp3",
        "desc": "Today's top global chart-toppers, trending hits, and dance-pop.",
        "icon": "📻",
        "color": 0x3498DB
    }
}


def format_duration(seconds: int) -> str:
    """Formats seconds into mm:ss or hh:mm:ss."""
    if not seconds or seconds <= 0:
        return "Live Stream 🔴"
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


# -----------------------------------------------------------------------------
# Data Models
# -----------------------------------------------------------------------------

class Track:
    def __init__(
        self,
        title: str,
        source_url: str,
        duration: int = 0,
        requester: str = "Someone",
        thumbnail: str = None,
        webpage_url: str = None,
        is_radio: bool = False
    ):
        self.title = title
        self.source_url = source_url
        self.duration = duration
        self.requester = requester
        self.thumbnail = thumbnail
        self.webpage_url = webpage_url
        self.is_radio = is_radio


class GuildMusicPlayer:
    """Manages audio playback, queue, and voice client for a specific guild."""

    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.voice_client: discord.VoiceClient | None = None
        self.text_channel: discord.abc.MessageableChannel | None = None
        self.volume: float = 0.70
        self.bitrate_kbps: int = 128
        self.is_247: bool = False
        self.manual_stop: bool = False
        self.inactivity_task: asyncio.Task | None = None
        self.alone_task: asyncio.Task | None = None
        self._play_lock = asyncio.Lock()

    def cancel_inactivity_timer(self):
        if self.inactivity_task and not self.inactivity_task.done():
            self.inactivity_task.cancel()
        self.inactivity_task = None

    def start_inactivity_timer(self):
        if self.is_247:
            return
        self.cancel_inactivity_timer()
        self.inactivity_task = self.bot.loop.create_task(self._inactivity_timeout())

    async def _inactivity_timeout(self):
        """Disconnects the bot after 5 minutes (300 seconds) of silence/inactivity."""
        await asyncio.sleep(300)
        if self.is_247:
            return
        if self.voice_client and self.voice_client.is_connected() and not self.voice_client.is_playing() and not self.voice_client.is_paused():
            try:
                await self.voice_client.disconnect()
            except Exception:
                pass
            self.voice_client = None
            self.current = None
            self.queue.clear()
            if self.text_channel:
                try:
                    embed = discord.Embed(
                        title="👋 Auto-Disconnected",
                        description=(
                            "Left the voice channel after **5 minutes** of inactivity.\n\n"
                            "*👑 Server owner can enable `/247` to keep Meow in voice permanently!*"
                        ),
                        color=0xED4245,
                        timestamp=discord.utils.utcnow()
                    )
                    embed.set_footer(text="🐾 Server Music Suite • Inactivity Policy")
                    await send_card(self.text_channel, embed=embed)
                except Exception:
                    pass

    def cancel_alone_timer(self):
        if self.alone_task and not self.alone_task.done():
            self.alone_task.cancel()
        self.alone_task = None

    def start_alone_timer(self):
        if self.is_247:
            return
        self.cancel_alone_timer()
        self.alone_task = self.bot.loop.create_task(self._alone_timeout())

    async def _alone_timeout(self):
        """Disconnects the bot after 5 minutes (300 seconds) of being alone in the channel."""
        await asyncio.sleep(300)
        if self.is_247:
            return
        if self.voice_client and self.voice_client.is_connected():
            humans = [m for m in self.voice_client.channel.members if not m.bot]
            if len(humans) == 0:
                try:
                    await self.voice_client.disconnect()
                except Exception:
                    pass
                self.voice_client = None
                self.current = None
                self.queue.clear()
                if self.text_channel:
                    try:
                        embed = discord.Embed(
                            title="👋 Auto-Disconnected",
                            description=(
                                "Left the voice channel because everyone left (empty for **5 minutes**).\n\n"
                                "*👑 Server owner can enable `/247` to keep Meow in voice permanently!*"
                            ),
                            color=0xED4245,
                            timestamp=discord.utils.utcnow()
                        )
                        embed.set_footer(text="🐾 Server Music Suite • Empty Channel Policy")
                        await send_card(self.text_channel, embed=embed)
                    except Exception:
                        pass

    async def _resolve_stream_url(self, track: Track) -> str | None:
        loop = asyncio.get_running_loop()

        def extract():
            opts = dict(YTDL_OPTIONS)
            target = track.webpage_url or track.title
            if target.startswith("http://") or target.startswith("https://"):
                opts["default_search"] = "auto"
            else:
                opts["default_search"] = "ytsearch1"

            with yt_dlp.YoutubeDL(opts) as ytdl:
                try:
                    info = ytdl.extract_info(target, download=False)
                    if "entries" in info and info["entries"]:
                        return info["entries"][0].get("url")
                    return info.get("url")
                except Exception as e:
                    print(f"[MUSIC] URL extraction failed for '{target}': {e}", flush=True)
                    return None

        return await loop.run_in_executor(None, extract)

    async def play_next(self, error=None):
        async with self._play_lock:
            if error:
                print(f"[MUSIC] Playback callback error in guild {self.guild_id}: {error}", flush=True)

            if not self.voice_client or not self.voice_client.is_connected():
                self.current = None
                return

            if not self.queue:
                self.current = None
                if not self.is_247:
                    self.start_inactivity_timer()
                return

            self.cancel_inactivity_timer()
            self.current = self.queue.pop(0)

            # Re-resolve stream URL if necessary to prevent 403 Forbidden tokens
            stream_url = self.current.source_url
            if not self.current.is_radio and (
                not stream_url
                or "manifest.m3u8" in stream_url
                or "googlevideo.com" in stream_url
                or "soundcloud.cloud" in stream_url
            ):
                resolved = await self._resolve_stream_url(self.current)
                if resolved:
                    stream_url = resolved
                    self.current.source_url = resolved

            try:
                ch_bitrate = getattr(self.voice_client.channel, "bitrate", 128000)
                self.bitrate_kbps = min(max(int(ch_bitrate / 1000), 64), 384)

                if discord.opus.is_loaded():
                    raw_source = discord.FFmpegPCMAudio(
                        stream_url,
                        executable=FFMPEG_EXE,
                        before_options=FFMPEG_BEFORE_OPTIONS,
                        options="-vn"
                    )
                    source = discord.PCMVolumeTransformer(raw_source, volume=self.volume)
                    stream_mode = "PCM+libopus"
                else:
                    source = discord.FFmpegOpusAudio(
                        stream_url,
                        executable=FFMPEG_EXE,
                        bitrate=self.bitrate_kbps,
                        before_options=FFMPEG_BEFORE_OPTIONS,
                        options=f"-af volume={self.volume}"
                    )
                    stream_mode = "FFmpegOpus"

                def _after(err):
                    if err:
                        print(f"[MUSIC] AudioPlayer thread error in guild {self.guild_id}: {err}", flush=True)
                    asyncio.run_coroutine_threadsafe(self.play_next(err), self.bot.loop)

                if self.voice_client.is_playing():
                    self.voice_client.stop()

                self.voice_client.play(source, after=_after)
                print(f"[MUSIC] 🎶 Started playing '{self.current.title}' via {stream_mode} ({self.bitrate_kbps}kbps) in guild {self.guild_id}", flush=True)

            except Exception as e:
                print(f"[MUSIC] Exception starting track '{self.current.title}': {type(e).__name__}: {e}", flush=True)
                if self.text_channel:
                    try:
                        await self.text_channel.send(f"⚠️ Error streaming **{self.current.title}**: `{type(e).__name__}: {e}`. Advancing queue...")
                    except Exception:
                        pass
                self.bot.loop.create_task(self.play_next())


# -----------------------------------------------------------------------------
# Interactive Voice Controls View
# -----------------------------------------------------------------------------

class MusicControlsView(discord.ui.View):
    def __init__(self, player: GuildMusicPlayer):
        super().__init__(timeout=None)
        self.player = player

    @discord.ui.button(label="Pause / Resume", style=discord.ButtonStyle.primary, emoji="⏯️", custom_id="music_pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message("❌ The bot is not currently in a voice channel.", ephemeral=True)

        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Music paused.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Music resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is currently playing.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️", custom_id="music_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message("❌ The bot is not in a voice channel.", ephemeral=True)

        if vc.is_playing() or vc.is_paused():
            skipped_title = self.player.current.title if self.player.current else "Current track"
            vc.stop()
            await interaction.response.send_message(f"⏭️ Skipped: **{skipped_title}**", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing to skip.", ephemeral=True)

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="music_queue")
    async def view_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.current and not self.player.queue:
            return await interaction.response.send_message("📭 The queue is completely empty!", ephemeral=True)

        embed = discord.Embed(title="🎶 Music Queue", color=0x5865F2)
        if self.player.current:
            dur = format_duration(self.player.current.duration)
            web_url = self.player.current.webpage_url or ""
            embed.description = f"**Now Playing:** 🎵 [{self.player.current.title}]({web_url}) `[{dur}]`\n\n### Upcoming Tracks:"

        lines = []
        for i, t in enumerate(self.player.queue[:8], start=1):
            dur = format_duration(t.duration)
            lines.append(f"`{i}.` **{t.title}** `[{dur}]` • *Requested by {t.requester}*")

        if not lines:
            embed.description += "\n*No upcoming tracks queued. Use `/play` to add more!*"
        else:
            embed.description += "\n" + "\n".join(lines)
            if len(self.player.queue) > 8:
                embed.description += f"\n*...and {len(self.player.queue) - 8} more tracks.*"

        await send_card(interaction, embed=embed, ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️", custom_id="music_stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message("❌ The bot is not in a voice channel.", ephemeral=True)

        self.player.queue.clear()
        self.player.current = None
        self.player.manual_stop = True

        if self.player.is_247:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await interaction.response.send_message("⏹️ Playback stopped and queue cleared. (Staying in VC because **24/7 Mode** is enabled).", ephemeral=True)
        else:
            await vc.disconnect()
            self.player.voice_client = None
            await interaction.response.send_message("⏹️ Playback stopped, queue cleared, and disconnected from voice.", ephemeral=True)


# -----------------------------------------------------------------------------
# Main Music Cog
# -----------------------------------------------------------------------------

class Music(commands.Cog):
    """Full-featured Music & Voice entertainment suite with 24/7 persistence."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildMusicPlayer] = {}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "music"):
                await interaction.response.send_message(
                    "⚠️ The **Music & Radio** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True
                )
                return False
        return True

    def get_player(self, guild_id: int) -> GuildMusicPlayer:
        if guild_id not in self.players:
            player = GuildMusicPlayer(self.bot, guild_id)
            self.players[guild_id] = player
        return self.players[guild_id]

    async def _ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        """Connects to the user's voice channel or moves to it reliably."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("❌ You must join a voice channel first to use music!", ephemeral=True)
            return None

        target_channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client

        # Clean up stale/zombie voice clients if present
        if vc and not vc.is_connected():
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            vc = None

        if not vc:
            try:
                vc = await target_channel.connect(timeout=20.0, reconnect=True)
            except Exception as e:
                await interaction.followup.send(f"❌ Failed to connect to voice: `{e}`", ephemeral=True)
                return None
        elif vc.channel.id != target_channel.id:
            try:
                await vc.move_to(target_channel)
            except Exception as e:
                await interaction.followup.send(f"❌ Failed to move to your voice channel: `{e}`", ephemeral=True)
                return None

        return vc

    # -------------------------------------------------------------------------
    # Auto-Reconnect Listeners
    # -------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self):
        """Auto-reconnect to 24/7 channels across bot restarts."""
        await self.bot.wait_until_ready()
        try:
            records = await db.get_all_247_guilds()
            for guild_id, channel_id in records:
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue
                channel = guild.get_channel(channel_id)
                if not channel or not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                    continue
                if not guild.voice_client or not guild.voice_client.is_connected():
                    try:
                        vc = await channel.connect(timeout=15.0, reconnect=True)
                        player = self.get_player(guild_id)
                        player.voice_client = vc
                        player.is_247 = True
                        print(f"[MUSIC] 🌟 Auto-reconnected to 24/7 channel '{channel.name}' in guild '{guild.name}'", flush=True)
                    except Exception as e:
                        print(f"[MUSIC] Could not auto-reconnect 24/7 to {channel.name}: {e}", flush=True)
        except Exception as e:
            print(f"[MUSIC] on_ready 24/7 check error: {e}", flush=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Monitor empty channels and unexpected 24/7 disconnects."""
        # 1. If the bot itself was disconnected unexpectedly
        if member.id == self.bot.user.id:
            if before.channel and not after.channel:
                player = self.players.get(before.channel.guild.id)
                if player:
                    player.voice_client = None
                    player.cancel_inactivity_timer()
                    player.cancel_alone_timer()
                    # Auto-reconnect if 24/7 was active and user did not use /stop
                    if player.is_247 and not player.manual_stop:
                        print(f"[MUSIC] 24/7 voice drop in {before.channel.guild.name}. Reconnecting in 3s...", flush=True)
                        await asyncio.sleep(3)
                        try:
                            vc = await before.channel.connect(timeout=15.0, reconnect=True)
                            player.voice_client = vc
                            print(f"[MUSIC] Reconnected 24/7 in {before.channel.name}!", flush=True)
                        except Exception as e:
                            print(f"[MUSIC] Reconnect error: {e}", flush=True)
            return

        # 2. Monitor channel population (5-min auto-disconnect if empty)
        guild = member.guild
        vc = guild.voice_client
        if not vc or not vc.channel:
            return

        player = self.get_player(guild.id)
        if before.channel == vc.channel or after.channel == vc.channel:
            humans = [m for m in vc.channel.members if not m.bot]
            if len(humans) == 0:
                if not player.is_247:
                    player.start_alone_timer()
            else:
                player.cancel_alone_timer()

    # -------------------------------------------------------------------------
    # 24/7 Voice Command (Owner-Only)
    # -------------------------------------------------------------------------

    @app_commands.command(name="247", description="Toggle 24/7 voice mode (Keep bot in VC indefinitely - Owner only).")
    @app_commands.describe(channel="Voice channel to stay in 24/7 (optional, defaults to current channel)")
    async def mode_247(self, interaction: discord.Interaction, channel: discord.VoiceChannel = None):
        is_guild_owner = (interaction.guild and interaction.user.id == interaction.guild.owner_id)
        is_bot_owner = await self.bot.is_owner(interaction.user)
        if not is_guild_owner and not is_bot_owner:
            return await interaction.response.send_message(
                "❌ Only the **Server Owner** or **Bot Owner** can configure 24/7 voice mode!",
                ephemeral=True
            )

        player = self.get_player(interaction.guild.id)
        current_247, saved_ch = await db.get_247_setting(interaction.guild.id)
        new_247 = not current_247

        if new_247:
            target_ch = channel
            if not target_ch:
                if interaction.guild.voice_client and interaction.guild.voice_client.channel:
                    target_ch = interaction.guild.voice_client.channel
                elif interaction.user.voice and interaction.user.voice.channel:
                    target_ch = interaction.user.voice.channel

            if not target_ch:
                return await interaction.response.send_message(
                    "❌ Please join a voice channel first or specify one: `/247 channel:#voice`",
                    ephemeral=True
                )

            vc = interaction.guild.voice_client
            if not vc or not vc.is_connected():
                try:
                    vc = await target_ch.connect(timeout=15.0, reconnect=True)
                except Exception as e:
                    return await interaction.response.send_message(f"❌ Failed to connect to {target_ch.mention}: `{e}`", ephemeral=True)
            elif vc.channel.id != target_ch.id:
                await vc.move_to(target_ch)

            player.voice_client = vc
            player.is_247 = True
            player.manual_stop = False
            player.cancel_inactivity_timer()
            player.cancel_alone_timer()
            await db.set_247_setting(interaction.guild.id, target_ch.id, True)

            embed = discord.Embed(
                title="👑 24/7 Voice Mode Enabled",
                description=(
                    f"### Stay-Connected Active\n"
                    f"> 🔊 **Voice Channel:** {target_ch.mention}\n"
                    f"> 🛡️ **Configured By:** {interaction.user.mention}\n"
                    f"> ⚡ **Auto-Disconnect:** `Disabled (Stays 24/7)`\n\n"
                    f"🐾 *Meow will now stay in this voice channel permanently across idle periods and restarts.*"
                ),
                color=0xF1C40F,
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text="🐾 Server Music Suite • 24/7 Mode")
            await send_card(interaction, embed=embed)
        else:
            player.is_247 = False
            player.manual_stop = False
            await db.set_247_setting(interaction.guild.id, 0, False)

            vc = interaction.guild.voice_client
            if vc and not vc.is_playing() and not vc.is_paused():
                player.start_inactivity_timer()

            embed = discord.Embed(
                title="⏹️ 24/7 Voice Mode Disabled",
                description=(
                    f"### Standard Idle Policy Restored\n"
                    f"> ⏱️ **Inactivity Timeout:** `5 Minutes`\n"
                    f"> 🛡️ **Configured By:** {interaction.user.mention}\n"
                    f"> ⚡ **Status:** Meow will now automatically disconnect after **5 minutes** of silence or when the channel is empty."
                ),
                color=0xED4245,
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text="🐾 Server Music Suite • 24/7 Mode")
            await send_card(interaction, embed=embed)

    # -------------------------------------------------------------------------
    # 24/7 Voice Radio Command
    # -------------------------------------------------------------------------

    @app_commands.command(name="radio", description="Stream 24/7 lag-free music radio stations in your voice channel!")
    @app_commands.describe(station="Choose a 24/7 radio station")
    @app_commands.choices(station=[
        app_commands.Choice(name="☕ 24/7 Lo-Fi Chill Beats (Study / Chill)", value="lofi"),
        app_commands.Choice(name="🎮 Gaming EDM & Beats", value="gaming"),
        app_commands.Choice(name="🌃 Nightwave Plaza (Synthwave & Retro)", value="synthwave"),
        app_commands.Choice(name="🌸 Anime & J-Pop (Listen.moe)", value="anime"),
        app_commands.Choice(name="🎷 Smooth Coffeehouse Jazz", value="jazz"),
        app_commands.Choice(name="📻 Top 40 Pop Hits", value="pop"),
    ])
    async def radio(self, interaction: discord.Interaction, station: app_commands.Choice[str]):
        await interaction.response.defer()
        vc = await self._ensure_voice(interaction)
        if not vc:
            return

        st_info = RADIO_STATIONS.get(station.value, RADIO_STATIONS["lofi"])
        player = self.get_player(interaction.guild.id)
        player.voice_client = vc
        player.text_channel = interaction.channel
        player.manual_stop = False

        is_247_db, _ = await db.get_247_setting(interaction.guild.id)
        player.is_247 = is_247_db

        player.queue.clear()
        if vc.is_playing() or vc.is_paused():
            vc.stop()

        radio_track = Track(
            title=st_info["name"],
            source_url=st_info["url"],
            duration=0,
            requester=interaction.user.display_name,
            is_radio=True
        )

        player.queue.append(radio_track)
        await player.play_next()

        embed = discord.Embed(
            title=f"{st_info["icon"]} 24/7 Radio Station Started",
            description=(
                f"### {st_info["name"]}\n"
                f"{st_info["desc"]}\n\n"
                f"> 🔊 **Voice Channel:** {vc.channel.mention}\n"
                f"> 👤 **Started By:** {interaction.user.mention}\n"
                f"> ⚡ **Uptime:** 24/7 Continuous Non-Stop Stream"
            ),
            color=st_info["color"],
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Server Radio • Click controls below to pause or stop")
        view = MusicControlsView(player)
        await interaction.followup.send(embed=embed, view=view)

    # -------------------------------------------------------------------------
    # Full Voice Player: /play
    # -------------------------------------------------------------------------

    @app_commands.command(name="play", description="Play any song or stream from SoundCloud, YouTube, or audio link!")
    @app_commands.describe(query="Song title, artist name, or direct music link")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        vc = await self._ensure_voice(interaction)
        if not vc:
            return

        player = self.get_player(interaction.guild.id)
        player.voice_client = vc
        player.text_channel = interaction.channel
        player.manual_stop = False

        is_247_db, _ = await db.get_247_setting(interaction.guild.id)
        player.is_247 = is_247_db

        loop = asyncio.get_running_loop()

        # Check for Spotify links
        spotify_first = None
        spotify_queued_count = 0
        search_target = query

        if is_spotify_url(query):
            entity_type, spotify_tracks = await loop.run_in_executor(None, resolve_spotify, query)
            if not spotify_tracks:
                return await interaction.followup.send(
                    "❌ Could not load Spotify metadata for that link. Please check that the track/playlist is public.",
                    ephemeral=True
                )

            spotify_first = spotify_tracks[0]
            search_target = spotify_first["query"]

            if entity_type in ("album", "playlist") and len(spotify_tracks) > 1:
                for st in spotify_tracks[1:]:
                    player.queue.append(Track(
                        title=f"{st['title']} - {st['artist']}"[:90],
                        source_url="",
                        duration=int(st.get("duration_ms", 0) / 1000),
                        requester=interaction.user.display_name,
                        thumbnail=st.get("thumbnail"),
                        webpage_url=st.get("query")
                    ))
                spotify_queued_count = len(spotify_tracks) - 1

        def extract():
            opts = dict(YTDL_OPTIONS)
            if search_target.startswith("http://") or search_target.startswith("https://"):
                opts["default_search"] = "auto"
                with yt_dlp.YoutubeDL(opts) as ytdl:
                    try:
                        info = ytdl.extract_info(search_target, download=False)
                        if "entries" in info and info["entries"]:
                            return info["entries"][0]
                        return info
                    except Exception as e:
                        print(f"[MUSIC] Direct URL extraction error: {e}", flush=True)
                        return None

            expected_sec = int(spotify_first.get("duration_ms", 0) / 1000) if spotify_first else 0

            # Prioritize YouTube for full official audio; fallback to SoundCloud
            providers = ["ytsearch5:", "scsearch5:"]
            for prefix in providers:
                search_query = f"{prefix}{search_target}"
                with yt_dlp.YoutubeDL(opts) as ytdl:
                    try:
                        info = ytdl.extract_info(search_query, download=False)
                        entries = info.get("entries") or ([info] if info.get("url") else [])
                        if not entries:
                            continue

                        # If expected duration is known (> 60s), reject short clips (< 60s) and find closest duration
                        if expected_sec > 60:
                            candidates = [e for e in entries if e and e.get("duration") and e.get("duration") >= 60]
                            if candidates:
                                candidates.sort(key=lambda e: abs(e.get("duration") - expected_sec))
                                return candidates[0]

                        # For any general song search, avoid short snippet clips (< 50s) if longer full tracks exist
                        long_candidates = [e for e in entries if e and e.get("duration") and e.get("duration") >= 50]
                        if long_candidates:
                            return long_candidates[0]

                        if entries and entries[0]:
                            return entries[0]
                    except Exception as e:
                        print(f"[MUSIC] Error searching '{search_query}': {e}", flush=True)

            return None

        data = await loop.run_in_executor(None, extract)
        if not data or not data.get("url"):
            return await interaction.followup.send("❌ Could not find or stream that track. Try a different title or direct link!", ephemeral=True)

        track_title = (f"{spotify_first['title']} - {spotify_first['artist']}" if spotify_first else data.get("title", query))[:90]
        track_url = data.get("url")
        track_duration = data.get("duration", 0) or (int(spotify_first.get("duration_ms", 0) / 1000) if spotify_first else 0)
        track_thumb = (spotify_first.get("thumbnail") if spotify_first and spotify_first.get("thumbnail") else data.get("thumbnail"))
        webpage_url = data.get("webpage_url") or (query if spotify_first else "")

        track = Track(
            title=track_title,
            source_url=track_url,
            duration=track_duration,
            requester=interaction.user.display_name,
            thumbnail=track_thumb,
            webpage_url=webpage_url
        )

        is_currently_playing = vc.is_playing() or vc.is_paused()
        player.queue.append(track)

        if not is_currently_playing:
            await player.play_next()
            embed = discord.Embed(
                title="🎶 Now Playing",
                description=(
                    f"### [{track.title}]({track.webpage_url})\n"
                    f"> ⏱️ **Duration:** `{format_duration(track.duration)}`\n"
                    f"> 👤 **Requested by:** {interaction.user.mention}\n"
                    f"> 🔊 **Voice Channel:** {vc.channel.mention}\n"
                    f"> 💎 **Audio Fidelity:** `Hi-Res ({player.bitrate_kbps} kbps • 48 kHz Opus Music)`"
                    + (f"\n> 🎧 **Source:** Spotify Track" if spotify_first else "")
                ),
                color=0x1DB954 if spotify_first else 0x5865F2,
                timestamp=discord.utils.utcnow()
            )
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
            if spotify_queued_count > 0:
                embed.add_field(name="💿 Spotify Collection", value=f"Queued **+{spotify_queued_count} more songs** from Spotify!", inline=False)
            embed.set_footer(text="🐾 Server Music Player • Use buttons below to control")
            view = MusicControlsView(player)
            await interaction.followup.send(embed=embed, view=view)
        else:
            q_pos = len(player.queue)
            embed = discord.Embed(
                title="✅ Added to Queue",
                description=(
                    f"### [{track.title}]({track.webpage_url})\n"
                    f"> ⏱️ **Duration:** `{format_duration(track.duration)}`\n"
                    f"> 🔢 **Position:** `#{q_pos}` in queue\n"
                    f"> 👤 **Requested by:** {interaction.user.mention}"
                    + (f"\n> 🎧 **Source:** Spotify Track" if spotify_first else "")
                ),
                color=0x1DB954 if spotify_first else 0x2ECC71,
                timestamp=discord.utils.utcnow()
            )
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
            if spotify_queued_count > 0:
                embed.add_field(name="💿 Spotify Collection", value=f"Queued **+{spotify_queued_count} more songs** from Spotify!", inline=False)
            embed.set_footer(text=f"🐾 Next track plays automatically • {len(player.queue)} in queue")
            await send_card(interaction, embed=embed)

    # -------------------------------------------------------------------------
    # Voice Controls: /pause, /resume, /skip, /stop, /queue, /nowplaying, /volume
    # -------------------------------------------------------------------------

    @app_commands.command(name="pause", description="Pause the currently playing song.")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message("❌ The bot is not connected to a voice channel.", ephemeral=True)
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Music playback has been paused.")
        else:
            await interaction.response.send_message("❌ Nothing is currently playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback if paused.")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message("❌ The bot is not connected to a voice channel.", ephemeral=True)
        if vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Music playback resumed.")
        else:
            await interaction.response.send_message("❌ Music is not paused.", ephemeral=True)

    @app_commands.command(name="skip", description="Skip to the next song in queue.")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message("❌ The bot is not in a voice channel.", ephemeral=True)

        player = self.get_player(interaction.guild.id)
        if vc.is_playing() or vc.is_paused():
            skipped = player.current.title if player.current else "Current song"
            vc.stop()
            await interaction.response.send_message(f"⏭️ Skipped: **{skipped}**")
        else:
            await interaction.response.send_message("❌ Nothing is currently playing to skip.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop music, clear the queue, and disconnect from voice.")
    async def stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message("❌ The bot is not currently in a voice channel.", ephemeral=True)

        player = self.get_player(interaction.guild.id)
        player.queue.clear()
        player.current = None
        player.manual_stop = True

        if player.is_247:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await interaction.response.send_message("⏹️ Playback stopped and queue cleared. (Staying in voice because **24/7 Mode** is enabled).")
        else:
            await vc.disconnect()
            player.voice_client = None
            await interaction.response.send_message("⏹️ Playback stopped, queue cleared, and disconnected from voice.")

    @app_commands.command(name="queue", description="Display the current songs in the queue.")
    async def queue_cmd(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        if not player.current and not player.queue:
            return await interaction.response.send_message("📭 The queue is completely empty! Use `/play` or `/radio` to start music.", ephemeral=True)

        embed = discord.Embed(title=f"🎶 {interaction.guild.name} — Music Queue", color=0x5865F2, timestamp=discord.utils.utcnow())
        if player.current:
            dur = format_duration(player.current.duration)
            embed.description = f"**Currently Playing:**\n🎵 **{player.current.title}** `[{dur}]`\n\n### Up Next:"

        lines = []
        for i, t in enumerate(player.queue[:10], start=1):
            dur = format_duration(t.duration)
            lines.append(f"`{i}.` **{t.title}** `[{dur}]` • *Req: {t.requester}*")

        if not lines:
            embed.description += "\n*Queue is clear! Use `/play <song>` to queue more tracks.*"
        else:
            embed.description += "\n" + "\n".join(lines)
            if len(player.queue) > 10:
                embed.description += f"\n*...and {len(player.queue) - 10} more songs in queue.*"

        embed.set_footer(text=f"Total: {len(player.queue) + (1 if player.current else 0)} track(s) • 24/7 Mode: {'ON' if player.is_247 else 'OFF'}")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="nowplaying", description="Show full info and controls for the currently playing track.")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        if not player.current:
            return await interaction.response.send_message("❌ Nothing is currently playing! Use `/play` or `/radio` to start.", ephemeral=True)

        cur = player.current
        dur = format_duration(cur.duration)
        embed = discord.Embed(
            title="🎶 Now Playing",
            description=(
                f"### [{cur.title}]({cur.webpage_url or ''})\n\n"
                f"> ⏱️ **Duration:** `{dur}`\n"
                f"> 👤 **Requested By:** `{cur.requester}`\n"
                f"> 🔊 **Volume:** `{int(player.volume * 100)}%`\n"
                f"> 👑 **24/7 Mode:** `{ENABLED if player.is_247 else DISABLED}`\n"
                f"> 💎 **Audio Fidelity:** `Hi-Res ({player.bitrate_kbps} kbps • 48 kHz Opus Music)`"
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        if cur.thumbnail:
            embed.set_thumbnail(url=cur.thumbnail)
        embed.set_footer(text="🐾 Server Music Suite")
        view = MusicControlsView(player)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="volume", description="Set playback volume (1% to 150%).")
    @app_commands.describe(percent="Volume percentage between 1 and 150")
    async def volume(self, interaction: discord.Interaction, percent: int):
        if percent < 1 or percent > 150:
            return await interaction.response.send_message("❌ Volume must be between `1%` and `150%`!", ephemeral=True)

        player = self.get_player(interaction.guild.id)
        player.volume = percent / 100.0
        await interaction.response.send_message(f"🔊 Playback volume set to **{percent}%**! (Applied to active and upcoming tracks)")

    # -------------------------------------------------------------------------
    # Music Utilities: /songinfo, /lyrics
    # -------------------------------------------------------------------------

    @app_commands.command(name="songinfo", description="Search any track for album artwork, audio preview, artist, and release info.")
    @app_commands.describe(query="Song title or artist to search")
    async def songinfo(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        url = "https://itunes.apple.com/search"
        params = {"term": query, "entity": "song", "limit": 1}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return await interaction.followup.send("❌ Error searching for song information.", ephemeral=True)
                    data = await resp.json(content_type=None)
        except Exception as e:
            return await interaction.followup.send(f"❌ Failed to reach music search API: `{e}`", ephemeral=True)

        if not data.get("resultCount") or not data.get("results"):
            return await interaction.followup.send(f"❌ No tracks found matching **'{query}'.**", ephemeral=True)

        t = data["results"][0]
        track_name = t.get("trackName", "Unknown Title")
        artist_name = t.get("artistName", "Unknown Artist")
        album_name = t.get("collectionName", "Single")
        genre = t.get("primaryGenreName", "Music")
        release_date = t.get("releaseDate", "")[:10]
        preview_url = t.get("previewUrl")
        view_url = t.get("trackViewUrl")
        artwork = t.get("artworkUrl100", "").replace("100x100bb.jpg", "600x600bb.jpg")

        embed = discord.Embed(
            title=f"🎵 {track_name}",
            url=view_url,
            color=0xFA2D48,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name=artist_name)
        if artwork:
            embed.set_thumbnail(url=artwork)

        embed.add_field(name="💿 Album", value=f"`{album_name}`", inline=True)
        embed.add_field(name="🎸 Genre", value=f"`{genre}`", inline=True)
        embed.add_field(name="📅 Released", value=f"`{release_date}`", inline=True)

        if preview_url:
            embed.add_field(
                name="🎧 30-Second Audio Preview",
                value=f"[▶️ Listen to Preview Clip]({preview_url})",
                inline=False
            )

        embed.set_footer(text="🐾 Server Music Explorer • Powered by iTunes & Apple Music")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="lyrics", description="Look up lyrics for any song.")
    @app_commands.describe(title="Title of the song", artist="Artist of the song (optional)")
    async def lyrics(self, interaction: discord.Interaction, title: str, artist: str = None):
        await interaction.response.defer()

        query_artist = artist.strip() if artist else ""
        query_title = title.strip()

        lyrics_text = None
        found_artist = query_artist
        found_title = query_title

        async with aiohttp.ClientSession() as session:
            if not query_artist:
                try:
                    async with session.get("https://itunes.apple.com/search", params={"term": query_title, "entity": "song", "limit": 1}, timeout=aiohttp.ClientTimeout(total=4)) as r:
                        if r.status == 200:
                            sdata = await r.json(content_type=None)
                            if sdata.get("resultCount") > 0:
                                found_artist = sdata["results"][0].get("artistName", "")
                                found_title = sdata["results"][0].get("trackName", query_title)
                except Exception:
                    pass

            if found_artist and found_title:
                try:
                    clean_art = found_artist.split("feat.")[0].split("&")[0].strip()
                    clean_tit = found_title.split("(feat.")[0].split("-")[0].strip()
                    api_url = f"https://api.lyrics.ovh/v1/{clean_art}/{clean_tit}"
                    async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            lyrics_text = data.get("lyrics")
                except Exception:
                    pass

        if not lyrics_text:
            return await interaction.followup.send(
                f"❌ Sorry, could not find lyrics for **'{title}'** by **'{artist or 'Various'}'.\nTry specifying both the song title and artist name! (e.g. `/lyrics title:Blinding Lights artist:The Weeknd`)",
                ephemeral=True
            )

        clean_lyrics = lyrics_text.strip()
        if len(clean_lyrics) > 3800:
            clean_lyrics = clean_lyrics[:3800] + "\n\n*... [Lyrics truncated for length]*"

        embed = discord.Embed(
            title=f"📜 Lyrics — {found_title}",
            description=f"**Artist:** `{found_artist}`\n\n```\n{clean_lyrics}\n```",
            color=0x1DB954,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Server Lyrics Finder • Powered by Lyrics.ovh")
        await send_card(interaction, embed=embed)

    # -------------------------------------------------------------------------
    # Auto-Showcase in 💬・fav-songs (1547683807916851240)
    # -------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        is_fav_channel = (message.channel.id == 1547683807916851240) or ("fav-song" in message.channel.name.lower())
        if not is_fav_channel:
            return

        content = message.content.strip()
        music_domains = ("spotify.com", "music.apple.com", "soundcloud.com", "youtube.com/watch", "youtu.be")
        if not any(dom in content for dom in music_domains):
            return

        try:
            for emoji in ("❤️", "🔥", "🎧", "✨"):
                await message.add_reaction(emoji)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
