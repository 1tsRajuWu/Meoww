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
import difflib
from utils import db
from utils.style import send_card
from utils.spotify import is_spotify_url, resolve_spotify
from utils.card import generate_now_playing_card

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

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -err_detect ignore_err -probesize 32768 -analyzeduration 0"

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch5",
    "source_address": "0.0.0.0",
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    }
}

# Flat options: fetch only metadata (title/duration/webpage_url), no stream URL extraction — very fast (~2s)
YTDL_FLAT_OPTIONS = {
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "source_address": "0.0.0.0",
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    }
}

# Stream options: used ONLY for Phase-2 single-video stream URL extraction
# No player_skip so yt-dlp can fully resolve the stream URL
YTDL_STREAM_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    }
}

# -----------------------------------------------------------------------------
# Studio Audio Equalizer & Filters (Real-Time DSP Presets)
# -----------------------------------------------------------------------------

AUDIO_FILTERS = {
    "normal": {
        "name": "Normal (Studio Clean)",
        "emoji": "🎵",
        "ffmpeg": "",
        "desc": "Original, uncompressed sound with flat frequency response."
    },
    "bassboost": {
        "name": "Bass Boost",
        "emoji": "🔊",
        "ffmpeg": "bass=g=10:f=110:w=0.6",
        "desc": "Deep low-end rumble and boosted sub-bass punch."
    },
    "nightcore": {
        "name": "Nightcore",
        "emoji": "🌙",
        "ffmpeg": "asetrate=48000*1.25,aresample=48000,atempo=1.05",
        "desc": "High-pitched, accelerated tempo electronic energy."
    },
    "vaporwave": {
        "name": "Vaporwave",
        "emoji": "🌊",
        "ffmpeg": "asetrate=48000*0.82,aresample=48000,atempo=0.92",
        "desc": "Slowed, pitched down, aesthetic retro chill."
    },
    "8d": {
        "name": "8D Spatial Audio",
        "emoji": "🎧",
        "ffmpeg": "apulsator=hz=0.125",
        "desc": "Dynamic 360-degree rotating binaural spatial panning."
    },
    "pop": {
        "name": "Pop / Vocal Boost",
        "emoji": "🎤",
        "ffmpeg": "equalizer=f=1000:t=q:w=1:g=2,equalizer=f=3000:t=q:w=1:g=3",
        "desc": "Enhanced vocal presence and elevated acoustic clarity."
    }
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
        is_radio: bool = False,
        actual_title: str = None
    ):
        self.title = title
        self.source_url = source_url
        self.duration = duration
        self.requester = requester
        self.thumbnail = thumbnail
        self.webpage_url = webpage_url
        self.is_radio = is_radio
        self.actual_title = actual_title


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
        self.current_filter: str = "normal"
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
                        title="💤  Left Voice — Inactive",
                        description=(
                            "No music was playing for **5 minutes** so I left to save resources.\n\n"
                            "> 🎵 `/play <song>` — start a new session anytime\n"
                            "> 👑 `/247` — keep me here permanently *(server owner only)*"
                        ),
                        color=0x5865F2,
                        timestamp=discord.utils.utcnow()
                    )
                    embed.set_footer(text="Meow • Auto-Disconnect • 5 min inactivity")
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
                            title="👥  Left Voice — Channel Empty",
                            description=(
                                "Everyone left the voice channel, so I left too after **5 minutes**.\n\n"
                                "> 🎵 `/play <song>` — hop back in anytime!\n"
                                "> 👑 `/247` — keep me here permanently *(server owner only)*"
                            ),
                            color=0x5865F2,
                            timestamp=discord.utils.utcnow()
                        )
                        embed.set_footer(text="Meow • Auto-Disconnect • Empty channel")
                        await send_card(self.text_channel, embed=embed)
                    except Exception:
                        pass

    async def _resolve_stream_url(self, track: Track) -> str | None:
        loop = asyncio.get_running_loop()

        def extract():
            target = track.webpage_url or track.title
            expected_sec = track.duration or 0
            search_term = track.title

            def _sim(a: str, b: str) -> float:
                return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()

            def score(e):
                dur = e.get("duration") or 0
                title = (e.get("title") or "").lower()
                uploader = (e.get("uploader") or "").lower()
                channel = (e.get("channel") or "").lower()
                
                # Check how many words from the search target match
                target_words = search_term.lower().split()
                matches = sum(1 for w in target_words if w in title or w in uploader or w in channel)
                word_match_ratio = matches / len(target_words) if target_words else 0
                
                dur_score = abs(dur - expected_sec) if expected_sec > 0 else 0
                short_penalty = 3600 if (expected_sec > 60 and 0 < dur < 60) else 0
                
                # Give bonuses (negative penalty) to official audios and exact duration
                bonus = 0
                if "official audio" in title or "topic" in channel or "topic" in uploader:
                    bonus += 15  # Equivalent to saving 15 seconds of error
                if "lyric" in title:
                    bonus += 5
                if "official video" in title or "music video" in title:
                    # Music videos often have long acting intros
                    bonus -= 10
                
                sim_score = _sim(search_term, title) * 30 
                word_match_bonus = word_match_ratio * 40
                
                final_score = dur_score + short_penalty - sim_score - word_match_bonus - bonus
                return final_score

            # 1. Direct URL path (YouTube / SoundCloud / CDN link)
            if target and target.startswith(("http://", "https://")):
                if not ("spotify.com" in target or "spotify.link" in target):
                    opts = dict(YTDL_STREAM_OPTIONS)
                    with yt_dlp.YoutubeDL(opts) as ytdl:
                        try:
                            info = ytdl.extract_info(target, download=False)
                            if info:
                                if "entries" in info and info["entries"]:
                                    url = info["entries"][0].get("url")
                                else:
                                    url = info.get("url")
                                if url:
                                    return url
                        except Exception as e:
                            print(f"[MUSIC] Direct URL extraction failed for '{target}': {e}", flush=True)
                    # fall through to search if direct URL failed / no stream found
            elif target and not target.startswith(("http://", "https://")):
                search_term = target

            # 2. Two-phase search: Phase 1 flat (~2s) → pick best → Phase 2 stream extract (~3s)
            # DO NOT fallback to SoundCloud unless explicitly requested to prevent 'Wrong Song' bug
            prefixes = ["scsearch5:"] if search_term.startswith("scsearch") else ["ytsearch5:"]
            
            for prefix in prefixes:
                search_query = f"{prefix}{search_term}"
                try:
                    # Phase 1: metadata only — fast
                    with yt_dlp.YoutubeDL(YTDL_FLAT_OPTIONS) as ytdl:
                        flat_info = ytdl.extract_info(search_query, download=False)
                    if not flat_info:
                        continue
                    entries = [e for e in (flat_info.get("entries") or []) if e]
                    if not entries:
                        continue

                    best_meta = sorted(entries, key=score)[0]
                    best_url = best_meta.get("url") or best_meta.get("webpage_url")
                    if not best_url:
                        continue

                    print(
                        f"[MUSIC] Phase-1 winner: '{best_meta.get('title')}' "
                        f"dur={best_meta.get('duration')}s for '{search_term}'",
                        flush=True
                    )

                    # Phase 2: stream URL extraction for the single winner
                    with yt_dlp.YoutubeDL(YTDL_STREAM_OPTIONS) as ytdl:
                        stream_info = ytdl.extract_info(best_url, download=False)
                    if not stream_info:
                        continue
                    if "entries" in stream_info and stream_info["entries"]:
                        stream_info = stream_info["entries"][0]
                    if not stream_info:
                        continue
                    stream_url = stream_info.get("url")
                    if stream_url:
                        if stream_info.get("webpage_url"):
                            track.webpage_url = stream_info["webpage_url"]
                        return stream_url
                except Exception as e:
                    print(f"[MUSIC] 2-phase search failed for '{search_query}': {e}", flush=True)
                    if "429" in str(e) or "Too Many Requests" in str(e) or "Sign in" in str(e) or "bot" in str(e).lower():
                        raise Exception("YouTube blocked the server (Bot Detection/429 Too Many Requests). Please try again later or provide a direct SoundCloud link.")

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

                filter_cfg = AUDIO_FILTERS.get(self.current_filter, AUDIO_FILTERS["normal"])
                filter_args = filter_cfg.get("ffmpeg", "")

                if discord.opus.is_loaded():
                    ffmpeg_opts = "-vn"
                    if filter_args:
                        ffmpeg_opts += f" -af {filter_args}"
                    raw_source = discord.FFmpegPCMAudio(
                        stream_url,
                        executable=FFMPEG_EXE,
                        before_options=FFMPEG_BEFORE_OPTIONS,
                        options=ffmpeg_opts
                    )
                    source = discord.PCMVolumeTransformer(raw_source, volume=self.volume)
                    stream_mode = f"PCM+libopus [{filter_cfg['name']}]"
                else:
                    vol_filter = f"volume={self.volume}"
                    combined = f"{filter_args},{vol_filter}" if filter_args else vol_filter
                    source = discord.FFmpegOpusAudio(
                        stream_url,
                        executable=FFMPEG_EXE,
                        bitrate=self.bitrate_kbps,
                        before_options=FFMPEG_BEFORE_OPTIONS,
                        options=f"-af {combined}"
                    )
                    stream_mode = f"FFmpegOpus [{filter_cfg['name']}]"

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
                        err_embed = discord.Embed(
                            title="⚠️  Playback Error",
                            description=(
                                f"Couldn't stream **{self.current.title}**.\n"
                                f"`{type(e).__name__}: {e}`\n\n"
                                "> ⏭️ Skipping to the next track..."
                            ),
                            color=0xFEE75C,
                            timestamp=discord.utils.utcnow()
                        )
                        err_embed.set_footer(text="Meow • Playback Engine")
                        await self.text_channel.send(embed=err_embed)
                    except Exception:
                        pass
                self.bot.loop.create_task(self.play_next())



# -----------------------------------------------------------------------------
# Interactive Voice Controls View & DSP Equalizer
# -----------------------------------------------------------------------------

class FilterSelect(discord.ui.Select):
    """Dynamic DSP Equalizer select dropdown for real-time audio shaping."""

    def __init__(self, player: GuildMusicPlayer):
        self.player = player
        options = [
            discord.SelectOption(
                label=f["name"],
                value=k,
                emoji=f["emoji"],
                description=f["desc"][:50],
                default=(player.current_filter == k)
            )
            for k, f in AUDIO_FILTERS.items()
        ]
        super().__init__(
            placeholder="🎛️ Equalizer & DSP Audio Filters...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="music_filter_select",
            row=1
        )

    async def callback(self, interaction: discord.Interaction):
        chosen_val = self.values[0]
        self.player.current_filter = chosen_val
        chosen = AUDIO_FILTERS.get(chosen_val, AUDIO_FILTERS["normal"])

        vc = self.player.voice_client
        if vc and (vc.is_playing() or vc.is_paused()) and self.player.current and self.player.current.source_url:
            try:
                stream_url = self.player.current.source_url
                filter_args = chosen.get("ffmpeg", "")
                if discord.opus.is_loaded():
                    opts = "-vn"
                    if filter_args:
                        opts += f" -af {filter_args}"
                    raw = discord.FFmpegPCMAudio(stream_url, executable=FFMPEG_EXE, before_options=FFMPEG_BEFORE_OPTIONS, options=opts)
                    src = discord.PCMVolumeTransformer(raw, volume=self.player.volume)
                else:
                    vol_f = f"volume={self.player.volume}"
                    comb = f"{filter_args},{vol_f}" if filter_args else vol_f
                    src = discord.FFmpegOpusAudio(stream_url, executable=FFMPEG_EXE, bitrate=self.player.bitrate_kbps, before_options=FFMPEG_BEFORE_OPTIONS, options=f"-af {comb}")

                def _after(err):
                    asyncio.run_coroutine_threadsafe(self.player.play_next(err), self.player.bot.loop)

                vc.stop()
                vc.play(src, after=_after)
            except Exception as e:
                print(f"[MUSIC] Filter live update error: {e}", flush=True)

        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"{chosen['emoji']}  Filter Applied — {chosen['name']}",
                description=f"> {chosen['desc']}",
                color=0x9B59B6
            ),
            ephemeral=True
        )


class MusicControlsView(discord.ui.View):
    def __init__(self, player: GuildMusicPlayer):
        super().__init__(timeout=None)
        self.player = player
        self.add_item(FilterSelect(player))

    @discord.ui.button(label="Pause / Resume", style=discord.ButtonStyle.primary, emoji="⏯️", custom_id="music_pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message(
                embed=discord.Embed(description="❌ I'm not in a voice channel.", color=0xED4245),
                ephemeral=True
            )
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message(
                embed=discord.Embed(description="⏸️ Paused.", color=0x5865F2),
                ephemeral=True
            )
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message(
                embed=discord.Embed(description="▶️ Resumed.", color=0x57F287),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=discord.Embed(description="❌ Nothing is playing right now.", color=0xED4245),
                ephemeral=True
            )

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️", custom_id="music_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message(
                embed=discord.Embed(description="❌ I'm not in a voice channel.", color=0xED4245),
                ephemeral=True
            )
        if vc.is_playing() or vc.is_paused():
            skipped_title = self.player.current.title if self.player.current else "Current track"
            vc.stop()
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=f"⏭️ Skipped **{skipped_title}**.",
                    color=0x5865F2
                ),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=discord.Embed(description="❌ Nothing to skip.", color=0xED4245),
                ephemeral=True
            )

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="music_queue")
    async def view_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.current and not self.player.queue:
            return await interaction.response.send_message(
                embed=discord.Embed(description="📭 The queue is empty! Use `/play` to add songs.", color=0x5865F2),
                ephemeral=True
            )

        embed = discord.Embed(title="📜  Music Queue", color=0x5865F2)
        if self.player.current:
            dur = format_duration(self.player.current.duration)
            web_url = self.player.current.webpage_url or ""
            embed.description = (
                f"**▶️ Now Playing**\n"
                f"[{self.player.current.title}]({web_url}) `{dur}`\n\n"
                f"**Up Next:**"
            )

        lines = []
        for i, t in enumerate(self.player.queue[:8], start=1):
            dur = format_duration(t.duration)
            lines.append(f"`{i}.` {t.title} `{dur}`")

        if not lines:
            embed.description += "\n*Queue is empty — use `/play` to add more!*"
        else:
            embed.description += "\n" + "\n".join(lines)
            if len(self.player.queue) > 8:
                embed.description += f"\n*+{len(self.player.queue) - 8} more tracks in queue*"

        embed.set_footer(text=f"Meow • {len(self.player.queue)} track(s) queued")
        await send_card(interaction, embed=embed, ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️", custom_id="music_stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message(
                embed=discord.Embed(description="❌ I'm not in a voice channel.", color=0xED4245),
                ephemeral=True
            )

        self.player.queue.clear()
        self.player.current = None
        self.player.manual_stop = True

        if self.player.is_247:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="⏹️ Stopped playback & cleared queue.\n> 💡 Staying in voice because **24/7 Mode** is on.",
                    color=0x5865F2
                ),
                ephemeral=True
            )
        else:
            await vc.disconnect()
            self.player.voice_client = None
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="⏹️ Stopped, cleared queue, and left the voice channel.",
                    color=0xED4245
                ),
                ephemeral=True
            )


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
            await interaction.edit_original_response(content="❌ You must join a voice channel first to use music!", embed=None, view=None)
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
                await interaction.edit_original_response(content=f"❌ Failed to connect to voice: `{e}`", embed=None, view=None)
                return None
        elif vc.channel.id != target_channel.id:
            try:
                await vc.move_to(target_channel)
            except Exception as e:
                await interaction.edit_original_response(content=f"❌ Failed to move to your voice channel: `{e}`", embed=None, view=None)
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
                title="👑  24/7 Mode — Enabled",
                description=(
                    f"> 🔊 **Channel:** {target_ch.mention}\n"
                    f"> 🛡️ **Set by:** {interaction.user.mention}\n"
                    f"> ⚡ **Auto-disconnect:** Disabled\n\n"
                    f"I'll stay here permanently through idle periods and restarts."
                ),
                color=0xF1C40F,
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text="Meow • 24/7 Voice Mode")
            await send_card(interaction, embed=embed)
        else:
            player.is_247 = False
            player.manual_stop = False
            await db.set_247_setting(interaction.guild.id, 0, False)

            vc = interaction.guild.voice_client
            if vc and not vc.is_playing() and not vc.is_paused():
                player.start_inactivity_timer()

            embed = discord.Embed(
                title="⏹️  24/7 Mode — Disabled",
                description=(
                    f"> ⏱️ **Inactivity timeout:** 5 minutes\n"
                    f"> 🛡️ **Set by:** {interaction.user.mention}\n\n"
                    f"I'll automatically leave after **5 min** of silence or an empty channel."
                ),
                color=0xED4245,
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text="Meow • 24/7 Voice Mode")
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
        await interaction.response.send_message("📻 Tuning into radio station...")
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
        embed.set_footer(text="Meow • Server Radio")
        view = MusicControlsView(player)
        await interaction.edit_original_response(content=None, embed=embed, view=view)

    # -------------------------------------------------------------------------
    # Full Voice Player: /play
    # -------------------------------------------------------------------------

    @app_commands.command(name="play", description="Play any song or stream from SoundCloud, YouTube, or audio link!")
    @app_commands.describe(query="Song title, artist name, or direct music link")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.send_message(f"🔍 Searching for `{query[:60]}`...")
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
                return await interaction.edit_original_response(
                    content="❌ Could not load Spotify metadata for that link. Please check that the track/playlist is public.",
                    embed=None, view=None
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
            # --- Direct URL path (YouTube link, SoundCloud link, audio URL) ---
            if search_target.startswith(("http://", "https://")):
                with yt_dlp.YoutubeDL(YTDL_STREAM_OPTIONS) as ytdl:
                    try:
                        info = ytdl.extract_info(search_target, download=False)
                        if info:
                            if "entries" in info and info["entries"]:
                                return info["entries"][0]
                            return info
                    except Exception as e:
                        print(f"[MUSIC] Direct URL extraction error: {e}", flush=True)
                        return None

            # --- Search path: 2-phase for speed + accuracy ---
            expected_sec = int(spotify_first.get("duration_ms", 0) / 1000) if spotify_first else 0

            def _sim(a: str, b: str) -> float:
                return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()

            def score(e):
                dur = e.get("duration") or 0
                title = (e.get("title") or "").lower()
                uploader = (e.get("uploader") or "").lower()
                channel = (e.get("channel") or "").lower()
                
                target_words = search_target.lower().split()
                matches = sum(1 for w in target_words if w in title or w in uploader or w in channel)
                word_match_ratio = matches / len(target_words) if target_words else 0
                
                dur_score = abs(dur - expected_sec) if expected_sec > 0 else 0
                short_penalty = 3600 if (expected_sec > 60 and 0 < dur < 60) else 0
                
                bonus = 0
                if "official audio" in title or "topic" in channel or "topic" in uploader:
                    bonus += 15
                if "lyric" in title:
                    bonus += 5
                if "official video" in title or "music video" in title:
                    bonus -= 10
                
                sim_score = _sim(search_target, title) * 30 
                word_match_bonus = word_match_ratio * 40
                
                final_score = dur_score + short_penalty - sim_score - word_match_bonus - bonus
                return final_score

            # DO NOT fallback to SoundCloud unless explicitly requested to prevent 'Wrong Song' bug
            prefixes = ["scsearch5:"] if search_target.startswith("scsearch") else ["ytsearch5:"]
                
            for prefix in prefixes:
                search_query = f"{prefix}{search_target}"
                try:
                    # Phase 1: fast flat metadata fetch — no stream resolution (~2s)
                    with yt_dlp.YoutubeDL(YTDL_FLAT_OPTIONS) as ytdl:
                        flat_info = ytdl.extract_info(search_query, download=False)
                    if not flat_info:
                        continue
                    entries = [e for e in (flat_info.get("entries") or []) if e]
                    if not entries:
                        continue

                    best_meta = sorted(entries, key=score)[0]
                    candidate_url = best_meta.get("url") or best_meta.get("webpage_url")
                    if not candidate_url:
                        continue

                    print(
                        f"[MUSIC] Phase-1 pick: '{best_meta.get('title')}' "
                        f"dur={best_meta.get('duration')}s sim={_sim(search_target, best_meta.get('title','')):.2f} "
                        f"for query='{search_target}'",
                        flush=True
                    )

                    # Phase 2: extract stream URL for just the winner (~3s)
                    with yt_dlp.YoutubeDL(YTDL_STREAM_OPTIONS) as ytdl:
                        stream_info = ytdl.extract_info(candidate_url, download=False)
                    if not stream_info:
                        continue
                    if "entries" in stream_info and stream_info["entries"]:
                        stream_info = stream_info["entries"][0]
                    if stream_info and stream_info.get("url"):
                        return stream_info
                except Exception as e:
                    print(f"[MUSIC] 2-phase search error for '{search_query}': {e}", flush=True)
                    if "429" in str(e) or "Too Many Requests" in str(e) or "Sign in" in str(e) or "bot" in str(e).lower():
                        raise Exception("YouTube blocked the server (Bot Detection/429 Too Many Requests). Please try again later or provide a direct SoundCloud link.")

            return None


        try:
            data = await loop.run_in_executor(None, extract)
        except Exception as e:
            return await interaction.edit_original_response(
                content=f"❌ **Playback Failed:** {e}", embed=None, view=None
            )
            
        if not data or not data.get("url"):
            return await interaction.edit_original_response(
                content="❌ Could not find or stream that track. Try a different title or direct link!", embed=None, view=None
            )

        actual_title = data.get("title") or ""
        track_title = (f"{spotify_first['title']} - {spotify_first['artist']}" if spotify_first else actual_title or query)[:90]
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
            webpage_url=webpage_url,
            actual_title=actual_title if spotify_first and actual_title else None
        )

        is_currently_playing = vc.is_playing() or vc.is_paused()
        player.queue.append(track)

        if not is_currently_playing:
            await player.play_next()
            
            # Extract artist and title for the card
            t_parts = track.title.split(" - ", 1)
            if len(t_parts) == 2:
                artist, title_only = t_parts[0], t_parts[1]
            else:
                artist, title_only = "Unknown Artist", track.title

            card_buf = await generate_now_playing_card(
                title=title_only,
                artist=artist,
                duration_str=format_duration(track.duration),
                requester=interaction.user.display_name,
                thumbnail_url=track.thumbnail or ""
            )
            
            embed = discord.Embed(color=0x1DB954 if spotify_first else 0x5865F2)
            embed.set_image(url="attachment://now_playing.png")
            if track.actual_title:
                embed.add_field(name="🔍 Audio Match", value=f"[{track.actual_title[:45]}]({track.webpage_url})", inline=True)
            if spotify_queued_count > 0:
                embed.add_field(name="💿 Playlist", value=f"+{spotify_queued_count} more queued", inline=True)
                
            view = MusicControlsView(player)
            await interaction.edit_original_response(
                content=None, 
                embed=embed, 
                view=view,
                attachments=[discord.File(card_buf, "now_playing.png")]
            )
        else:
            q_pos = len(player.queue)
            t_parts = track.title.split(" - ", 1)
            title_only = t_parts[1] if len(t_parts) == 2 else track.title
            
            embed = discord.Embed(color=0x1DB954 if spotify_first else 0x57F287)
            embed.set_author(name="Track Added to Queue")
            
            desc = (
                f"**Track:** [{title_only}]({track.webpage_url})\n"
                f"**Requested by:** {interaction.user.mention}\n"
                f"**Duration:** {format_duration(track.duration)}\n"
                f"**Position:** #{q_pos}"
            )
            if track.actual_title:
                desc += f"\n**Audio Match:** `{track.actual_title[:35]}`"
            
            embed.description = desc
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
                
            await interaction.edit_original_response(content=None, embed=embed, view=None)


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
            await interaction.response.send_message(
                embed=discord.Embed(description="⏸️ Paused.", color=0x5865F2), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=discord.Embed(description="❌ Nothing is currently playing.", color=0xED4245), ephemeral=True
            )

    @app_commands.command(name="resume", description="Resume playback if paused.")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message(
                embed=discord.Embed(description="❌ I'm not in a voice channel.", color=0xED4245), ephemeral=True
            )
        if vc.is_paused():
            vc.resume()
            await interaction.response.send_message(
                embed=discord.Embed(description="▶️ Resumed.", color=0x57F287), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=discord.Embed(description="❌ Nothing is paused.", color=0xED4245), ephemeral=True
            )

    @app_commands.command(name="skip", description="Skip to the next song in queue.")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message(
                embed=discord.Embed(description="❌ I'm not in a voice channel.", color=0xED4245), ephemeral=True
            )

        player = self.get_player(interaction.guild.id)
        if vc.is_playing() or vc.is_paused():
            skipped = player.current.title if player.current else "Current song"
            vc.stop()
            await interaction.response.send_message(
                embed=discord.Embed(description=f"⏭️ Skipped **{skipped}**.", color=0x5865F2), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=discord.Embed(description="❌ Nothing to skip.", color=0xED4245), ephemeral=True
            )

    @app_commands.command(name="stop", description="Stop music, clear the queue, and disconnect from voice.")
    async def stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message(
                embed=discord.Embed(description="❌ I'm not in a voice channel.", color=0xED4245), ephemeral=True
            )

        player = self.get_player(interaction.guild.id)
        player.queue.clear()
        player.current = None
        player.manual_stop = True

        if player.is_247:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="⏹️ Stopped & cleared queue.\n> 💡 Staying in voice — **24/7 Mode** is on.",
                    color=0x5865F2
                )
            )
        else:
            await vc.disconnect()
            player.voice_client = None
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="⏹️ Stopped, cleared queue, and left the voice channel.",
                    color=0xED4245
                )
            )

    @app_commands.command(name="queue", description="Display the current songs in the queue.")
    async def queue_cmd(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        if not player.current and not player.queue:
            return await interaction.response.send_message(
                embed=discord.Embed(description="📭 Queue is empty! Use `/play` or `/radio` to start.", color=0x5865F2),
                ephemeral=True
            )

        embed = discord.Embed(
            title=f"📜  {interaction.guild.name} — Queue",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        if player.current:
            dur = format_duration(player.current.duration)
            web = player.current.webpage_url or ""
            embed.description = (
                f"**▶️ Now Playing**\n"
                f"[{player.current.title}]({web}) `{dur}`\n\n"
                f"**Up Next:**"
            )

        lines = []
        for i, t in enumerate(player.queue[:10], start=1):
            dur = format_duration(t.duration)
            lines.append(f"`{i}.` {t.title} `{dur}` — *{t.requester}*")

        if not lines:
            embed.description += "\n*Queue is clear — use `/play <song>` to add more.*"
        else:
            embed.description += "\n" + "\n".join(lines)
            if len(player.queue) > 10:
                embed.description += f"\n*+{len(player.queue) - 10} more tracks*"

        mode = "ON 👑" if player.is_247 else "OFF"
        embed.set_footer(text=f"Meow • {len(player.queue) + (1 if player.current else 0)} track(s) • 24/7: {mode}")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="nowplaying", description="Show full info and controls for the currently playing track.")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        if not player.current:
            return await interaction.response.send_message(
                embed=discord.Embed(description="❌ Nothing is playing! Use `/play` or `/radio` to start.", color=0xED4245),
                ephemeral=True
            )

        await interaction.response.defer()
        cur = player.current
        filter_cfg = AUDIO_FILTERS.get(player.current_filter, AUDIO_FILTERS["normal"])
        
        # Extract artist and title for the card
        t_parts = cur.title.split(" - ", 1)
        if len(t_parts) == 2:
            artist, title_only = t_parts[0], t_parts[1]
        else:
            artist, title_only = "Unknown Artist", cur.title

        card_buf = await generate_now_playing_card(
            title=title_only,
            artist=artist,
            duration_str=format_duration(cur.duration),
            requester=cur.requester,
            thumbnail_url=cur.thumbnail or ""
        )

        embed = discord.Embed(color=0x5865F2)
        embed.set_image(url="attachment://now_playing.png")
        
        if cur.actual_title:
            embed.add_field(name="🔍 Audio Match", value=f"[{cur.actual_title[:45]}]({cur.webpage_url})", inline=False)
            
        embed.add_field(name="🔊 Volume", value=f"`{int(player.volume * 100)}%`", inline=True)
        embed.add_field(name=f"{filter_cfg['emoji']} Filter", value=filter_cfg["name"], inline=True)
        embed.add_field(name="👑 24/7 Mode", value="`ON`" if player.is_247 else "`OFF`", inline=True)
        
        view = MusicControlsView(player)
        await interaction.followup.send(
            embed=embed, 
            view=view,
            file=discord.File(card_buf, "now_playing.png")
        )

    @app_commands.command(name="volume", description="Set playback volume (1% to 150%).")
    @app_commands.describe(percent="Volume percentage between 1 and 150")
    async def volume(self, interaction: discord.Interaction, percent: int):
        if percent < 1 or percent > 150:
            return await interaction.response.send_message(
                embed=discord.Embed(description="❌ Volume must be between `1%` and `150%`.", color=0xED4245),
                ephemeral=True
            )

        player = self.get_player(interaction.guild.id)
        player.volume = percent / 100.0
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"🔊 Volume set to **{percent}%**.",
                color=0x57F287
            ),
            ephemeral=True
        )

    @app_commands.command(name="filter", description="Apply real-time audio filters (Bassboost, Nightcore, 8D, Vaporwave, etc.)")
    @app_commands.describe(effect="Audio effect preset to apply")
    @app_commands.choices(effect=[
        app_commands.Choice(name="Normal (Clean Studio) 🎵", value="normal"),
        app_commands.Choice(name="Bass Boost 🔊", value="bassboost"),
        app_commands.Choice(name="Nightcore 🌙", value="nightcore"),
        app_commands.Choice(name="Vaporwave 🌊", value="vaporwave"),
        app_commands.Choice(name="8D Spatial Audio 🎧", value="8d"),
        app_commands.Choice(name="Pop / Vocal Boost 🎤", value="pop"),
    ])
    async def filter_cmd(self, interaction: discord.Interaction, effect: str):
        if not interaction.guild:
            return
        player = self.get_player(interaction.guild.id)
        if not player.voice_client or not player.voice_client.is_connected():
            return await interaction.response.send_message(
                embed=discord.Embed(description="❌ I'm not in a voice channel.", color=0xED4245), ephemeral=True
            )

        chosen = AUDIO_FILTERS.get(effect, AUDIO_FILTERS["normal"])
        player.current_filter = effect

        embed = discord.Embed(
            title=f"{chosen['emoji']}  Filter Applied — {chosen['name']}",
            description=f"> {chosen['desc']}",
            color=0x9B59B6,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Server Equalizer • Use /filter effect:Normal to reset")

        # Live re-apply if currently playing
        vc = player.voice_client
        if (vc.is_playing() or vc.is_paused()) and player.current and player.current.source_url:
            try:
                stream_url = player.current.source_url
                filter_args = chosen.get("ffmpeg", "")
                if discord.opus.is_loaded():
                    opts = "-vn"
                    if filter_args:
                        opts += f" -af {filter_args}"
                    raw = discord.FFmpegPCMAudio(stream_url, executable=FFMPEG_EXE, before_options=FFMPEG_BEFORE_OPTIONS, options=opts)
                    src = discord.PCMVolumeTransformer(raw, volume=player.volume)
                else:
                    vol_f = f"volume={player.volume}"
                    comb = f"{filter_args},{vol_f}" if filter_args else vol_f
                    src = discord.FFmpegOpusAudio(stream_url, executable=FFMPEG_EXE, bitrate=player.bitrate_kbps, before_options=FFMPEG_BEFORE_OPTIONS, options=f"-af {comb}")

                def _after(err):
                    asyncio.run_coroutine_threadsafe(player.play_next(err), self.bot.loop)

                vc.stop()
                vc.play(src, after=_after)
            except Exception as e:
                print(f"[MUSIC] Filter live update error: {e}", flush=True)

        await interaction.response.send_message(embed=embed)

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
