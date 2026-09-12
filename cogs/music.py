import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import aiohttp
import yt_dlp
import imageio_ffmpeg
import random
import datetime

# -----------------------------------------------------------------------------
# Audio & FFmpeg Configuration
# -----------------------------------------------------------------------------

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn"
}

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
        self.volume: float = 0.70
        self.is_paused: bool = False
        self.inactivity_task: asyncio.Task | None = None

    def cancel_inactivity_timer(self):
        if self.inactivity_task and not self.inactivity_task.done():
            self.inactivity_task.cancel()
        self.inactivity_task = None

    def start_inactivity_timer(self):
        self.cancel_inactivity_timer()
        self.inactivity_task = self.bot.loop.create_task(self._inactivity_timeout())

    async def _inactivity_timeout(self):
        """Disconnects the bot after 3 minutes of silence/inactivity."""
        await asyncio.sleep(180)
        if self.voice_client and self.voice_client.is_connected() and not self.voice_client.is_playing():
            try:
                await self.voice_client.disconnect()
            except Exception:
                pass
            self.voice_client = None
            self.current = None
            self.queue.clear()

    def play_next(self, error=None):
        if error:
            print(f"Music playback error in guild {self.guild_id}: {error}")

        if not self.voice_client or not self.voice_client.is_connected():
            self.current = None
            return

        if not self.queue:
            self.current = None
            self.start_inactivity_timer()
            return

        self.cancel_inactivity_timer()
        self.current = self.queue.pop(0)

        try:
            raw_source = discord.FFmpegPCMAudio(
                self.current.source_url,
                executable=FFMPEG_EXE,
                before_options=FFMPEG_OPTIONS["before_options"],
                options=FFMPEG_OPTIONS["options"]
            )
            vol_source = discord.PCMVolumeTransformer(raw_source, volume=self.volume)
            self.voice_client.play(vol_source, after=self.play_next)
        except Exception as e:
            print(f"Error starting track {self.current.title}: {e}")
            self.play_next()


# -----------------------------------------------------------------------------
# Interactive Voice Controls View
# -----------------------------------------------------------------------------

class MusicControlsView(discord.ui.View):
    def __init__(self, player: GuildMusicPlayer):
        super().__init__(timeout=300)
        self.player = player

    @discord.ui.button(label="Pause / Resume", style=discord.ButtonStyle.primary, emoji="⏯️")
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

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️")
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

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.secondary, emoji="📜")
    async def view_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.current and not self.player.queue:
            return await interaction.response.send_message("📭 The queue is completely empty!", ephemeral=True)

        embed = discord.Embed(title="🎶 Music Queue", color=0x5865F2)
        if self.player.current:
            dur = format_duration(self.player.current.duration)
            embed.description = f"**Now Playing:** 🎵 [{self.player.current.title}]({self.player.current.webpage_url or ''}) `[{dur}]`\n\n### Upcoming Tracks:"

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

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message("❌ The bot is not in a voice channel.", ephemeral=True)

        self.player.queue.clear()
        self.player.current = None
        await vc.disconnect()
        self.player.voice_client = None
        await interaction.response.send_message("⏹️ Playback stopped and disconnected from voice.", ephemeral=True)


# -----------------------------------------------------------------------------
# Main Music Cog
# -----------------------------------------------------------------------------

class Music(commands.Cog):
    """Full-featured Music & Voice entertainment suite."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildMusicPlayer] = {}

    def get_player(self, guild_id: int) -> GuildMusicPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildMusicPlayer(self.bot, guild_id)
        return self.players[guild_id]

    async def _ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        """Connects to the user's voice channel or moves to it."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("❌ You must join a voice channel first to use music!", ephemeral=True)
            return None

        target_channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client

        if not vc:
            try:
                vc = await target_channel.connect()
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

        # Clear queue and stop existing track
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
        player.play_next()

        embed = discord.Embed(
            title=f"{st_info['icon']} 24/7 Radio Station Started",
            description=(
                f"### {st_info['name']}\n"
                f"{st_info['desc']}\n\n"
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

        # Asynchronous audio extraction in thread
        loop = asyncio.get_running_loop()

        def extract():
            opts = dict(YTDL_OPTIONS)
            # If direct link, don't use scsearch
            if query.startswith("http://") or query.startswith("https://"):
                opts["default_search"] = "auto"
            else:
                opts["default_search"] = "scsearch1"

            with yt_dlp.YoutubeDL(opts) as ytdl:
                try:
                    info = ytdl.extract_info(query, download=False)
                    if "entries" in info and info["entries"]:
                        return info["entries"][0]
                    return info
                except Exception as e:
                    # Fallback to YouTube search if SoundCloud had no match
                    if not (query.startswith("http://") or query.startswith("https://")):
                        opts["default_search"] = "ytsearch1"
                        with yt_dlp.YoutubeDL(opts) as ytdl_fallback:
                            try:
                                fb_info = ytdl_fallback.extract_info(query, download=False)
                                if "entries" in fb_info and fb_info["entries"]:
                                    return fb_info["entries"][0]
                                return fb_info
                            except Exception:
                                pass
                    print(f"YTDL error: {e}")
                    return None

        data = await loop.run_in_executor(None, extract)
        if not data or not data.get("url"):
            return await interaction.followup.send("❌ Could not find or stream that track. Try a different title or direct link!", ephemeral=True)

        track_title = data.get("title", query)[:90]
        track_url = data.get("url")
        track_duration = data.get("duration", 0)
        track_thumb = data.get("thumbnail")
        webpage_url = data.get("webpage_url", "")

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
            player.play_next()
            embed = discord.Embed(
                title="🎶 Now Playing",
                description=f"### [{track.title}]({track.webpage_url})\n> ⏱️ **Duration:** `{format_duration(track.duration)}`\n> 👤 **Requested by:** {interaction.user.mention}\n> 🔊 **Voice Channel:** {vc.channel.mention}",
                color=0x5865F2,
                timestamp=discord.utils.utcnow()
            )
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
            embed.set_footer(text="🐾 Server Music Player • Use buttons below to control")
            view = MusicControlsView(player)
            await interaction.followup.send(embed=embed, view=view)
        else:
            q_pos = len(player.queue)
            embed = discord.Embed(
                title="✅ Added to Queue",
                description=f"### [{track.title}]({track.webpage_url})\n> ⏱️ **Duration:** `{format_duration(track.duration)}`\n> 🔢 **Position:** `#{q_pos}` in queue\n> 👤 **Requested by:** {interaction.user.mention}",
                color=0x2ECC71,
                timestamp=discord.utils.utcnow()
            )
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
            embed.set_footer(text=f"🐾 Next track plays automatically • {len(player.queue)} in queue")
            await interaction.followup.send(embed=embed)

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
        await vc.disconnect()
        player.voice_client = None
        await interaction.response.send_message("⏹️ Playback stopped, queue cleared, and disconnected.")

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

        embed.set_footer(text=f"Total: {len(player.queue) + (1 if player.current else 0)} track(s)")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="Show full info and controls for the currently playing track.")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        if not player.current:
            return await interaction.response.send_message("❌ Nothing is currently playing! Use `/play` or `/radio` to start.", ephemeral=True)

        cur = player.current
        dur = format_duration(cur.duration)
        embed = discord.Embed(
            title="🎶 Now Playing",
            description=f"### [{cur.title}]({cur.webpage_url or ''})\n\n> ⏱️ **Duration:** `{dur}`\n> 👤 **Requested By:** `{cur.requester}`\n> 🔊 **Volume:** `{int(player.volume * 100)}%`",
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

        vc = interaction.guild.voice_client
        if vc and vc.source:
            try:
                vc.source.volume = player.volume
            except Exception:
                pass

        await interaction.response.send_message(f"🔊 Playback volume set to **{percent}%**!")

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
            return await interaction.followup.send(f"❌ No tracks found matching **'{query}'**.", ephemeral=True)

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
        await interaction.followup.send(embed=embed)

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
                f"❌ Sorry, could not find lyrics for **'{title}'** by **'{artist or 'Various'}'**.\nTry specifying both the song title and artist name! (e.g. `/lyrics title:Blinding Lights artist:The Weeknd`)",
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
        await interaction.followup.send(embed=embed)

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
