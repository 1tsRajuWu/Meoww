"""Whether this process can actually *speak* in a voice channel, and what is missing.

A player was retired once for being "the most fragile part" of the bot, and the
fragility was never in the queue or the filters — it was that four separate
things have to be true before a single frame reaches Discord, they fail in four
different ways, and **not one of them says anything to the member**. The card
says "now playing", the bot is in the channel, and nothing is heard.

This module is the one place that asks all four questions and can answer the
only useful sentence: *which piece is missing, and what fixes it*. It is asked
once at boot (one printed line), by ``/radio doctor``, by the owner console and
by ``/api/voice-debug`` — one probe, four readers, so no surface can disagree
with another about whether the radio can play here.

What is required, measured against the installed library rather than assumed:

* **``davey``** — mandatory, and it is the one that is easy to get wrong.
  ``discord.py`` raises ``RuntimeError('davey library needed in order to use
  voice')`` from ``VoiceClient.__init__`` when it is absent: Discord's end-to-end
  encryption (DAVE) is not optional for a bot any more, so this is not "quieter
  audio", it is **no connection at all**.
* **an ffmpeg binary** — the decoder. A host with none on ``PATH`` still has one,
  because ``imageio-ffmpeg`` ships a self-contained static build in its wheel;
  that is what makes this work on a host that cannot ``apt-get`` anything. A
  system binary is preferred when present (it is usually newer).
* **libopus** — **not** required to play, which is worth writing down because
  the opposite is widely believed and this module used to assume it. A
  ``FFmpegOpusAudio`` reports ``is_opus() == True``, so ``discord.py`` never
  builds an ``opus.Encoder`` for it and ffmpeg does the encoding in its own
  process. libopus is what makes a **``PCMVolumeTransformer``** possible, i.e.
  volume that changes mid-track without restarting the stream; without it the
  volume is a number baked into ffmpeg's command line and a change re-renders the
  source. So it is an upgrade, and it is reported as one rather than as a fault.

The distinction matters because the alternative was to demand a Docker host with
``libopus0`` installed, which is a real change to the deploy for a feature that
turns out not to need it. Docker is still the *better* host — a real ffmpeg and
live volume — and the repository carries that Dockerfile; this module is what
tells the operator which of the two they are on.
"""
from __future__ import annotations

import ctypes.util
import glob
import os
import shutil
from dataclasses import dataclass

from . import config_audit

#: Where a vendored library would live. Checked before the system paths because a
#: repository that ships its own library means it, and because on a host where
#: nothing else is installed this is the only one that exists.
VENDORED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "lib"
)

#: Filenames a vendored or system libopus may have, per platform.
OPUS_FILENAMES: tuple[str, ...] = (
    "libopus.so.0", "libopus.so", "libopus.dylib", "libopus.0.dylib", "opus.dll",
)

#: The globs the debug endpoint has always reported, kept so an operator reading
#: the old output recognises the new one.
OPUS_GLOBS: tuple[str, ...] = (
    "/usr/lib/libopus*.so*",
    "/usr/lib/*-linux-*/libopus*.so*",
    "/usr/lib64/libopus*.so*",
    "/usr/local/lib/libopus*.so*",
    "/lib/libopus*.so*",
    "/lib/*-linux-*/libopus*.so*",
    "/opt/homebrew/lib/libopus*.dylib*",
    "/usr/local/lib/libopus*.dylib*",
)





def ffmpeg_path(*, refresh: bool = False) -> tuple[str | None, str]:
    """``(path, where it came from)`` for the ffmpeg binary this process will use.

    Order is deliberate: an explicit override, then ``PATH``, then the copy
    ``imageio-ffmpeg`` ships. The bundled one is not a fallback of last resort —
    on a host without root it is the *only* ffmpeg that will ever exist, which is
    exactly why it is a dependency of this project rather than an optional extra.
    """
    explicit = config_audit.get("MEOW_FFMPEG")
    if explicit:
        return (explicit, "MEOW_FFMPEG") if os.path.exists(explicit) else (None, "MEOW_FFMPEG is set but not a file")

    system = shutil.which("ffmpeg")
    if system:
        return system, "PATH"

    try:
        import imageio_ffmpeg  # noqa: PLC0415 — optional at import time, required to play

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and os.path.exists(bundled):
            return bundled, "imageio-ffmpeg"
    except Exception as exc:  # pragma: no cover - depends on the install
        return None, f"imageio-ffmpeg could not provide one ({type(exc).__name__})"

    return None, "not found"


def opus_candidates() -> list[str]:
    """Every path a libopus might be at, most specific first."""
    found: list[str] = []
    for name in OPUS_FILENAMES:
        found.append(os.path.join(VENDORED_DIR, name))
    located = ctypes.util.find_library("opus")
    if located:
        found.append(located)
    for pattern in OPUS_GLOBS:
        found.extend(sorted(glob.glob(pattern)))
    return found


def load_opus() -> tuple[bool, str]:
    """Load libopus if it can be, and say whether it now is.

    Both halves are tried and both are silent about failing, because the honest
    answer here is a *capability*, not an exception: without it the player takes
    the baked-volume path and everything still plays. The string returned is
    where it came from, for the one line the boot prints.
    """
    try:
        import discord  # noqa: PLC0415

        if discord.opus.is_loaded():
            return True, "already loaded"

        try:
            discord.opus._load_default()  # noqa: SLF001 - the documented door for this
        except Exception:
            pass
        if discord.opus.is_loaded():
            return True, "default"

        for path in opus_candidates():
            if not path or not os.path.exists(path):
                continue
            try:
                discord.opus.load_opus(path)
            except Exception:
                continue
            if discord.opus.is_loaded():
                return True, path
    except Exception as exc:  # discord itself missing — reported by the caps dataclass
        return False, f"could not ask ({type(exc).__name__})"
    return False, "not found"


def _dave() -> tuple[bool, str | None, str | None]:
    """``(present, version, why_not)`` for the library voice cannot connect without."""
    try:
        import davey  # noqa: PLC0415

        version = getattr(davey, "__version__", None)
        return True, str(version) if version else "installed", None
    except Exception as exc:
        return False, None, f"davey is not importable ({type(exc).__name__}: {exc})"


@dataclass(frozen=True)
class Caps:
    """What this process can do in voice right now. Immutable, so it can be cached."""

    ffmpeg: str | None
    ffmpeg_source: str
    opus: bool
    opus_source: str
    dave: bool
    dave_version: str | None
    dave_problem: str | None

    @property
    def live_volume(self) -> bool:
        """Whether the volume can change mid-track instead of restarting the source."""
        return self.opus

    @property
    def playable(self) -> bool:
        """The two things that are genuinely required: ffmpeg, and DAVE."""
        return bool(self.ffmpeg) and self.dave

    @property
    def quality(self) -> str:
        """One word for a panel: what this host is, rather than a list of flags."""
        if not self.playable:
            return "cannot play"
        if self.ffmpeg_source == "PATH" and self.opus:
            return "host-installed"
        if self.opus:
            return "bundled ffmpeg"
        return "bundled ffmpeg, no live volume"

    def missing(self) -> tuple[str, ...]:
        """Every reason the radio cannot play here, in the order they must be fixed."""
        problems: list[str] = []
        if not self.dave:
            problems.append(
                "**`davey` is missing**, and Discord requires it for any voice "
                "connection at all — the bot cannot even join a channel without it. "
                "Fix: `pip install davey` (it is in `requirements.txt`)."
            )
        if not self.ffmpeg:
            problems.append(
                "**No ffmpeg binary.** Without it there is nothing to decode a "
                "stream. Fix: install ffmpeg on the host, set `MEOW_FFMPEG` to its "
                "path, or install `imageio-ffmpeg`, which ships a static build."
            )
        return tuple(problems)

    def line(self) -> str:
        """The one line a boot prints. Says the answer, not the ingredients."""
        if self.playable:
            extras = []
            if self.opus:
                extras.append("live volume")
            if self.dave_version:
                extras.append(f"DAVE {self.dave_version}")
            tail = f" ({', '.join(extras)})" if extras else ""
            return f"[VOICE] ✅ can play — {self.quality}, ffmpeg {self.ffmpeg_source}{tail}"
        return "[VOICE] ❌ cannot play — " + " · ".join(self.missing())

    def to_dict(self) -> dict:
        """The shape the console and ``/api/voice-debug`` read."""
        return {
            "playable": self.playable,
            "quality": self.quality,
            "ffmpeg": self.ffmpeg,
            "ffmpeg_source": self.ffmpeg_source,
            "opus": self.opus,
            "opus_source": self.opus_source,
            "live_volume": self.live_volume,
            "dave": self.dave,
            "dave_version": self.dave_version,
            "dave_problem": self.dave_problem,
            "missing": list(self.missing()),
        }


#: One probe per process. The libraries cannot appear or vanish while this process
#: runs, so re-asking is a wasted subprocess on every card — but the answer is also
#: cheap to force for a test or a doctor command, which is what ``refresh`` is for.
_cache: Caps | None = None


def probe(*, refresh: bool = False) -> Caps:
    """Ask once, remember, and answer the same thing everywhere."""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    ffmpeg, ffmpeg_source = ffmpeg_path()
    opus, opus_source = load_opus()
    dave, dave_version, dave_problem = _dave()
    _cache = Caps(
        ffmpeg=ffmpeg,
        ffmpeg_source=ffmpeg_source,
        opus=opus,
        opus_source=opus_source,
        dave=dave,
        dave_version=dave_version,
        dave_problem=dave_problem,
    )
    return _cache


def startup_line() -> str:
    """What ``bot.py`` prints at boot, so a broken host is visible in the log."""
    return probe().line()


def build_source(caps: Caps | None = None):
    """Whether the radio should be offered at all, and the sentence when it should not.

    Every command that joins a channel goes through this before it touches voice,
    because the alternative is what the archived cog did: join the channel, send
    the card, and play silence with the only evidence in a container log.
    """
    caps = caps or probe()
    if caps.playable:
        return True, ""
    return False, (
        "🎧 **The radio cannot play on this host right now** — the bot is not joined to "
        "anything.\n\n" + "\n".join(f"- {item}" for item in caps.missing())
    )
