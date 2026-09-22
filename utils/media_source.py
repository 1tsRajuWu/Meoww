"""The stream box: what is playing, and whether this host can open one at all.

Radio is the only playback this bot does, and that single fact decides what is
left here. There is no extraction and no search any more — a station is a URL
that was *fetched before it was written down* (the catalogue and that rule live
in :mod:`cogs.music`) — so nothing in this module turns words into a recording.
What remains is the part that was always about streams rather than about finding
them:

* :class:`Track` — one thing in the queue, with everything the player needs from
  it. ``stream_url`` is the load-bearing field, and for radio it is filled in
  *before* the track is queued: the station is its address, which is why a radio
  session cannot be affected by anything a platform thinks about this host.
* :class:`Probe` / :func:`self_check` — *can this host open a station at all?*
  A capability probe answers a different question (``utils/voice_caps`` says
  whether ffmpeg and libopus exist) and a link scan answers a third (the weekly
  pass in ``utils/media_health`` asks about every URL written down). This one
  asks the whole path at once — this host, this network, one real stream — and it
  is the only check that fails when a deployment has been firewalled off from
  the outside world while everything installed on it is perfectly fine.

The extractor that used to live here — yt-dlp, the client ladder, the provider
order and its refusal memory, the second-provider rescue, the Spotify half, the
proxy that carried both halves of a signed fetch — is archived whole in
``deleted_features/utils/media_source.py``, alongside the suite that covered it
(``deleted_features/tests/test_music_matching.py``). Restoring a resolver means
restoring *that* file and the ``yt-dlp`` line in ``requirements.txt``; nothing in
this one has to move for it.

Two rules carry over from the code that was here, because they were learned
rather than guessed:

* **A check must never raise.** :func:`self_check` is called from a weekly
  background pass, from ``/health`` and from an administrator's command, and a
  diagnostic that can break its callers is worse than no diagnostic.
* **Absent is not the same as fine.** A station that answers ``200`` with an
  HTML error page is not a stream, so the content type is read before the answer
  is believed — the same rule, and the same prefixes, as the link scan's.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import config_audit

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
#: station that quietly goes off the air would report the deployment as broken,
#: which is the check becoming the fault it exists to find.
DEFAULT_PROBE_TARGET = "https://ice1.somafm.com/groovesalad-128-mp3"


@dataclass
class Track:
    """One thing in the queue, with everything needed to play it.

    Radio-shaped by default, because radio is what plays: ``is_radio`` is True
    unless something says otherwise, and the fields the extractor used to fill in
    are gone with it — there is no ``video_id`` to re-resolve by, no search to
    drift, and no provider whose copy this might or might not be.
    """

    title: str
    provider: str = "radio"
    duration: int = 0
    requester: str = "Someone"
    #: Artwork, when a track has any. No station in the catalogue carries one yet,
    #: and every card that reads it already draws without it.
    thumbnail: str = ""
    webpage_url: str = ""
    #: The address to open. For a station this is the catalogue's own URL, set at
    #: queue time, and it is the reason radio keeps playing on a day when every
    #: other way of finding audio has been closed.
    stream_url: str = ""
    is_radio: bool = True
    #: Whether the *source* is already Opus — the one input that can be handed to
    #: Discord untouched. No station here declares it, so every stream is decoded
    #: and re-encoded; the field stays because that is a property of the station
    #: rather than of the player, and the day one serves Opus it is set here and
    #: the player's pass-through path starts being used.
    is_opus: bool = False
    #: The source's own bitrate, when it is known. Radio URLs do not declare one,
    #: so the encoder falls back to what the channel asks for.
    abr: float = 0.0
    resolved_at: float = 0.0
    #: A live stream has no end, which is what keeps the early-EOF guard from
    #: judging one: a station that drops is a fault to report, not a short song.
    live: bool = True

    def describe(self) -> str:
        """A one-line label: title and who asked, for a footer or a log."""
        return f"{self.title} · {self.requester}" if self.requester else self.title


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

    The question is the one a version string cannot answer and the capability
    probe above it cannot either: ffmpeg can be installed, libopus loaded and
    every link in the tree healthy while this host has no route to the outside
    world at all. Radio is what plays here, so *a station this process can open*
    is the whole of what "music works" means.

    Never raises, by design: it is called from a weekly background pass, from an
    administrator's command and from an API route, and a check that can break its
    callers is worse than no check. The answer is either a stream this host can
    read bytes from, or a sentence naming what got in the way.
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
