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

**A stream that dies is not a song that ended.** ``ffmpeg`` exiting early — a
blocked connection, a provider cutting a datacenter IP, a bad flag — reaches the
after-callback as ``err=None``, exactly like a track finishing. So the player
knows how long the source *should* have run, and a source that ends far short of
that is reported as a fault: it names the track, how much of it played, and what
the engine was. The archived cog could not tell the two apart, which is what
"the music is short" was.

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
#: Anything shorter than the floor is not judged (intros, stingers, live joins),
#: and the fraction is generous because a provider's own duration is approximate.
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


def _seek_prefix(seconds: float) -> str:
    """``-ss`` before ``-i`` seeks the input, which is what makes it cheap."""
    if not seconds or seconds <= 0.5:
        return ""
    return f"-ss {seconds:.2f} "


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

#: Loop modes, in the order the command offers them.
LOOP_MODES: tuple[str, ...] = ("off", "track", "queue")


def filter_for(key: str) -> Filter:
    """The named filter, or ``normal`` — never a KeyError from a stored value."""
    return FILTER_BY_KEY.get((key or "normal").lower(), FILTER_BY_KEY["normal"])


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
            "unplayable": "a track could not be played",
            "blocked": "the provider refused this host",
            "opus_missing": "this host cannot encode audio",
            # Not a failure, and that is the point of giving it a name here: the
            # song is playing, just from the other provider, and the room is told
            # so that a *different* upload is never a silent surprise.
            "rescued": "the track was picked up from another provider",
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
        self.loop = "off"
        self.is_247 = False
        self.manual_stop = False
        self.source: discord.AudioSource | None = None
        self.engine = ""
        #: Where in the current track playback should begin, in seconds. Set by
        #: ``seek`` and cleared when a new track starts.
        self.seek_offset = 0.0

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
            truncated = self._looks_truncated(played, error)
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
                        detail=(
                            f"the stream ended after **{_mmss(played)}** when the "
                            f"provider said it ran **{_mmss(self._expected)}** — that is a cut-off "
                            "stream, not a short song"
                        ),
                        title=getattr(self.current, "title", ""),
                        played=played,
                        expected=self._expected,
                        engine=self.engine,
                    )
                )
            asyncio.run_coroutine_threadsafe(self.play_next(error), self.bot.loop)

        return _after

    def _looks_truncated(self, played: float, error) -> bool:
        """Whether a source that ended without an error ended *early*."""
        if error or not self.current:
            return False
        expected = self._expected
        if expected < EARLY_EOF_FLOOR or getattr(self.current, "live", False):
            return False
        return played < expected * EARLY_EOF_FRACTION

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
        """Advance: resolve if needed, build the source, start it. One lock."""
        async with self._play_lock:
            if error:
                print(f"[MUSIC] playback error in guild {self.guild_id}: {error}", flush=True)

            vc = self.voice_client
            if vc is None or not vc.is_connected():
                self.current = None
                return

            # A loop is decided before the queue is touched, because a track on
            # repeat must not consume the next entry.
            if self.current is not None and self.loop == "track" and not self.manual_stop:
                self.queue.insert(0, self.current)
            elif self.current is not None and self.loop == "queue" and not self.manual_stop:
                self.queue.append(self.current)
            self.manual_stop = False

            if not self.queue:
                self.current = None
                self.start_inactivity_timer()
                return

            self.cancel_inactivity_timer()
            track = self.queue.pop(0)
            resolve_error = ""

            if not track.is_radio and not track.fresh:
                try:
                    resolution = await asyncio.get_running_loop().run_in_executor(
                        None, media_source.stream, track
                    )
                    media_source.apply_stream(track, resolution)
                except media_source.MediaError as exc:
                    resolve_error = exc.message
                except Exception as exc:  # a provider that answered with a shape we do not know
                    resolve_error = f"The provider could not be read ({type(exc).__name__})."

            if not track.stream_url:
                # **Before giving up, ask a different provider.** One host's network
                # being refused by one platform is the failure nobody can see from
                # a laptop, and it used to be the end of the song: the room was
                # told the stream died and the queue moved on. The same artist and
                # title are looked for somewhere that will answer, and *only if that
                # also fails* is the track reported as unplayable.
                rescued = await self._rescue(track)
                if rescued is not None:
                    self._report(
                        Fault(
                            kind="rescued",
                            detail=(
                                f"{track.provider} would not stream it"
                                + (f" ({resolve_error})" if resolve_error else "")
                                + f", so this is the {rescued.provider} copy."
                            ),
                            title=track.title,
                            engine=self.engine,
                        )
                    )
                    track = rescued
                else:
                    # Naming the reason beats skipping in silence: "it skipped my
                    # song" is indistinguishable from a bug, and this is the
                    # message the member can act on.
                    self.current = None
                    await self._advance_after_failure(track, resolve_error or "No playable stream was found.")
                    return

            self.current = track
            self.seek_offset = 0.0
            channel_bitrate = int(getattr(getattr(vc, "channel", None), "bitrate", 128000) or 128000)
            bitrate_kbps = min(max(channel_bitrate // 1000, 64), 384)

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

    async def _rescue(self, track):
        """The same song from another provider, or ``None``. Off the event loop.

        Off the loop for the same reason a search is: a second provider's search
        plus its own extraction is seconds of blocking network work, and doing it
        inline would freeze every other guild's playback.
        """
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, media_source.rescue, track
            )
        except Exception as exc:  # noqa: BLE001 — a rescue that breaks is a rescue that failed
            print(f"[MUSIC] rescue failed for {track.title!r}: {exc}", flush=True)
            return None

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

        Three paths, in order of how much they touch the audio, and each chosen for
        a reason that is measured rather than hoped:

        * **pass-through** — nothing to reshape, volume at 100%, the source already
          Opus and no louder than the channel: the packets go out untouched, which
          keeps the provider's own ~130 kbps instead of re-encoding it down, and
          skips an entire encode pass. This is also the path that needs **no
          libopus at all** (``FFmpegOpusAudio.is_opus()`` is ``True``, so
          ``discord.py`` never builds an encoder).
        * **PCM with a live volume transformer** — when libopus is available, so
          the volume can change mid-track instead of restarting the stream.
        * **re-encode in ffmpeg** — the always-available path: filters and volume
          are baked into the command line, and a change re-renders the source.
        """
        caps = voice_caps.probe()
        chosen = filter_for(self.filter_name)
        track = self.current
        executable = caps.ffmpeg or "ffmpeg"
        before = f"{_seek_prefix(self.seek_offset)}{BEFORE_OPTIONS}"

        copy_ok = (
            not chosen.args
            and abs(self.volume - 1.0) < 1e-6
            and track is not None
            and track.is_opus
            and 0 < track.abr <= bitrate_kbps + 8
        )
        if copy_ok:
            return (
                discord.FFmpegOpusAudio(
                    stream_url, executable=executable, codec="copy", before_options=before
                ),
                f"pass-through ({track.abr:.0f} kbps)",
            )

        shaping = "-vn"
        if chosen.args:
            shaping += f" -af {chosen.args}"
        if abs(self.volume - 1.0) >= 1e-6 and caps.opus:
            # PCM + transformer: the volume is applied in this process, so it can
            # change without touching the stream.
            raw = discord.FFmpegPCMAudio(
                stream_url, executable=executable, before_options=before, options=shaping
            )
            return (
                discord.PCMVolumeTransformer(raw, volume=self.volume),
                f"PCM + live volume [{chosen.name}]",
            )

        if abs(self.volume - 1.0) >= 1e-6:
            volume_arg = f"volume={self.volume}"
            shaping = f"{shaping} -af {volume_arg}" if chosen.args else f"-vn -af {volume_arg}"
        return (
            discord.FFmpegOpusAudio(
                stream_url,
                executable=executable,
                bitrate=bitrate_kbps,
                before_options=before,
                options=shaping,
            ),
            f"ffmpeg re-encode [{chosen.name}]",
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
            source, engine = self._build_source(self.current.stream_url)
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

    def seek(self, seconds: float) -> tuple[bool, float]:
        """Start the current track from ``seconds`` in. Returns what it really did.

        A seek is a re-render, so it takes the same generation-bumping path a
        filter does — and it is refused when there is nothing to seek or when the
        position is past the end, rather than restarting at zero to look busy.
        """
        track = self.current
        if track is None or not track.stream_url:
            return False, 0.0
        position = max(0.0, float(seconds))
        if track.duration and position >= track.duration - 1:
            return False, 0.0
        self.seek_offset = position
        applied = self.restart_current()
        if applied:
            # What is left of the track is what the early-EOF check must compare
            # against, or every seek would look like a truncated stream.
            self._expected = max(0.0, float(track.duration or 0) - position)
            self._started_at = time.monotonic()
        return applied, position

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
                    "identity": self.current.identity,
                }
                if self.current
                else None
            ),
            "engine": self.engine,
            "position": round(max(0.0, time.monotonic() - self._started_at), 1) if self.current else 0.0,
            "volume": round(self.volume, 3),
            "filter": self.filter_name,
            "loop": self.loop,
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
