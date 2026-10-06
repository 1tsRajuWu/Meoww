"""The radio engine: one station, one socket, and everything that can stop it early.

Radio is the only playback this bot does, and this module is written for a *live
stream* rather than for a track. That single fact removes most of what the player
it replaces had to carry — no queue, no search, no filters, no "next track", no
declared duration to be judged against — and it makes the one thing that matters
the loudest:

**A station cannot end, so anything that ends it is a fault.** A closed socket is
a *clean* EOF to everything downstream: ffmpeg reports success, the after-callback
fires with no error, and a room listening to a station that just died hears
silence with nothing said. So the player judges what ended — a live source that
reports itself finished at all was cut — and then it *asks again* before it says
anything, because the ending is far more often the connection than the station.
Both stations this bot ever reported to a room are the same host, and a
75-second read of each from a working machine delivered every second of it. A
station that drops once and comes back is not news; a station that cannot hold a
socket is, and that is what the card is for.

Three decisions are worth reading before the code:

* **The generation counter.** Every source is tagged with the generation it was
  created in, and a callback only acts while its generation is still current. This
  is what makes "start a different station while one is playing" safe: the old
  source's callback fires on the way out, and without the counter it would be
  read as the new station dying.
* **The retry budget belongs to the station.** A source that played at least
  ``LIVE_RETRY_AFTER`` seconds was healthy, so a station that ends every five
  minutes plays all night — which is what a radio is. ``LIVE_RETRY_LIMIT``
  attempts inside that window is where "reconnect" stops being plausible and
  becomes news. A *different* station starts with a fresh budget; the same
  station asked again keeps the one it spent.
* **The ffmpeg command line is built in exactly one place**, and it is
  deliberately short. ``-reconnect*`` stays, because a stream that is merely
  interrupted should be retried rather than treated as over, and ``-nostdin``
  matters more than it looks: without it ffmpeg shares stdin with the bot process
  and a stray read there can stop playback for no visible reason.

The same two rules the old probe was built on carry over, because they were
learned rather than guessed: **a check must never raise** (this is called from a
weekly pass, from ``/health`` and from an administrator's command), and **absent
is not the same as fine** (a station that answers ``200`` with an HTML error page
is not a stream, so the content type is read before the answer is believed).
"""
from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from . import config_audit, voice_caps

if TYPE_CHECKING:  # pragma: no cover — the catalogue is data and imports nothing from here
    from .radio_catalogue import Station

#: Extra ffmpeg flags before ``-i``. ``-nostdin`` matters more than it looks:
#: without it ffmpeg shares stdin with the bot process, and a stray read there
#: can stop playback for no visible reason.
BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"

#: Leave a channel nobody is in after this long. Only ever armed when the server
#: has *not* asked the bot to stay: a radio that was told to hold a channel holds
#: it, and that is the whole of the difference.
ALONE_SECONDS = 120

#: A source that played at least this long was healthy, so the retry budget is
#: restored — see the module docstring.
LIVE_RETRY_AFTER = 60.0
#: Attempts *inside* that window before a cut becomes news.
LIVE_RETRY_LIMIT = 4
#: The gap between attempts, short because the room is listening to silence.
LIVE_RETRY_PAUSE = 1.5

#: What ``libopus`` is told whenever this process has to re-encode, and why each
#: part of it. ``discord.py`` writes ``-fec true -packet_loss 15`` into *every*
#: ``FFmpegOpusAudio`` command line — the right trade for a voice library and the
#: wrong one for a music player, because the in-band redundancy is carried by
#: libopus's **speech** path. Measured on this host against a musical signal at
#: 96 kbps, ``-application audio`` scored −43.83 dB where ``voip`` scored
#: −36.97 dB, and turning FEC off removed its 4.3 dB share outright.
#: ``-compression_level 10`` then buys a little more quality at the same bitrate
#: for CPU one stream never notices. These are appended *after* discord.py's own
#: flags on purpose: ffmpeg keeps the last occurrence of an option.
RADIO_ENCODER = (
    "-application audio -frame_duration 20 -vbr on -compression_level 10 "
    "-fec false -packet_loss 0"
)

#: The highest this will ever ask libopus for. Past this a music stream gains
#: nothing audible, and it is comfortably under Discord's own 384 kbps ceiling.
MAX_ENCODE_KBPS = 256

#: Every live player, by guild. Module-level because two cogs have to agree about
#: one socket: a Discord client sits in one channel at a time, so Voice Company
#: has to be able to ask "is the radio using this connection?" before it decides
#: to leave the room — otherwise a companion leaving takes the radio with it.
PLAYERS: dict[int, "RadioPlayer"] = {}


def holds_voice(guild_id) -> bool:
    """Whether the radio is using this guild's voice connection right now.

    True while a station is playing or paused, or while a 24/7 seat is being
    kept: the three reasons a disconnect here would be somebody's radio stopping.
    """
    try:
        key = int(guild_id or 0)
    except (TypeError, ValueError):
        return False
    player = PLAYERS.get(key)
    if player is None:
        return False
    if player.current is not None:
        return True
    if player.stay:
        return True
    client = player.voice_client
    try:
        return bool(
            client
            and client.is_connected()
            and (client.is_playing() or client.is_paused())
        )
    except Exception:  # noqa: BLE001 — a question about a socket must not raise
        return False


# ------------------------------------------------------------------ the probe #
#: How long to wait for a station to start talking. Long enough for a slow
#: icecast server on another continent, short enough that a check run from a
#: command does not look like a hang.
PROBE_TIMEOUT = 12.0

#: Enough of the body to prove something is actually being served. A stream never
#: ends, so this is a read and a close rather than a download.
PROBE_BYTES = 64

#: The check identifies itself, so a page of hits in a station's log is
#: recognisable rather than mysterious.
USER_AGENT = "MeowStreamWatch/1.0 (+checks that the bot's stations still play)"

#: What counts as audio, and deliberately the *same* tuple the weekly link scan
#: uses (``utils/media_health.MEDIA_PREFIXES``): two rules for one question would
#: eventually disagree, and the disagreement would show up as a station reported
#: healthy by one check and dead by the other.
AUDIO_PREFIXES: tuple[str, ...] = ("audio/", "application/ogg", "video/")

#: What the self-check opens, overridable with ``STREAM_PROBE_TARGET``. The
#: default is one station in this bot's own catalogue — SomaFM's Groove Salad, a
#: long-lived icecast stream that needs no account — because a probe is only
#: useful if it fails for *this host's* reasons rather than for its own: a
#: station that quietly goes off the air would report the deployment as broken.
DEFAULT_PROBE_TARGET = "https://ice1.somafm.com/groovesalad-128-mp3"


@dataclass(frozen=True)
class Probe:
    """One stream check, in the shape three surfaces read.

    ``unreachable`` is kept apart from the rest because it is the only failure
    that is not a fault in the *stream*: this host could not open the connection
    at all. The weekly pass reports those as "could not ask" rather than "dead",
    because only one of the two is something to go and fix.
    """

    healthy: bool
    reason: str = ""
    #: What answered, when it did — the content type and the host, which is the
    #: evidence behind a healthy reading rather than a bare yes.
    detail: str = ""
    target: str = ""
    unreachable: bool = False
    at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "reason": self.reason,
            "detail": self.detail,
            "target": self.target,
            "unreachable": self.unreachable,
        }

    def sentence(self) -> str:
        """One line for a card: what happened, and what was behind it."""
        if not self.healthy:
            return f"**failed** — {self.reason}"
        return f"working · {self.detail}" if self.detail else "working"


def probe_target() -> str:
    """The station the self-check opens, from the environment when it is set."""
    configured = (config_audit.get("STREAM_PROBE_TARGET") or "").strip()
    return configured or DEFAULT_PROBE_TARGET


def self_check(target: str | None = None) -> Probe:
    """Open one station, all the way to bytes, and say what happened.

    The question a version string cannot answer and a capability probe cannot
    either: ffmpeg can be installed, libopus loaded and every link in the tree
    healthy while this host has no route to the outside world at all. Radio is
    what plays here, so *a station this process can open* is the whole of what
    "radio works" means.

    Never raises, by design. The answer is either a stream this host can read
    bytes from, or a sentence naming what got in the way.
    """
    address = (target or probe_target()).strip() or DEFAULT_PROBE_TARGET
    request = urllib.request.Request(
        address,
        headers={
            "User-Agent": USER_AGENT,
            # A partial read is enough, and some icecast servers are happier
            # being told so than being hung up on mid-stream.
            "Range": f"bytes=0-{PROBE_BYTES - 1}",
        },
    )
    try:
        response = urllib.request.urlopen(request, timeout=PROBE_TIMEOUT)
    except urllib.error.HTTPError as exc:
        # An answer, and a bad one: the station is there and will not serve this
        # request. Not ``unreachable`` — that word is for this host's network.
        return Probe(
            healthy=False,
            reason=f"the station answered HTTP {exc.code}",
            target=address,
            at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001 — a check must not raise
        return Probe(
            healthy=False,
            reason=f"this host could not open the station ({type(exc).__name__})",
            target=address,
            unreachable=True,
            at=time.time(),
        )
    try:
        status = int(getattr(response, "status", 0) or 0)
        headers = getattr(response, "headers", None) or {}
        content_type = str(headers.get("Content-Type", "")).lower()
        try:
            sample = response.read(PROBE_BYTES)
        except Exception:  # noqa: BLE001 — an unreadable body is a finding
            sample = b""
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 — closing twice is not a finding
                pass

    if status >= 400 or status == 0:
        return Probe(
            healthy=False,
            reason=f"the station answered HTTP {status or 'no status'}",
            target=address,
            at=time.time(),
        )
    if not content_type.startswith(AUDIO_PREFIXES):
        return Probe(
            healthy=False,
            reason=f"it answered {content_type or 'no content type'}, which is not a stream",
            target=address,
            at=time.time(),
        )
    if not sample:
        return Probe(
            healthy=False,
            reason="it answered with audio headers and then no audio at all",
            target=address,
            at=time.time(),
        )
    host = address.split("//", 1)[-1].split("/", 1)[0]
    return Probe(
        healthy=True,
        detail=f"{content_type.split(';')[0]} from {host}",
        target=address,
        at=time.time(),
    )


# --------------------------------------------------------------- the encoding #
def channel_bitrate_kbps(vc) -> int:
    """What the channel this connection is in asks Discord's clients to encode at.

    Discord's default is 64 kbps, and it is the *channel's* number rather than a
    limit on what the bot may send — which is why :func:`encode_bitrate` treats
    it as a floor and not as the target.
    """
    raw = int(getattr(getattr(vc, "channel", None), "bitrate", 128000) or 128000)
    return min(max(raw // 1000, 64), 384)


def encode_bitrate(channel_kbps: int, station) -> int:
    """How fast to encode when the audio has to be re-encoded rather than passed on.

    The rule used to be *the channel's own bitrate*, and that is the number behind
    the report of echo and poor quality: Discord's default voice channel is **64
    kbps**, so a 160 kbps recording was decoded and re-encoded at 64 — more than
    half of it discarded before libopus had done anything else — and this is the
    path taken by *every* source that is not already Opus.

    Encoding above the source cannot bring back what is gone; encoding below it
    throws away what is not. So: the source's own rate, never under what the
    channel asks for, never past :data:`MAX_ENCODE_KBPS`.
    """
    source = int(getattr(station, "bitrate", 0) or 0)
    return min(max(int(channel_kbps or 0), source, 64), MAX_ENCODE_KBPS)


# ---------------------------------------------------------------- the fault #
@dataclass
class Fault:
    """Something that went wrong during playback, in the shape a card needs."""

    kind: str
    detail: str
    title: str = ""
    played: float = 0.0
    expected: float = 0.0
    engine: str = ""
    #: Filled in by the player that raised it, so whoever reports the fault knows
    #: which room it happened in without being handed it separately.
    guild_id: int = 0

    @property
    def headline(self) -> str:
        """The fault as one sentence, for a card title or a log line."""
        names = {
            "truncated": "a station was cut off",
            "source_error": "the audio engine failed",
            "unplayable": "a station could not be played",
            "opus_missing": "this host cannot encode audio",
        }
        return names.get(self.kind, self.kind.replace("_", " "))


class RadioPlayer:
    """One guild's radio: the voice client, the station, and the source.

    Deliberately holds no Discord *lookup* of its own: the cog puts the channel
    and the client in, and this object never reaches for state it was not given,
    which is what lets the whole thing be driven in a test without a gateway.
    """

    def __init__(self, bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.voice_client: discord.VoiceClient | None = None
        self.text_channel = None
        #: The station that is on air, and who asked for it.
        self.current: Station | None = None
        self.requester = ""
        # 100% by default, and that default is a *quality* decision rather than a
        # taste one: anything other than exactly 1.0 disqualifies the
        # pass-through path, so a member who turns the volume down chooses a
        # re-encode and nobody should get one by default.
        self.volume = 1.0
        #: Whether this connection is a 24/7 seat: stays through silence and
        #: through restarts until somebody stops it. The default is the server's
        #: stored answer and the cog sets it before the first start.
        self.stay = False
        self.manual_stop = False
        self.source: discord.AudioSource | None = None
        self.engine = ""

        self._generation = 0
        self._play_lock = asyncio.Lock()
        #: Attempts spent on the station playing now — see ``LIVE_RETRY_LIMIT``.
        self._live_retries = 0
        self._started_at = 0.0
        PLAYERS[self.guild_id] = self
        self.alone_task: asyncio.Task | None = None

        #: Where a cut or a source error is reported. The cog sets this to a
        #: coroutine (operator log + a card in the channel); leaving it unset is
        #: a supported state, and it is what keeps a fault from raising inside
        #: the audio thread, where nothing would see it.
        self.on_fault = None

    # ----------------------------------------------------------------- timers #
    def cancel_alone_timer(self) -> None:
        if self.alone_task and not self.alone_task.done():
            self.alone_task.cancel()
        self.alone_task = None

    def start_alone_timer(self) -> None:
        """Leave, eventually, when the last person has gone.

        A timer rather than an immediate exit because somebody stepping out to
        fetch a drink is not the end of a listening session — the same reasoning
        the Voice Company module uses for its 12-second grace, at a scale that
        fits a radio. A 24/7 seat never arms it.
        """
        if self.stay:
            return
        self.cancel_alone_timer()
        try:
            self.alone_task = self.bot.loop.create_task(self._alone_timeout())
        except Exception:  # noqa: BLE001 — a timer that will not start is not a fault
            self.alone_task = None

    async def _alone_timeout(self) -> None:
        await asyncio.sleep(ALONE_SECONDS)
        if self.stay:
            return
        channel = getattr(self.voice_client, "channel", None)
        if channel is None or not getattr(self.voice_client, "is_connected", lambda: False)():
            return
        humans = [
            member
            for member in getattr(channel, "members", ()) or ()
            if not getattr(member, "bot", False)
        ]
        if not humans:
            await self.leave(reason="empty")

    # -------------------------------------------------------------- playback #
    def _after_for(self, generation: int):
        """An after-callback owned by one generation of the source.

        This is the whole of the "a deliberate stop is not a station dying" fix,
        and it is also where a cut is told apart from anything else.
        """

        def _after(error):
            if generation != self._generation:
                # A newer source owns playback: the stop that fired this callback
                # was ours, so this signal is not a station ending.
                return
            played = max(0.0, time.monotonic() - self._started_at)
            if error:
                self._report(
                    Fault(
                        kind="source_error",
                        detail=f"the audio engine reported `{type(error).__name__}: {error}`",
                        title=self._title(),
                        played=played,
                        engine=self.engine,
                    )
                )
            elif self._worth_another_attempt(played):
                asyncio.run_coroutine_threadsafe(self._reconnect(played), self.bot.loop)
                return
            elif self.current is not None:
                # No error and no retry left: a live stream reporting itself
                # finished *is* the cut, and it is the case a room used to hear
                # nothing about.
                self._report(
                    Fault(
                        kind="truncated",
                        detail=self._cut_detail(played),
                        title=self._title(),
                        played=played,
                        engine=self.engine,
                    )
                )
            asyncio.run_coroutine_threadsafe(self._ended(played), self.bot.loop)

        return _after

    def _title(self) -> str:
        """The station's name as a card writes it, or a neutral label."""
        station = self.current
        name = getattr(station, "name", "") or ""
        icon = getattr(station, "icon", "") or ""
        return f"{icon} {name}".strip() or "The station"

    def _worth_another_attempt(self, played: float) -> bool:
        """Whether this ending should be reconnected instead of reported.

        Deliberately not a question about the *station*: whether a cut was the
        station going away or the network in between closing the socket looks
        identical from here, and it is answered by whether the next attempt
        plays. So the budget is spent first and asked about later.
        """
        if self.current is None:
            return False
        if played >= LIVE_RETRY_AFTER:
            # It played a real stretch before it stopped, so this is a connection
            # that ended rather than a station that will not hold one.
            self._live_retries = 0
        if self._live_retries >= LIVE_RETRY_LIMIT:
            return False
        self._live_retries += 1
        return True

    async def _reconnect(self, played: float) -> None:
        """Try the same station again, and tell the room nothing unless it fails.

        The room hears a gap of ``LIVE_RETRY_PAUSE`` and then, usually, the radio
        again — no card, because a station that dropped once and came back is not
        news, and a card every time is what teaches a room to ignore them.
        ``played`` is carried so the card, when there is one, still says how much
        of the station the room actually heard.
        """
        await asyncio.sleep(LIVE_RETRY_PAUSE)
        station = self.current
        async with self._play_lock:
            if self.manual_stop or station is None or self.current is not station:
                return
            if self.voice_client is None or not self.voice_client.is_connected():
                return
            started, _why = self._start(station)
        if started:
            print(
                f"[RADIO] ⟳ reconnected {station.name!r} in guild {self.guild_id} "
                f"(attempt {self._live_retries}/{LIVE_RETRY_LIMIT})",
                flush=True,
            )
            return
        # It would not come back. Now it is news — said once, and then the
        # connection is left holding either a 24/7 seat or nothing at all.
        self._report(
            Fault(
                kind="truncated",
                detail=self._cut_detail(played),
                title=self._title(),
                played=played,
                engine=self.engine,
            )
        )
        await self._ended(played)

    async def _ended(self, played: float) -> None:
        """Nothing is on air any more: hold the seat or start leaving."""
        self.current = None
        self.source = None
        if self.stay:
            return
        self.start_alone_timer()

    def _cut_detail(self, played: float) -> str:
        """Why the source ended, in the words the thing that ended deserves."""
        detail = (
            f"the station went quiet after **{_mmss(played)}** — a live stream has no "
            "end, so that is a dropped connection rather than a finished song"
        )
        if self._live_retries:
            detail += (
                f". It would not come back after {LIVE_RETRY_LIMIT} attempts, so this "
                "is the station for now rather than a blip"
            )
        return detail

    def _report(self, fault: Fault) -> None:
        """Hand a fault to whoever is listening, and never raise into the audio thread."""
        fault.guild_id = self.guild_id
        print(
            f"[RADIO] fault in guild {self.guild_id}: {fault.kind} — {fault.detail}",
            flush=True,
        )
        if self.on_fault is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.on_fault(fault), self.bot.loop)
        except Exception as exc:  # noqa: BLE001 — a report must not raise
            print(f"[RADIO] could not report a fault: {exc}", flush=True)

    async def start(self, station: "Station", requester: str = "") -> tuple[bool, str]:
        """Put a station on air, replacing whatever was playing.

        The lock is held across the swap so two starts cannot interleave; the
        generation is bumped *before* the old source is stopped, so its
        after-callback reads as history rather than as this station dying.
        """
        async with self._play_lock:
            vc = self.voice_client
            if vc is None or not vc.is_connected():
                return False, "I lost the voice connection."
            self.manual_stop = False
            self.cancel_alone_timer()
            if vc.is_playing() or vc.is_paused():
                self._generation += 1
                try:
                    vc.stop()
                except Exception:  # noqa: BLE001 — stopping twice is not a fault
                    pass
            self.current = station
            self.requester = requester
            started, why = self._start(station)
            if not started:
                self.current = None
                self._report(
                    Fault(kind="unplayable", detail=why, title=self._title(), engine=self.engine)
                )
            return started, why

    def _start(self, station: "Station") -> tuple[bool, str]:
        """Build the source for ``station`` and hand it to Discord.

        Returns ``(started, why_not)``, and it exists as one method because two
        things start a source — the command, and a station that was cut and is
        being asked again. Those two want the same eight lines and must not
        drift; the alternative was the reconnect path quietly missing the
        generation bump or the start clock, which is how a resumed station ends
        up with no fault detection at all.
        """
        vc = self.voice_client
        if vc is None or not vc.is_connected():
            return False, "The voice connection went away."
        if self.current is not station:
            # The budget belongs to the station that spent it. A reconnect passes
            # the *same* station back in, so it keeps its count — a server that
            # switches station must not inherit a spent one, or the new station's
            # first drop is reported as a failure that never happened to it.
            self._live_retries = 0
        try:
            source, engine = self._build_source(station.url, channel_bitrate_kbps(vc), station)
        except Exception as exc:
            return False, f"The audio engine refused to start ({type(exc).__name__})."

        if vc.is_playing() or vc.is_paused():
            self._generation += 1
            try:
                vc.stop()
            except Exception:  # noqa: BLE001
                pass
        self._generation += 1
        self.engine = engine
        self.source = source
        self._started_at = time.monotonic()
        try:
            vc.play(source, after=self._after_for(self._generation))
        except Exception as exc:
            self.source = None
            return False, f"Discord refused the stream ({type(exc).__name__})."
        print(
            f"[RADIO] ▶ {station.name!r} via {engine} in guild {self.guild_id}",
            flush=True,
        )
        return True, ""

    def _build_source(self, stream_url: str, bitrate_kbps: int = 128, station=None):
        """The one place an ffmpeg command line is written. Returns ``(source, engine)``.

        A station arrives decoded and re-encoded: icecast serves MP3 or AAC, and
        Discord wants Opus, so ffmpeg does the work on every stream. Three paths
        exist, in order of how much they touch the audio:

        * **pass-through** — volume at 100% and the source already Opus: the
          packets go out untouched, which keeps the source's own rate instead of
          re-encoding it down, and needs **no libopus at all**. No station in the
          catalogue declares itself Opus yet, so nothing takes this path today; it
          is one flag on a station away from being the best-sounding one.
        * **PCM with a live volume transformer** — when libopus is available, so
          the volume can change mid-stream instead of restarting it.
        * **re-encode in ffmpeg** — the always-available path: the volume is baked
          into the command line, and a change re-renders the source. It encodes at
          :func:`encode_bitrate` and with :data:`RADIO_ENCODER`, both because this
          is a radio meant to sound like music and not like a phone call.
        """
        station = station if station is not None else self.current
        caps = voice_caps.probe()
        executable = caps.ffmpeg or "ffmpeg"
        before = BEFORE_OPTIONS

        copy_ok = (
            abs(self.volume - 1.0) < 1e-6
            and station is not None
            and getattr(station, "is_opus", False)
        )
        if copy_ok:
            source_kbps = getattr(station, "bitrate", 0) or 0
            kept = f"{source_kbps:.0f} kbps" if source_kbps else "source rate"
            return (
                discord.FFmpegOpusAudio(
                    stream_url, executable=executable, codec="copy", before_options=before
                ),
                f"pass-through ({kept}, untouched)",
            )

        if abs(self.volume - 1.0) >= 1e-6 and caps.opus:
            # PCM + transformer: the volume is applied in this process, so it can
            # change without touching the stream.
            raw = discord.FFmpegPCMAudio(
                stream_url, executable=executable, before_options=before, options="-vn"
            )
            return (
                discord.PCMVolumeTransformer(raw, volume=self.volume),
                "PCM + live volume",
            )

        options = "-vn"
        if abs(self.volume - 1.0) >= 1e-6:
            options += f" -af volume={self.volume}"
        options = f"{options} {RADIO_ENCODER}"
        target_kbps = encode_bitrate(bitrate_kbps, station)
        return (
            discord.FFmpegOpusAudio(
                stream_url,
                executable=executable,
                bitrate=target_kbps,
                before_options=before,
                options=options,
            ),
            f"ffmpeg encode {target_kbps} kbps",
        )

    def restart(self) -> bool:
        """Re-render what is playing so a volume change is audible now.

        The generation is bumped *before* the stop, because the old source's
        after-callback fires on the way out and would otherwise read the
        deliberate stop as the station dying and spend a retry on it.
        """
        vc = self.voice_client
        if not vc or not vc.is_connected():
            return False
        if not (vc.is_playing() or vc.is_paused()):
            return False
        station = self.current
        if station is None or not getattr(station, "url", ""):
            return False
        try:
            source, engine = self._build_source(
                station.url, channel_bitrate_kbps(vc), station
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[RADIO] could not rebuild the source: {exc}", flush=True)
            return False

        self._generation += 1
        try:
            vc.stop()
        except Exception as exc:  # noqa: BLE001
            print(f"[RADIO] could not stop the old source: {exc}", flush=True)
            return False
        self.engine = engine
        self.source = source
        self._started_at = time.monotonic()
        vc.play(source, after=self._after_for(self._generation))
        return True

    async def stop(self, *, disconnect: bool = True, reason: str = "stopped") -> None:
        """Take the station off air, and leave unless this is a 24/7 seat."""
        self.current = None
        self.requester = ""
        self.source = None
        self.manual_stop = True
        self._generation += 1
        self.cancel_alone_timer()
        vc = self.voice_client
        if vc is None:
            return
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
        except Exception:  # noqa: BLE001
            pass
        if disconnect and not self.stay:
            try:
                await vc.disconnect(force=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[RADIO] could not disconnect ({reason}): {exc}", flush=True)
            # Only let go of the client when the socket really went away. A held
            # seat is *still connected* — the station is off air, the bot is not
            # gone — and dropping the reference there would have this player
            # report ``connected: false`` about a channel it is sitting in, and
            # refuse the next ``start()`` with "I lost the voice connection."
            self.voice_client = None

    async def leave(self, *, reason: str = "left") -> None:
        """Leave without clearing 24/7 state — used by the timer."""
        await self.stop(disconnect=True, reason=reason)

    # ---------------------------------------------------------------- volume #
    def set_volume(self, value: float) -> tuple[bool, float]:
        """Set the volume, and whether it took effect on what is playing now.

        The PCM path can change it live; every other path has to re-render the
        stream, which is a sub-second gap at the live edge and is reported as
        such rather than pretending nothing happened.
        """
        self.volume = min(max(float(value), 0.05), 1.5)
        if isinstance(self.source, discord.PCMVolumeTransformer):
            self.source.volume = self.volume
            return True, self.volume
        return self.restart(), self.volume

    # ---------------------------------------------------------------- status #
    def status(self) -> dict:
        """What this guild is doing, for the owner console and the page.

        Names of *stations*, never of people: this shape is also served on a
        page, and a room is not a roster.
        """
        station = self.current
        return {
            "guild_id": self.guild_id,
            "connected": bool(self.voice_client and self.voice_client.is_connected()),
            "channel": getattr(getattr(self.voice_client, "channel", None), "name", None),
            "playing": bool(self.voice_client and self.voice_client.is_playing()),
            "paused": bool(self.voice_client and self.voice_client.is_paused()),
            "station": (
                {
                    "key": getattr(station, "key", ""),
                    "name": getattr(station, "name", ""),
                    "genre": getattr(station, "genre", ""),
                    "language": getattr(station, "language", ""),
                    "country": getattr(station, "country", ""),
                    "capital": getattr(station, "capital", ""),
                    "icon": getattr(station, "icon", ""),
                }
                if station
                else None
            ),
            "requester": self.requester,
            "engine": self.engine,
            "position": round(max(0.0, time.monotonic() - self._started_at), 1)
            if station
            else 0.0,
            "volume": round(self.volume, 3),
            "stay": self.stay,
        }


def _mmss(seconds: float) -> str:
    """``m:ss`` for a duration, which is how a station's airtime is read."""
    total = int(max(0.0, seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
