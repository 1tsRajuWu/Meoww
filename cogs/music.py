import math
import os
import re
import shutil
import time
import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import aiohttp
import yt_dlp
import imageio_ffmpeg
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
}

# Stream options: used ONLY for Phase-2 single-video stream URL extraction.
#
# The old hardcoded ``player_client: ["android", "web"]`` was the single biggest
# quality bug in this cog: that client set exposes *only* YouTube's muxed 360p MP4
# (format 18 — low-bitrate AAC inside a video container), so ``bestaudio/best``
# fell back to a video stream and every song was demuxed and re-encoded from it.
# yt-dlp's own defaults negotiate the audio-only Opus format (251, ~130 kbps),
# which is exactly what Discord voice wants. Preferred explicitly so a future
# format reshuffle can't quietly pick the m4a variant.
YTDL_STREAM_OPTIONS = {
    "format": "bestaudio[ext=webm]/bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
}

# A freshly-resolved googlevideo URL stays valid for hours, so re-searching on
# every track start was pure waste — and worse, the second search could land on a
# different upload than the one the user was shown. Reuse until this many seconds.
STREAM_URL_TTL = 1800

# Words that mean "this is not the recording the user asked for". These are
# disqualifying rather than a mild nudge: a spot-on duration match must never let
# a slowed/reverbed or karaoke upload outrank the actual song.
_JUNK_MARKERS = (
    "cover", "karaoke", "instrumental", "remix", "mashup", "nightcore",
    "sped up", "speed up", "slowed", "reverbed", "reverb", "chopped", "screwed",
    "8d audio", "bass boost", "bass boosted", "reaction", "tribute",
    "made popular by", "in the style of", "ai cover", "live cover",
)
_JUNK_PENALTY = 300.0


def _junk_penalty(title_lower: str, target_lower: str) -> float:
    """Penalty for markers meaning "this is a different recording".

    Matching is word-bounded (“discover” is not a “cover”) and a marker the user
    actually asked for is skipped, so searching for a remix still returns one.
    """
    total = 0.0
    for marker in _JUNK_MARKERS:
        if marker in target_lower:
            continue
        if re.search(rf"\b{re.escape(marker)}\b", title_lower):
            total += _JUNK_PENALTY
    return total


def ytdl_extract(options: dict, target: str):
    """Run a yt-dlp extraction, preferring the high-quality default clients.

    If YouTube rejects the defaults (bot checks vary by network), retry once with
    the legacy client set. That still yields the old low-quality stream, which
    beats failing outright.
    """
    try:
        with yt_dlp.YoutubeDL(options) as ytdl:
            return ytdl.extract_info(target, download=False)
    except Exception as e:
        if options.get("extractor_args"):
            raise
        print(f"[MUSIC] Default extractor failed ({type(e).__name__}); retrying with legacy clients...", flush=True)
        legacy = dict(options)
        legacy["extractor_args"] = {"youtube": {"player_client": ["android", "web"]}}
        with yt_dlp.YoutubeDL(legacy) as ytdl:
            return ytdl.extract_info(target, download=False)


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


# Spotify joins collaborators ("Daft Punk, Pharrell Williams") and marks featured
# artists inline ("... feat. Nile Rodgers"). The upload lives on the primary
# artist's channel, so everything from the first separator on is dropped.
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|;|\bfeat\.?\b|\bft\.?\b|\bwith\b|\bx\b)\s*", re.IGNORECASE)


def _clean_artist(artist: str | None) -> str | None:
    """Reduce an artist string to the primary name, dropping placeholders."""
    artist = (artist or "").strip()
    if not artist:
        return None
    primary = _ARTIST_SPLIT.split(artist, 1)[0].strip()
    if not primary or primary.lower() in ("unknown", "unknown artist", "various artists", "va"):
        return None
    return primary


def _artist_from_query(query: str) -> str | None:
    """Best-effort artist from an ``Artist - Title`` query.

    The Spotify resolver already builds its search string this way, so plain text
    queries shaped like that get the same treatment.
    """
    if not query or query.startswith(("http://", "https://")) or " - " not in query:
        return None
    head = query.split(" - ", 1)[0].strip()
    return _clean_artist(head if len(head) <= 60 else None)


def _artist_strength(entry: dict, artist: str | None) -> int:
    """How confidently this upload belongs to the artist: 0 none, 1 likely, 2 own.

    A channel that *is* the artist (or their VEVO/Topic feed, or a verified channel
    carrying their name) counts as 2. A channel that merely mentions the name — the
    fan re-upload case — counts as 1, so it can't outrank a real one.
    """
    artist = _clean_artist(artist)
    if not artist:
        return 0
    tokens = [w for w in re.split(r"[^\w]+", artist.lower()) if len(w) > 1]
    if not tokens:
        return 0
    joined = "".join(tokens)
    channel = (entry.get("channel") or "").lower()
    channel_flat = channel.replace(" ", "")
    who = f"{channel} {entry.get('uploader') or ''}".lower()

    if channel_flat in (joined, f"{joined}vevo", f"{joined}topic", f"{joined}-topic"):
        return 2
    if entry.get("channel_is_verified") and all(t in who for t in tokens):
        return 2
    if joined in who.replace(" ", ""):
        return 1
    return 0


def _score_candidate(entry: dict, search_target: str, expected_sec: int) -> float:
    """Lower is better. Rewards a real title match and an official-looking
    upload, penalises covers/remixes and lengths that can't be the track."""
    dur = entry.get("duration") or 0
    title = entry.get("title") or ""
    lowered = title.lower()
    who = f"{entry.get('channel') or ''} {entry.get('uploader') or ''}".lower()
    blob = f"{lowered} {who}"

    sim = _similarity(search_target, title)
    tokens = [w for w in re.split(r"[^\w]+", search_target.lower()) if len(w) > 2]
    covered = sum(1 for w in tokens if w in blob) / len(tokens) if tokens else 0.0

    official = 0.0
    if "official audio" in lowered:
        official += 25
    if "topic" in who:
        official += 30
    if "provided to youtube" in lowered:
        official += 15
    if "lyric" in lowered:
        official += 8
    target_lower = search_target.lower()
    junk = _junk_penalty(lowered, target_lower)
    if "live" not in target_lower and re.search(r"\blive\b", lowered):
        junk += _JUNK_PENALTY

    # Popularity and verification: the canonical upload of a song is almost always
    # the verified artist channel with the largest audience, which is exactly how a
    # re-upload farm gets ruled out when the titles look alike.
    audience = 0.0
    if entry.get("channel_is_verified"):
        audience -= 20
    views = entry.get("view_count") or 0
    if views > 0:
        audience -= min(math.log10(views + 1) * 6, 54)

    # Duration is a tiebreaker, not the deciding vote: an uncapped seconds-delta
    # used to let any re-upload that happened to match the Spotify album length
    # outrank the artist's own (shorter) radio edit.
    dur_penalty = 0.0
    if expected_sec > 0 and dur > 0:
        delta = abs(dur - expected_sec)
        tolerance = max(8, expected_sec * 0.05)
        if delta > tolerance:
            dur_penalty = min(delta - tolerance, 60)
        if delta > 45:
            dur_penalty += 40
    if 0 < dur < 45:
        dur_penalty += 200

    return -(sim * 70) - (covered * 60) - official + audience + dur_penalty + junk


def _best_match(entries: list, search_target: str, expected_sec: int = 0, artist: str | None = None):
    """Pick the result that is actually the requested song.

    Scoring is additive rather than a hard filter on the artist's own channel: a
    filter is all-or-nothing, so when the only "artist" upload was a slowed remix
    it would happily return that.
    """
    candidates = [e for e in entries if e]
    if not candidates:
        return None
    artist = _clean_artist(artist)

    def total(entry: dict) -> float:
        score = _score_candidate(entry, search_target, expected_sec)
        strength = _artist_strength(entry, artist)
        if strength == 2:
            score -= 120
        elif strength == 1:
            score -= 40
        return score

    return min(candidates, key=total)


def _is_bot_block(err: Exception) -> bool:
    """Recognise YouTube's anti-bot responses without false-positiving on words
    like "bot" inside an unrelated error (e.g. "bootstrap")."""
    msg = str(err).lower()
    return any(s in msg for s in (
        "429", "too many requests", "sign in to confirm", "not a bot",
        "confirm you're not a bot", "confirm you\u2019re not a bot",
    ))


def _apply_stream_meta(track: "Track", info: dict) -> None:
    """Record the codec/bitrate/freshness of a resolved stream on the track."""
    codec = (info.get("acodec") or "").lower()
    ext = (info.get("ext") or "").lower()
    track.source_is_opus = codec == "opus" or (ext == "webm" and codec != "mp4a")
    try:
        track.source_abr = float(info.get("abr") or 0)
    except (TypeError, ValueError):
        track.source_abr = 0.0
    track.resolved_at = time.monotonic()

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
        actual_title: str = None,
        search_query: str = None,
        expected_artist: str = None,
        source_is_opus: bool = False,
        source_abr: float = 0.0,
        resolved_at: float = 0.0
    ):
        self.title = title
        self.source_url = source_url
        self.duration = duration
        self.requester = requester
        self.thumbnail = thumbnail
        self.webpage_url = webpage_url
        self.is_radio = is_radio
        self.actual_title = actual_title
        # The exact text this track was found with, reused verbatim if the stream
        # URL has to be resolved again (re-deriving it from ``title`` used to
        # search "Title - Artist" and could resolve to a different video).
        self.search_query = search_query
        self.expected_artist = expected_artist
        # Codec/bitrate of the resolved source, so the player knows whether it can
        # pass Opus through untouched or must re-encode.
        self.source_is_opus = source_is_opus
        self.source_abr = source_abr
        self.resolved_at = resolved_at


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
        # The source currently handed to the voice client (kept so a volume change
        # can update it live) and a generation counter, so the audio thread's
        # after-callback can tell "the track finished" apart from "we replaced it".
        self.source: discord.AudioSource | None = None
        self._generation: int = 0
        # Human-readable name of the branch ``_build_source`` produced, for logs.
        self._engine: str = "FFmpegOpus"

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
            # Reuse the query this track was originally found with. Deriving it from
            # ``track.title`` reversed it to "Title - Artist", a different search that
            # could resolve to a completely different upload than the one shown.
            search_term = track.search_query or track.title
            artist = track.expected_artist or _artist_from_query(search_term)

            def remember(stream_info) -> str | None:
                url = stream_info.get("url")
                if not url:
                    return None
                if stream_info.get("webpage_url"):
                    track.webpage_url = stream_info["webpage_url"]
                _apply_stream_meta(track, stream_info)
                return url

            # 1. Direct URL path (YouTube / SoundCloud / CDN link)
            if target and target.startswith(("http://", "https://")):
                if not ("spotify.com" in target or "spotify.link" in target):
                    try:
                        info = ytdl_extract(YTDL_STREAM_OPTIONS, target)
                        if info:
                            if "entries" in info and info["entries"]:
                                info = info["entries"][0]
                            url = remember(info)
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
                    flat_info = ytdl_extract(YTDL_FLAT_OPTIONS, search_query)
                    if not flat_info:
                        continue
                    entries = [e for e in (flat_info.get("entries") or []) if e]
                    if not entries:
                        continue

                    best_meta = _best_match(entries, search_term, expected_sec, artist)
                    best_url = best_meta.get("url") or best_meta.get("webpage_url")
                    if not best_url:
                        continue

                    print(
                        f"[MUSIC] Phase-1 winner: '{best_meta.get('title')}' "
                        f"dur={best_meta.get('duration')}s for '{search_term}'",
                        flush=True
                    )

                    # Phase 2: stream URL extraction for the single winner
                    stream_info = ytdl_extract(YTDL_STREAM_OPTIONS, best_url)
                    if not stream_info:
                        continue
                    if "entries" in stream_info and stream_info["entries"]:
                        stream_info = stream_info["entries"][0]
                    url = remember(stream_info)
                    if url:
                        return url
                except Exception as e:
                    print(f"[MUSIC] 2-phase search failed for '{search_query}': {e}", flush=True)
                    if _is_bot_block(e):
                        raise Exception("YouTube blocked the server (Bot Detection/429 Too Many Requests). Please try again later or provide a direct SoundCloud link.")

            return None

        return await loop.run_in_executor(None, extract)


    def _build_source(self, stream_url: str) -> discord.AudioSource:
        """Build the ffmpeg source for the current filter + volume settings.

        This was copy-pasted in three places (the player, `/filter` and the filter
        dropdown) and all three carried the same bug: they called
        ``voice_client.stop()`` and then played the rebuilt source, so the stop's
        after-callback *also* advanced the queue and threw away a queued track.
        """
        filter_cfg = AUDIO_FILTERS.get(self.current_filter, AUDIO_FILTERS["normal"])
        filter_args = filter_cfg.get("ffmpeg", "")

        # Nothing to reshape, the source is already Opus, and it fits under the
        # channel's bitrate: pass the packets through untouched. That keeps YouTube's
        # full ~130 kbps audio instead of re-encoding it down to the channel rate,
        # and skips an entire encode pass.
        current = self.current
        if (not filter_args
                and abs(self.volume - 1.0) < 1e-6
                and current is not None
                and current.source_is_opus
                and 0 < current.source_abr <= self.bitrate_kbps + 8):
            self._engine = f"Opus+stream-copy ({current.source_abr:.0f}kbps source)"
            return discord.FFmpegOpusAudio(
                stream_url,
                executable=FFMPEG_EXE,
                codec="copy",
                before_options=FFMPEG_BEFORE_OPTIONS
            )

        if discord.opus.is_loaded():
            self._engine = "PCM+libopus"
            ffmpeg_opts = "-vn"
            if filter_args:
                ffmpeg_opts += f" -af {filter_args}"
            raw_source = discord.FFmpegPCMAudio(
                stream_url,
                executable=FFMPEG_EXE,
                before_options=FFMPEG_BEFORE_OPTIONS,
                options=ffmpeg_opts
            )
            # PCM goes through a volume transformer, so volume can change live.
            return discord.PCMVolumeTransformer(raw_source, volume=self.volume)

        # Without libopus the volume has to be baked into the ffmpeg command line,
        # which is why changing it later restarts the stream.
        vol_filter = f"volume={self.volume}"
        combined = f"{filter_args},{vol_filter}" if filter_args else vol_filter
        self._engine = f"FFmpegOpus re-encode [{filter_cfg['name']}]"
        return discord.FFmpegOpusAudio(
            stream_url,
            executable=FFMPEG_EXE,
            bitrate=self.bitrate_kbps,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=f"-af {combined}"
        )

    def _after_for(self, generation: int):
        """After-callback owned by one source, so stale callbacks are ignored."""
        def _after(err):
            if generation != self._generation:
                # This source was replaced on purpose (filter/volume restart, a
                # skip, or a new track). A newer source owns playback now, so this
                # signal is not a track ending and must not advance the queue.
                return
            if err:
                print(f"[MUSIC] AudioPlayer thread error in guild {self.guild_id}: {err}", flush=True)
            asyncio.run_coroutine_threadsafe(self.play_next(err), self.bot.loop)
        return _after

    def _start_source(self, source: discord.AudioSource) -> None:
        self.voice_client.play(source, after=self._after_for(self._generation))
        self.source = source

    def restart_current(self) -> bool:
        """Re-render the current track in place for a filter or volume change.

        Returns False when nothing needed restarting (no track, no stream URL, or
        the player is idle). ``vc.stop()`` fires the old source's after-callback,
        which is why the generation is bumped first: otherwise the callback would
        treat the deliberate stop as a finished song and skip ahead.
        """
        vc = self.voice_client
        if not vc or not vc.is_connected():
            return False
        if not (vc.is_playing() or vc.is_paused()):
            return False
        if not self.current or not self.current.source_url:
            return False
        try:
            source = self._build_source(self.current.source_url)
        except Exception as e:
            print(f"[MUSIC] Could not rebuild the audio source: {e}", flush=True)
            return False
        self._generation += 1
        try:
            vc.stop()
        except Exception as e:
            print(f"[MUSIC] Could not stop the old source: {e}", flush=True)
            return False
        self._start_source(source)
        return True

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

            # Re-resolve stream URLs only when they are missing or stale: googlevideo
            # links stay valid for hours, so re-searching a freshly extracted one was
            # pure waste — and the second search could resolve to a different upload
            # than the one the user was shown. Expired links still get refreshed here.
            stream_url = self.current.source_url
            freshly_resolved = (time.monotonic() - self.current.resolved_at) < STREAM_URL_TTL
            needs_resolve = (
                not stream_url
                or "manifest.m3u8" in stream_url
                or "soundcloud.cloud" in stream_url
                or ("googlevideo.com" in stream_url and not freshly_resolved)
            )
            if not self.current.is_radio and needs_resolve:
                try:
                    resolved = await self._resolve_stream_url(self.current)
                except Exception as e:
                    # The extractor raises on bot-checks/429. This runs from the
                    # audio thread's after-callback, so an escaping error used to
                    # stall playback with only an asyncio log line.
                    print(f"[MUSIC] Could not resolve '{self.current.title}': {e}", flush=True)
                    resolved = None
                if resolved:
                    stream_url = resolved
                    self.current.source_url = resolved

            if not stream_url:
                print(f"[MUSIC] No playable stream for '{self.current.title}'; skipping it.", flush=True)
                self.bot.loop.create_task(self.play_next())
                return

            try:
                ch_bitrate = getattr(self.voice_client.channel, "bitrate", 128000)
                self.bitrate_kbps = min(max(int(ch_bitrate / 1000), 64), 384)

                filter_name = AUDIO_FILTERS.get(self.current_filter, AUDIO_FILTERS["normal"])["name"]
                source = self._build_source(stream_url)
                # A new generation takes ownership of playback: whatever is playing
                # is being replaced by us, so its after-callback must not advance
                # the queue on top of this.
                self._generation += 1
                if self.voice_client.is_playing() or self.voice_client.is_paused():
                    self.voice_client.stop()

                self._start_source(source)
                engine = self._engine

                print(f"[MUSIC] 🎶 Started playing '{self.current.title}' via {engine} [{filter_name}] ({self.bitrate_kbps}kbps) in guild {self.guild_id}", flush=True)

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

        self.player.restart_current()

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
                    f"> 🛡️ **Set by:** {interaction.user.display_name}\n"
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
                    f"> 🛡️ **Set by:** {interaction.user.display_name}\n\n"
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

        radio_track = Track(
            title=st_info["name"],
            source_url=st_info["url"],
            duration=0,
            requester=interaction.user.display_name,
            is_radio=True
        )
        player.queue.append(radio_track)

        # Let the stop's after-callback do the advancing. Calling play_next() here
        # as well produced two advances: the second one found an empty queue, cleared
        # `current`, and then /nowplaying, /queue and /skip all denied that the radio
        # was playing while the stream was still going out.
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        else:
            await player.play_next()

        embed = discord.Embed(
            title=f"{st_info['icon']} 24/7 Radio Station Started",
            description=(
                f"### {st_info['name']}\n"
                f"{st_info['desc']}\n\n"
                f"> 🔊 **Voice Channel:** {vc.channel.mention}\n"
                f"> 👤 **Started By:** {interaction.user.display_name}\n"
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
                        webpage_url=st.get("query"),
                        # Same query the first track used, so playlist entries don't
                        # drift to "Title - Artist" (and a different video) later.
                        search_query=st.get("query"),
                        expected_artist=_clean_artist(st.get("artist"))
                    ))
                spotify_queued_count = len(spotify_tracks) - 1

        def extract():
            # --- Direct URL path (YouTube link, SoundCloud link, audio URL) ---
            if search_target.startswith(("http://", "https://")):
                try:
                    info = ytdl_extract(YTDL_STREAM_OPTIONS, search_target)
                except Exception as e:
                    print(f"[MUSIC] Direct URL extraction error: {e}", flush=True)
                    return None
                if not info:
                    return None
                if "entries" in info and info["entries"]:
                    return info["entries"][0]
                return info

            # --- Search path: 2-phase for speed + accuracy ---
            if spotify_first:
                expected_sec = int(spotify_first.get("duration_ms", 0) / 1000)
                artist = _clean_artist(spotify_first.get("artist"))
            else:
                expected_sec = 0
                artist = _artist_from_query(search_target)

            # DO NOT fallback to SoundCloud unless explicitly requested to prevent 'Wrong Song' bug
            prefixes = ["scsearch5:"] if search_target.startswith("scsearch") else ["ytsearch5:"]

            for prefix in prefixes:
                search_query = f"{prefix}{search_target}"
                try:
                    # Phase 1: fast flat metadata fetch — no stream resolution (~2s)
                    flat_info = ytdl_extract(YTDL_FLAT_OPTIONS, search_query)
                    if not flat_info:
                        continue
                    entries = [e for e in (flat_info.get("entries") or []) if e]
                    if not entries:
                        continue

                    best_meta = _best_match(entries, search_target, expected_sec, artist)
                    candidate_url = best_meta.get("url") or best_meta.get("webpage_url")
                    if not candidate_url:
                        continue

                    print(
                        f"[MUSIC] Phase-1 pick: '{best_meta.get('title')}' "
                        f"dur={best_meta.get('duration')}s "
                        f"sim={_similarity(search_target, best_meta.get('title', '')):.2f} "
                        f"for query='{search_target}'",
                        flush=True
                    )

                    # Phase 2: extract stream URL for just the winner (~3s)
                    stream_info = ytdl_extract(YTDL_STREAM_OPTIONS, candidate_url)
                    if not stream_info:
                        continue
                    if "entries" in stream_info and stream_info["entries"]:
                        stream_info = stream_info["entries"][0]
                    if stream_info and stream_info.get("url"):
                        return stream_info
                except Exception as e:
                    print(f"[MUSIC] 2-phase search error for '{search_query}': {e}", flush=True)
                    if _is_bot_block(e):
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
            actual_title=actual_title if spotify_first and actual_title else None,
            search_query=search_target,
            expected_artist=_clean_artist(spotify_first.get("artist")) if spotify_first else _artist_from_query(query)
        )
        # The URL was extracted moments ago, so the player must not re-search it.
        _apply_stream_meta(track, data)

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
                f"**Requested by:** {interaction.user.display_name}\n"
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

        # The PCM branch exposes a live volume transformer; the opus branch bakes
        # the volume into the ffmpeg command line, so the stream has to be rebuilt
        # for the change to be audible. Previously neither happened, so /volume
        # silently did nothing at all until the next track started.
        if isinstance(player.source, discord.PCMVolumeTransformer):
            player.source.volume = player.volume
            applied = True
        else:
            applied = player.restart_current()

        description = f"🔊 Volume set to **{percent}%**."
        if not applied:
            description += "\n> Saved — it will apply to the next track."
        await interaction.response.send_message(
            embed=discord.Embed(description=description, color=0x57F287),
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

        # Rebuild the stream in place so the change is audible immediately. This
        # used to stop the player and start the new source by hand, whose
        # after-callback then skipped a queued track.
        if not player.restart_current():
            embed.set_footer(text="🐾 Server Equalizer • Saved — applies to the next track")

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

        # Match by name, not by one server's channel ID: the hardcoded ID meant
        # this silently did nothing anywhere else (the same fault /updatedb had).
        channel_name = getattr(message.channel, "name", "") or ""
        if "fav-song" not in channel_name.lower():
            return

        # `message.content` is empty without the Message Content intent, so this
        # listener could never fire in practice. A posted link also arrives as an
        # embed (and its artwork as an attachment), which needs no intent.
        music_domains = ("spotify.com", "music.apple.com", "soundcloud.com", "youtube.com/watch", "youtu.be")
        links = [message.content or ""]
        links += [e.url or "" for e in message.embeds]
        links += [a.url or "" for a in message.attachments]
        if not any(dom in link for link in links for dom in music_domains):
            return

        for emoji in ("❤️", "🔥", "🎧", "✨"):
            try:
                await message.add_reaction(emoji)
            except Exception as e:
                print(f"[MUSIC] Could not react in {channel_name}: {e}", flush=True)
                return


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
