"""One guild's playback: the queue, the source, and everything that can go wrong quietly.

Three defects decide the shape of this module, and all three are about a *callback
meaning something other than what it says*.

**A deliberate stop is not a finished song.** Applying a filter or changing the
volume means replacing the audio source, and the old source's after-callback
fires on the way out — so the queue advanced too, and changing a filter threw
away a queued track. Every source is tagged with the **generation** it was
created in, and a callback only advances the queue while its generation is still
current. The same counter is what makes ``restart_current`` safe, and what stops
``/radio`` from advancing twice and then wiping ``current`` while the new stream
was playing.

**A stream that dies is not a song that ended.** ``ffmpeg`` reporting an exit —
a dropped connection, a bad flag, the station going down — reaches the
after-callback looking like a finished track, and a *clean* EOF looks even more
like one. So the player judges what ended: a **live** source (a station, which
has no end) that reports itself finished at all was cut, and a source that
declares a length and stops far short of it was cut too. Either way the room is
told — it names the track, how much of it played, and what the engine was —
because the archived cog could not tell the two apart, and that silence was
"the music is short".

**A black box is not a diagnosis.** The ffmpeg command line is built in exactly
one place (:meth:`GuildPlayer._build_source`) and it is deliberately short. The
old one carried ``-err_detect ignore_err -probesize 32768 -analyzeduration 0``,
which tells ffmpeg to ignore the errors that would explain a truncation and to
start playing before it has read enough of the container to know what it is —
plausible remedies for a different problem, and prime suspects for this one.
``-reconnect*`` stays, because a stream that is merely interrupted should be
retried rather than treated as over.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import discord

from . import media_source, voice_caps

#: Extra ffmpeg flags before ``-i``. ``-nostdin`` matters more than it looks:
#: without it ffmpeg shares stdin with the bot process, and a stray read there
#: can stop playback for no visible reason.
BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-nostdin"
)

#: Leave a channel nobody is in, and an idle connection, after this long.
ALONE_SECONDS = 120
INACTIVE_SECONDS = 300

#: A queue nobody can drain is a memory leak wearing a playlist. Radio and 24/7
#: channels are exempt because staying queued there is the point.
MAX_QUEUE = 200

#: How a track can end early and be *reported* rather than silently skipped.
#: Anything shorter than the floor is not judged (a station that is simply off
#: the air answers the socket and closes it, and a five-second silence is not
#: worth a card), and the fraction is generous because a declared duration is
#: approximate even when somebody did declare one.
EARLY_EOF_FLOOR = 25
EARLY_EOF_FRACTION = 0.6

AVERAGE_FRAME = 20

#: Every live player, by guild. Module-level because two cogs have to agree about
#: one socket: a Discord client sits in one channel at a time, so Voice Company
#: has to be able to ask "is music using this connection?" before it decides to
#: leave the room — otherwise a companion leaving takes the music with it.
PLAYERS: dict[int, "GuildPlayer"] = {}


def holds_voice(guild_id) -> bool:
    """Whether a music player is using this guild's voice connection right now.

    True while there is anything to play, anything queued, or a 24/7 seat being
    kept: the three reasons a disconnect here would be somebody's music stopping.
    """
    try:
        key = int(guild_id or 0)
    except (TypeError, ValueError):
        return False
    player = PLAYERS.get(key)
    if player is None:
        return False
    if player.current is not None or player.queue:
        return True
    if player.is_247:
        return True
    client = player.voice_client
    try:
        return bool(client and client.is_connected() and (client.is_playing() or client.is_paused()))
    except Exception:
        return False


@dataclass(frozen=True)
class Filter:
    """One real-time audio preset: the words, and the ffmpeg that does it."""

    key: str
    name: str
    emoji: str
    args: str
    description: str


#: The presets. ``normal`` is the absence of a filter rather than an empty one, so
#: "reset" and "no filter" are the same code path.
FILTERS: tuple[Filter, ...] = (
    Filter("normal", "Normal (studio clean)", "🎵", "", "The recording as it was mastered, flat."),
    Filter("bassboost", "Bass boost", "🔊", "bass=g=10:f=110:w=0.6", "Low-end weight and sub-bass punch."),
    Filter("nightcore", "Nightcore", "🌙", "asetrate=48000*1.25,aresample=48000,atempo=1.05", "Faster and higher — the sped-up edit, on request."),
    Filter("vaporwave", "Vaporwave", "🌊", "asetrate=48000*0.82,aresample=48000,atempo=0.92", "Slowed, pitched down, hazy."),
    Filter("8d", "8D spatial audio", "🎧", "apulsator=hz=0.125", "Rotates around the listener's head."),
    Filter("pop", "Vocal boost", "🎤", "equalizer=f=1000:t=q:w=1:g=2,equalizer=f=3000:t=q:w=1:g=3", "Pushes a vocal forward in the mix."),
)

FILTER_BY_KEY: dict[str, Filter] = {item.key: item for item in FILTERS}


def filter_for(key: str) -> Filter:
    """The named filter, or ``normal`` — never a KeyError from a stored value."""
    return FILTER_BY_KEY.get((key or "normal").lower(), FILTER_BY_KEY["normal"])


#: What ``libopus`` is told whenever this process has to re-encode, and why each
#: part of it. This is the second half of the quality complaint, and it exists
#: because the first half (a ceiling on pass-through, below) is not the only way
#: to throw audio away.
#:
#: ``discord.py`` writes ``-fec true -packet_loss 15`` into *every*
#: ``FFmpegOpusAudio`` command line. That is the right trade for a voice library —
#: telling the encoder to expect 15% loss is how a dropped syllable survives — and
#: the wrong one for a music player, because the in-band redundancy is carried by
#: libopus's **speech** path. Measured on this host, against a musical signal at
#: 96 kbps, the fidelity of the encoded result was:
#:
#: ====================  ====================
#: ``-application audio``  −43.83 dB (best)
#: ``audio`` + FEC on      −39.53 dB
#: ``-application voip``   −36.97 dB
#: ====================  ====================
#:
#: i.e. discord.py's two defaults together cost about **7 dB** against the music
#: configuration, and turning FEC off removed the whole of its 4.3 dB share even
#: with ``-packet_loss 15`` still present. ``-compression_level 10`` then buys a
#: little more quality at the same bitrate for CPU one stream never notices.
#:
#: These are appended *after* discord.py's own flags on purpose: ffmpeg keeps the
#: last occurrence of an option, so ours are the ones that take effect.
MUSIC_ENCODER = (
    "-application audio -frame_duration 20 -vbr on -compression_level 10 "
    "-fec false -packet_loss 0"
)

#: The highest this will ever ask libopus for. Past this a music stream gains
#: nothing audible, and it is comfortably under Discord's own 384 kbps ceiling.
MAX_ENCODE_KBPS = 256


def channel_bitrate_kbps(vc) -> int:
    """What the channel this connection is in asks Discord's clients to encode at.

    Discord's default is 64 kbps, and it is the *channel's* number rather than a
    limit on what the bot may send — which is why :func:`encode_bitrate` treats it
    as a floor and not as the target.
    """
    raw = int(getattr(getattr(vc, "channel", None), "bitrate", 128000) or 128000)
    return min(max(raw // 1000, 64), 384)


def encode_bitrate(channel_kbps: int, track) -> int:
    """How fast to encode when the audio has to be re-encoded rather than passed on.

    The rule used to be *the channel's own bitrate*, and that is the number behind
    the report of echo and poor quality: Discord's default voice channel is **64
    kbps**, so a 160 kbps recording was decoded and re-encoded at 64 — more than
    half of it discarded before libopus had done anything else — and this is the
    path taken by *every* source that is not already Opus, which is every source
    once the provider that does serve Opus refuses the host.

    Encoding above the source cannot bring back what is gone; encoding below it
    throws away what is not. So: the source's own rate, never under what the
    channel asks for, never past :data:`MAX_ENCODE_KBPS`.
    """
    source = int(getattr(track, "abr", 0) or 0)
    return min(max(int(channel_kbps or 0), source, 64), MAX_ENCODE_KBPS)


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
            "truncated": "a stream was cut off",
            "source_error": "the audio engine failed",
            "unplayable": "a station could not be played",
            "opus_missing": "this host cannot encode audio",
        }
        return names.get(self.kind, self.kind.replace("_", " "))


class GuildPlayer:
    """The queue, the voice client and the current source for one guild.

    Deliberately holds no Discord *lookup* of its own: the cog puts the channel
    and the client in, and this object never reaches for state it was not given,
    which is what lets the whole thing be driven in a test without a gateway.
    """

    def __init__(self, bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.voice_client: discord.VoiceClient | None = None
        self.text_channel = None
        self.queue: list[media_source.Track] = []
        self.current: media_source.Track | None = None
        # 100% by default, and that default is a *quality* decision rather than a
        # taste one: the archived cog shipped 0.7, and anything other than exactly
        # 1.0 disqualifies the pass-through path — so every single track was
        # decoded and re-encoded while the code that avoided it sat unused. A
        # member who turns the volume down chooses a re-encode; nobody should get
        # one by default.
        self.volume = 1.0
        self.filter_name = "normal"
        self.is_247 = False
        self.manual_stop = False
        self.source: discord.AudioSource | None = None
        self.engine = ""

        self._generation = 0
        self._play_lock = asyncio.Lock()
        self._started_at = 0.0
        self._expected = 0.0
        PLAYERS[self.guild_id] = self
        self.alone_task: asyncio.Task | None = None
        self.inactivity_task: asyncio.Task | None = None

        #: Where a truncation or a source error is reported. The cog sets this to
        #: a coroutine (operator log + a card in the channel); leaving it unset is
        #: a supported state, and it is what keeps a fault from raising inside the
        #: audio thread, where nothing would see it.
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
        fits an album.
        """
        if self.is_247:
            return
        self.cancel_alone_timer()
        try:
            self.alone_task = self.bot.loop.create_task(self._alone_timeout())
        except Exception:
            self.alone_task = None

    async def _alone_timeout(self) -> None:
        await asyncio.sleep(ALONE_SECONDS)
        if self.is_247:
            return
        channel = getattr(self.voice_client, "channel", None)
        if channel is None or not self.voice_client.is_connected():
            return
        humans = [
            member
            for member in getattr(channel, "members", ()) or ()
            if not getattr(member, "bot", False)
        ]
        if not humans:
            await self.leave(reason="empty")

    def cancel_inactivity_timer(self) -> None:
        if self.inactivity_task and not self.inactivity_task.done():
            self.inactivity_task.cancel()
        self.inactivity_task = None

    def start_inactivity_timer(self) -> None:
        if self.is_247:
            return
        self.cancel_inactivity_timer()
        try:
            self.inactivity_task = self.bot.loop.create_task(self._inactivity_timeout())
        except Exception:
            self.inactivity_task = None

    async def _inactivity_timeout(self) -> None:
        await asyncio.sleep(INACTIVE_SECONDS)
        if self.is_247 or self.queue or self.current:
            return
        if self.voice_client and self.voice_client.is_connected():
            if not (self.voice_client.is_playing() or self.voice_client.is_paused()):
                await self.leave(reason="idle")

    # -------------------------------------------------------------- playback #
    def _after_for(self, generation: int):
        """An after-callback owned by one generation of the source.

        This is the whole of the "deliberate stop advanced the queue" fix, and it
        is also where a truncated stream is told apart from a finished one.
        """

        def _after(error):
            if generation != self._generation:
                # A newer source owns playback: the stop that fired this callback
                # was ours (a filter, a volume change, a skip), so this signal is
                # not a track ending and must not advance the queue.
                return
            played = max(0.0, time.monotonic() - self._started_at)
            truncated = self._ended_early(played, error)
            if error:
                self._report(
                    Fault(
                        kind="source_error",
                        detail=f"the audio engine reported `{type(error).__name__}: {error}`",
                        title=getattr(self.current, "title", ""),
                        played=played,
                        expected=self._expected,
                        engine=self.engine,
                    )
                )
            elif truncated:
                self._report(
                    Fault(
                        kind="truncated",
                        detail=self._cut_detail(played),
                        title=getattr(self.current, "title", ""),
                        played=played,
                        expected=self._expected,
                        engine=self.engine,
                    )
                )
            asyncio.run_coroutine_threadsafe(self.play_next(error), self.bot.loop)

        return _after

    def _ended_early(self, played: float, error) -> bool:
        """Whether a source that ended without an error ended *early*.

        Two shapes, because there are two things that can end here. A **station
        has no end**, so a live source that reports itself finished was cut — and
        that is the case the room used to hear nothing about, since a closed
        connection is a clean EOF to everything downstream. A source that
        *declares* a length (which no station does; ``Track`` still carries one)
        is judged against it, with a floor so an intro is not a fault and a
        generous fraction because the declared number is approximate.
        """
        if error or not self.current:
            return False
        if getattr(self.current, "live", False):
            return True
        expected = self._expected
        if expected < EARLY_EOF_FLOOR:
            return False
        return played < expected * EARLY_EOF_FRACTION

    def _cut_detail(self, played: float) -> str:
        """Why the source ended, in the words the thing that ended deserves."""
        if getattr(self.current, "live", False):
            return (
                f"the station went quiet after **{_mmss(played)}** — a live stream has no "
                "end, so that is a dropped connection rather than a finished song"
            )
        return (
            f"the stream ended after **{_mmss(played)}** when it was meant to run "
            f"**{_mmss(self._expected)}** — that is a cut-off stream, not a short song"
        )

    def _report(self, fault: Fault) -> None:
        """Hand a fault to whoever is listening, and never raise into the audio thread."""
        fault.guild_id = self.guild_id
        print(
            f"[MUSIC] fault in guild {self.guild_id}: {fault.kind} — {fault.detail}",
            flush=True,
        )
        if self.on_fault is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.on_fault(fault), self.bot.loop)
        except Exception as exc:
            print(f"[MUSIC] could not report a fault: {exc}", flush=True)

    async def play_next(self, error=None) -> None:
        """Advance: take the next station, build the source, start it. One lock."""
        async with self._play_lock:
            if error:
                print(f"[MUSIC] playback error in guild {self.guild_id}: {error}", flush=True)

            vc = self.voice_client
            if vc is None or not vc.is_connected():
                self.current = None
                return

            self.manual_stop = False

            if not self.queue:
                self.current = None
                self.start_inactivity_timer()
                return

            self.cancel_inactivity_timer()
            track = self.queue.pop(0)

            if not track.stream_url:
                # Nothing to resolve it with: a station arrives with its own
                # address, and the extractor that could turn *words* into one is
                # archived. A track here without an address is a defect in
                # whatever queued it rather than a refused provider, so it is
                # named rather than skipped in silence.
                self.current = None
                await self._advance_after_failure(
                    track, "That station has no address to play — it was queued without one."
                )
                return

            self.current = track
            bitrate_kbps = channel_bitrate_kbps(vc)

            try:
                source, engine = self._build_source(track.stream_url, bitrate_kbps)
            except Exception as exc:
                self.current = None
                await self._advance_after_failure(track, f"The audio engine refused to start ({type(exc).__name__}).")
                return

            if vc.is_playing() or vc.is_paused():
                self._generation += 1
                vc.stop()
            self._generation += 1
            self.engine = engine
            self.source = source
            self._started_at = time.monotonic()
            self._expected = float(track.duration or 0)
            try:
                vc.play(source, after=self._after_for(self._generation))
            except Exception as exc:
                self.current = None
                self.source = None
                await self._advance_after_failure(track, f"Discord refused the stream ({type(exc).__name__}).")
                return
            print(
                f"[MUSIC] ▶ {track.title!r} via {engine} in guild {self.guild_id}",
                flush=True,
            )

    async def _advance_after_failure(self, track, reason: str) -> None:
        """Say what went wrong, then keep the queue moving."""
        self._report(
            Fault(
                kind="unplayable",
                detail=reason,
                title=getattr(track, "title", ""),
                engine=self.engine,
            )
        )
        if self.voice_client and self.voice_client.is_connected() and self.queue:
            self.bot.loop.create_task(self.play_next())

    def _build_source(self, stream_url: str, bitrate_kbps: int = 128):
        """The one place an ffmpeg command line is written. Returns ``(source, engine)``.

        A station arrives decoded and re-encoded: icecast serves MP3 or AAC, and
        Discord wants Opus, so ffmpeg is doing the work on every stream this bot
        plays. Three paths exist in order of how much they touch the audio, and
        each was chosen for a reason that is measured rather than hoped:

        * **pass-through** — nothing to reshape, volume at 100%, the source already
          Opus: the packets go out untouched, which keeps the source's own ~130
          kbps instead of re-encoding it down, and skips an entire encode pass.
          This is also the path that needs **no libopus at all**
          (``FFmpegOpusAudio.is_opus()`` is ``True``, so ``discord.py`` never
          builds an encoder). No station in the catalogue declares itself Opus
          yet, so nothing takes this path today; it is one flag on a station away
          from being the best-sounding one.

          It used to *also* require the source to be no louder than the channel,
          and that one clause is the quality report: a channel's bitrate is where
          Discord's own client should encode and it is **64 kbps on a normal
          server**, so a 130 kbps Opus song failed the test and was re-encoded down
          to 64 — every song, on every server without a boost. Handing on the
          provider's own packets costs nothing and cannot sound worse than
          re-encoding them, so the clause is gone.
        * **PCM with a live volume transformer** — when libopus is available, so
          the volume can change mid-track instead of restarting the stream.
        * **re-encode in ffmpeg** — the always-available path: filters and volume
          are baked into the command line, and a change re-renders the source. It
          encodes at :func:`encode_bitrate` and with :data:`MUSIC_ENCODER`, both
          because this is a music player and not a phone call.
        """
        caps = voice_caps.probe()
        chosen = filter_for(self.filter_name)
        track = self.current
        executable = caps.ffmpeg or "ffmpeg"
        before = BEFORE_OPTIONS

        copy_ok = (
            not chosen.args
            and abs(self.volume - 1.0) < 1e-6
            and track is not None
            and track.is_opus
        )
        if copy_ok:
            source_kbps = getattr(track, "abr", 0) or 0
            kept = f"{source_kbps:.0f} kbps" if source_kbps else "source rate"
            return (
                discord.FFmpegOpusAudio(
                    stream_url, executable=executable, codec="copy", before_options=before
                ),
                f"pass-through ({kept}, untouched)",
            )

        shaping = "-vn"
        if chosen.args:
            shaping += f" -af {chosen.args}"
        if abs(self.volume - 1.0) >= 1e-6 and caps.opus:
            # PCM + transformer: the volume is applied in this process, so it can
            # change without touching the stream. ``discord.py``'s own encoder is
            # already ``APPLICATION_AUDIO``, so nothing has to be corrected here.
            raw = discord.FFmpegPCMAudio(
                stream_url, executable=executable, before_options=before, options=shaping
            )
            return (
                discord.PCMVolumeTransformer(raw, volume=self.volume),
                f"PCM + live volume [{chosen.name}]",
            )

        # One ``-af``, not two: a second one *replaces* the first rather than
        # chaining onto it, so a filter chosen here used to disappear silently
        # whenever the volume was not at 100%.
        chain: list[str] = []
        if chosen.args:
            chain.append(chosen.args)
        if abs(self.volume - 1.0) >= 1e-6:
            chain.append(f"volume={self.volume}")
        options = f"-vn -af {','.join(chain)}" if chain else "-vn"
        options = f"{options} {MUSIC_ENCODER}"
        target_kbps = encode_bitrate(bitrate_kbps, track)
        return (
            discord.FFmpegOpusAudio(
                stream_url,
                executable=executable,
                bitrate=target_kbps,
                before_options=before,
                options=options,
            ),
            f"ffmpeg encode [{chosen.name}] {target_kbps} kbps",
        )

    def restart_current(self) -> bool:
        """Re-render what is playing so a filter or a volume change is audible now.

        The generation is bumped *before* the stop, because the old source's
        after-callback fires on the way out and would otherwise read the
        deliberate stop as a finished track and skip ahead.
        """
        vc = self.voice_client
        if not vc or not vc.is_connected():
            return False
        if not (vc.is_playing() or vc.is_paused()):
            return False
        if not self.current or not self.current.stream_url:
            return False
        try:
            source, engine = self._build_source(
                self.current.stream_url, channel_bitrate_kbps(vc)
            )
        except Exception as exc:
            print(f"[MUSIC] could not rebuild the source: {exc}", flush=True)
            return False

        self._generation += 1
        try:
            vc.stop()
        except Exception as exc:
            print(f"[MUSIC] could not stop the old source: {exc}", flush=True)
            return False
        # Whatever is left of the track, not the whole of it: a filter applied at
        # 2:30 must not restart the song from the beginning.
        remaining = max(0.0, self._expected - (time.monotonic() - self._started_at))
        self.engine = engine
        self.source = source
        self._started_at = time.monotonic()
        self._expected = remaining or self._expected
        vc.play(source, after=self._after_for(self._generation))
        return True

    def skip(self) -> bool:
        """Stop the current source so the queue advances."""
        vc = self.voice_client
        if not vc or not vc.is_connected() or not (vc.is_playing() or vc.is_paused()):
            return False
        self.manual_stop = True
        vc.stop()
        return True

    async def stop(self, *, disconnect: bool = True, reason: str = "stopped") -> None:
        """Clear everything, and leave unless this connection is a 24/7 one."""
        self.queue.clear()
        self.current = None
        self.source = None
        self.manual_stop = True
        self._generation += 1
        self.cancel_alone_timer()
        self.cancel_inactivity_timer()
        vc = self.voice_client
        if vc is None:
            return
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
        except Exception:
            pass
        if disconnect and not self.is_247:
            try:
                await vc.disconnect(force=True)
            except Exception as exc:
                print(f"[MUSIC] could not disconnect ({reason}): {exc}", flush=True)
        self.voice_client = None

    async def leave(self, *, reason: str = "left") -> None:
        """Leave without clearing 24/7 state — used by the timers."""
        await self.stop(disconnect=True, reason=reason)

    # ---------------------------------------------------------------- volume #
    def set_volume(self, value: float) -> tuple[bool, float]:
        """Set the volume, and whether it took effect on what is playing now.

        The PCM path can change it live; every other path has to re-render the
        stream, which is a sub-second gap and is reported as such rather than
        pretending nothing happened.
        """
        self.volume = min(max(float(value), 0.05), 1.5)
        if isinstance(self.source, discord.PCMVolumeTransformer):
            self.source.volume = self.volume
            return True, self.volume
        return self.restart_current(), self.volume

    def set_filter(self, key: str) -> tuple[bool, Filter]:
        """Apply a preset, and whether it is audible on the current source."""
        self.filter_name = filter_for(key).key
        return self.restart_current(), filter_for(key)

    # ---------------------------------------------------------------- status #
    def status(self) -> dict:
        """What this guild is doing, for the owner console and ``/music`` itself.

        Counts and names of *recordings*, never of people: this shape is also
        served on a page, and a queue is not a roster.
        """
        return {
            "guild_id": self.guild_id,
            "connected": bool(self.voice_client and self.voice_client.is_connected()),
            "channel": getattr(getattr(self.voice_client, "channel", None), "name", None),
            "playing": bool(self.voice_client and self.voice_client.is_playing()),
            "paused": bool(self.voice_client and self.voice_client.is_paused()),
            "current": (
                {
                    "title": self.current.title,
                    "provider": self.current.provider,
                    "duration": self.current.duration,
                    "requester": self.current.requester,
                }
                if self.current
                else None
            ),
            "engine": self.engine,
            "position": round(max(0.0, time.monotonic() - self._started_at), 1) if self.current else 0.0,
            "volume": round(self.volume, 3),
            "filter": self.filter_name,
            "queue": len(self.queue),
            "queue_preview": [track.title for track in self.queue[:5]],
            "is_247": self.is_247,
        }


def _mmss(seconds: float) -> str:
    """``m:ss`` for a duration, which is how a track length is read."""
    total = int(max(0.0, seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_duration(seconds: int) -> str:
    """A track length for a card: ``m:ss``, or the word for a live stream."""
    if not seconds or seconds <= 0:
        return "live"
    return _mmss(seconds)
