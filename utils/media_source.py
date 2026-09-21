"""Turning what somebody typed into *one specific recording*, and streaming it.

The two complaints this module exists for are both about **identity**.

**"It plays a different song."** The archived cog kept a search *query* on each
track and, when a stream URL expired, ran that query again. A search is not a
lookup: it is a fresh result set, so the refresh could return a different upload
than the card the member was looking at — and the second search happened
mid-song, where nobody could see it happen. Every track here carries a
**stable identity** — a provider and that provider's own id for the recording —
and it is re-resolved by asking for *that id*. A search runs exactly once, at
request time, and its result is what gets played.

**"It plays a short clip / the wrong version."** YouTube search happily returns
30-second previews, loops, nightcore edits, karaoke tracks and fan re-uploads,
and a duration can match one of those exactly. So the scorer prefers an upload
that *is* the artist's own (their channel, their VEVO/Topic feed, a verified
channel carrying their name), penalises the words that mean "this is a different
recording" — word-bounded, and skipped when the member actually asked for that
word — and refuses anything under :data:`MIN_TRACK_SECONDS` outright, because a
45-second answer to a song request is a preview and not a song.

**And when it cannot tell, it says so instead of guessing.** A pick that is not
clearly ahead of the runner-up comes back as a :class:`Resolution` with
alternatives, and the command offers them: three buttons and a real choice beat
a confident wrong answer, which is the entire class of bug this module was
written to remove.

Format policy is one line, and it is the *quality* fix: ask for audio-only Opus
first (``bestaudio[ext=webm]``), never the muxed video stream. yt-dlp's own
defaults negotiate it; naming it here is what stops a future reshuffle from
quietly falling back to a 360p MP4 that then has to be demuxed.
"""
from __future__ import annotations

import base64
import difflib
import json
import math
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import config_audit

#: How many search results are considered. Five is what the scorer was tuned
#: against and it is one flat extraction, not five.
SEARCH_LIMIT = 5

#: Below this, a result is a preview, a jingle or a loop rather than the song.
MIN_TRACK_SECONDS = 45

#: The gap between the best and the second-best score that counts as *decided*.
#: Scores get more negative as they get better, so the gap is compared as an
#: absolute difference; inside this margin the member is asked.
CONFIDENT_MARGIN = 40.0

#: Audio-only Opus, then audio-only, then whatever exists. The middle clause is
#: what covers a provider that has no webm.
STREAM_FORMAT = "bestaudio[ext=webm][acodec=opus]/bestaudio[acodec=opus]/bestaudio/best"

#: How long a resolved stream URL is trusted. A ``googlevideo`` URL carries its
#: own expiry (hours), so re-resolving a fresh one is pure waste — and the second
#: search was the wrong-song bug.
STREAM_URL_TTL = 1800

#: Words that mean "this is not the recording that was asked for". Disqualifying
#: rather than a nudge: a spot-on duration match must never let a slowed edit
#: outrank the artist's own upload.
JUNK_MARKERS: tuple[str, ...] = (
    "cover", "karaoke", "instrumental", "remix", "mashup", "nightcore",
    "sped up", "speed up", "slowed", "reverb", "chopped", "screwed",
    "8d audio", "bass boost", "bass boosted", "reaction", "tribute",
    "made popular by", "in the style of", "ai cover", "live cover", "loop",
    # An *arrangement* is a different recording too, and this is the one that got
    # through: with YouTube refusing the host, the second provider becomes the
    # first one asked, and SoundCloud's answer to the studio single was
    # "we can't be friends (wait for your love) [Acoustic Version]". Asking for
    # the flavour is still honoured — the marker is skipped when the query
    # contains it — so *"Ariana Grande acoustic"* still finds one.
    "acoustic", "piano version",
)
JUNK_PENALTY = 300.0

#: Words that mean it *is* the real one.
OFFICIAL_MARKERS: tuple[tuple[str, float], ...] = (
    ("official audio", 30.0),
    ("official video", 22.0),
    ("provided to youtube", 18.0),
    ("topic", 34.0),
    ("lyric", 8.0),
)

#: A link to one of these is a stream, not a search: identity comes from the URL.
_YOUTUBE_ID = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/|embed/)|youtu\.be/|music\.youtube\.com/watch\?(?:[^#]*&)?v=)([\w-]{6,})",
    re.IGNORECASE,
)
#: A SoundCloud track's *portable* identity is its **page address**, and the scheme
#: is part of the match: SoundCloud's numeric id is an API id, so an identity
#: without the address turns ``soundcloud.com/<number>`` into a **user** — which is
#: how a rescue reached a 404 on the second provider it was asked for.
_SOUNDCLOUD_ID = re.compile(r"(https?://(?:www\.)?soundcloud\.com/[\w-]+/[\w-]+)", re.IGNORECASE)
_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|&|;|\bfeat\.?\b|\bft\.?\b|\bwith\b|\bx\b)\s*", re.IGNORECASE
)
_SPOTIFY_HOST = re.compile(r"spotify\.(?:com|link)", re.IGNORECASE)
_NUMERIC = re.compile(r"^\d+$")


class MediaError(Exception):
    """A query that could not become a playable track, said in words a member reads.

    ``kind`` is for the code and the operator log; the message is for the person
    who typed the command, and every one of them names something they can do
    instead of restating the failure.
    """

    def __init__(self, kind: str, message: str, *, provider: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        #: Which platform this failure belongs to, when it belongs to one. A
        #: refusal that cannot name the platform that refused is a sentence the
        #: operator can only guess at, and guessing is what sent somebody to
        #: upgrade yt-dlp over a SoundCloud 403.
        self.provider = provider


# --------------------------------------------------------------------------- #
# yt-dlp: availability, options and one extraction
# --------------------------------------------------------------------------- #
def available() -> bool:
    """Whether the extractor is importable at all."""
    try:
        import yt_dlp  # noqa: F401,PLC0415
    except Exception:
        return False
    return True


def version() -> str:
    """The installed yt-dlp version, for the console and the staleness check."""
    try:
        from yt_dlp.version import __version__  # noqa: PLC0415

        return str(__version__)
    except Exception:
        return "unknown"


def cookie_file() -> str | None:
    """A cookies file for the providers that ask for one, if this deploy has it.

    Two shapes, because the two hosts differ: ``YTDL_COOKIES`` is a path that
    already exists (a host with a disk), and ``YTDL_COOKIES_B64`` is the file's
    contents in base64, written to a temp file on first use — which is the only
    way to hand a secret to a host whose filesystem is rebuilt on every deploy.
    """
    path = config_audit.get("YTDL_COOKIES")
    if path and os.path.exists(path):
        return path
    blob = config_audit.get("YTDL_COOKIES_B64")
    if not blob:
        return None
    target = os.path.join(tempfile.gettempdir(), "meow-ytdl-cookies.txt")
    if not os.path.exists(target):
        try:
            raw = base64.b64decode(blob)
            with open(target, "wb") as handle:
                handle.write(raw)
        except Exception as exc:
            print(f"[MUSIC] YTDL_COOKIES_B64 could not be written: {exc}", flush=True)
            return None
    return target


def extractor_args() -> dict:
    """``extractor_args`` from the environment, or ``{}``.

    ``YTDL_PO_TOKEN`` is ``<client>+<token>`` (or a bare token, which is applied
    to the web client) and ``YTDL_VISITOR_DATA`` is the visitor data it must be
    paired with. Both are unset by default: they are what an operator reaches for
    when a datacenter IP has been noticed, and shipping a value nobody can
    reproduce would be worse than shipping none.
    """
    args: dict[str, list[str]] = {}
    token = config_audit.get("YTDL_PO_TOKEN")
    if token:
        client, _, value = token.partition("+")
        if not value:
            client, value = "web", client
        args.setdefault("player_client", []).append(client)
        args.setdefault("po_token", []).append(f"{client}.gvs+{value}" if "gvs" not in value else value)
    visitor = config_audit.get("YTDL_VISITOR_DATA")
    if visitor:
        args.setdefault("visitor_data", []).append(visitor)
    return {"youtube": args} if args else {}


def _base_options(*, flat: bool) -> dict[str, Any]:
    """The options every extraction here shares.

    ``noplaylist`` is on for both: a search result is a list, and a member who
    pastes an album link should get an album resolved deliberately (through the
    Spotify/metadata path), not by accident.
    """
    options: dict[str, Any] = {
        "noplaylist": True,
        "nocheckcertificate": True,
        "quiet": True,
        "no_warnings": True,
        "logtostderr": False,
        "socket_timeout": 15,
        "retries": 2,
        "extractor_retries": 2,
        "ignoreerrors": flat,
    }
    if flat:
        options["extract_flat"] = "in_playlist"
    else:
        options["format"] = STREAM_FORMAT
    cookies = cookie_file()
    if cookies:
        options["cookiefile"] = cookies
    args = extractor_args()
    if args:
        options["extractor_args"] = args
    return options


#: The player-client sets tried, in order, until one answers with a playable
#: stream. The first rung is **yt-dlp's own choice**, which is the only one that
#: negotiates audio-only Opus and therefore the only one that sounds like the
#: source; the rest are the clients that keep answering on a network YouTube has
#: flagged, which a datacenter address always is.
#:
#: The order is measured rather than guessed — and it has to be measured *again*
#: whenever the extractor moves, which is the lesson this tuple now carries. Of the
#: thirteen clients yt-dlp 2026.08 knows, the previous second rung does not appear:
#: ``tv_embedded`` had been removed upstream, so the rung had become a copy of the
#: first one — a full extraction, spent every time the host was already failing, to
#: learn what the first attempt had just said. Nothing anywhere reported it, because
#: an unknown client name is not an error, it is the absence of one.
#: :func:`client_ladder` is the answer to that: a rung this build cannot honour is
#: dropped on the way in, so the next rename costs nothing instead of costing speed
#: on exactly the hosts the ladder exists for.
#:
#: ``android_vr`` leads because it hands back a *direct* audio URL with no signature
#: deciphering (a handful of formats and a ``url``, which is the whole requirement),
#: the ``tv`` family follows because that is where a flagged address still gets
#: through, and the legacy ``android``/``web`` pair is last because it is alive
#: everywhere and sounds the worst — it exposes only a muxed 360p MP4 — so being
#: last means it is reached only when nothing better answered.
CLIENT_LADDER: tuple[tuple[str, ...], ...] = (
    (),
    ("android_vr",),
    ("tv",),
    ("visionos",),
    ("ios",),
    ("android", "web"),
)


def known_clients() -> frozenset[str]:
    """Every player client the installed yt-dlp knows, or an empty set.

    Read from the extractor rather than kept as a list here, because a list here
    is a list that goes stale in silence.
    """
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS  # noqa: PLC0415

        return frozenset(INNERTUBE_CLIENTS)
    except Exception:  # noqa: BLE001 — a version that moved its tables is not a failure
        return frozenset()


def client_ladder() -> tuple[tuple[str, ...], ...]:
    """ :data:`CLIENT_LADDER` with every rung this yt-dlp cannot honour removed.

    An empty answer from :func:`known_clients` means the check could not be made,
    and an uncheckable ladder is tried whole rather than dropped — a host with an
    older extractor should still climb.
    """
    known = known_clients()
    if not known:
        return CLIENT_LADDER
    rungs = [()]
    for rung in CLIENT_LADDER:
        if rung and all(name in known for name in rung):
            rungs.append(rung)
    return tuple(rungs)

#: The client set that last produced a stream, so the next extraction starts where
#: the last one succeeded. A process-level memo and nothing more: it is a reading
#: about *this host's network right now*, so persisting it would be a stale guess
#: about a network that changed — and a restart is exactly when the guess should
#: be thrown away.
_last_good_client: tuple[str, ...] | None = None

#: Whether that extraction had to *climb* to get there, which is a different
#: question from which client answered: a deploy that configured a client of its
#: own answers on the first rung and looks identical to a deploy the provider likes.
#: Only climbing is a statement about the network.
_last_climbed: bool = False


def _attempts(options: dict[str, Any]):
    """The option sets to try for one extraction, best first.

    The options as handed in are always the first attempt, so an operator's
    ``YTDL_PO_TOKEN`` / ``YTDL_VISITOR_DATA`` configuration is respected rather
    than overridden — but it is no longer the *only* attempt, which it used to be:
    a configured token that the provider has stopped honouring disabled every
    fallback, so the one host that had been configured to cope was the one that
    died first.

    A rung the last successful extraction used is moved to the front, so a host
    that has settled on ``android_vr`` asks for it first instead of re-learning it
    on every single song.
    """
    order = list(client_ladder())
    if _last_good_client and _last_good_client in order:
        order.remove(_last_good_client)
        order.insert(0, _last_good_client)
    yield options
    for client in order:
        if not client:
            # The empty rung *is* yt-dlp's own choice, which the caller's options
            # already describe unless an operator configured something else — and
            # that configuration was the first attempt above.
            continue
        attempt = dict(options)
        attempt["extractor_args"] = {"youtube": {"player_client": list(client)}}
        yield attempt


def _has_playable_url(info) -> bool:
    """Whether an extraction produced *one* URL a player can open.

    The distinction that matters and is easy to miss: a result can be full of
    formats and still have nothing directly playable, because the selected format
    is a video/audio *pair* that would need muxing — which is exactly what
    ``bestvideo+bestaudio`` means, and why a client set can answer "47 formats" and
    still be useless to a streaming player.
    """
    if not isinstance(info, dict):
        return False
    if info.get("entries"):
        return bool([entry for entry in info["entries"] if entry])
    return bool(info.get("url"))


def _worth_climbing(exc: Exception) -> bool:
    """Whether a different client set could plausibly answer where this one did not.

    Everything except a *network* failure. A host with no route out will not grow
    one in the three seconds four more extractions cost, and the failures that do
    climb are precisely the ones that vary by client: a bot check, a format the
    client is not offered, a video that one client is told is unavailable.
    """
    return not looks_unreachable(exc)


def ytdl_extract(options: dict, target: str, *, require_url: bool = False):
    """Run one extraction, climbing :data:`CLIENT_LADDER` until something answers.

    ``require_url`` is what turns "the provider replied" into "the provider replied
    with something playable": a metadata extraction does not need a direct URL, a
    *stream* extraction is nothing but one, and a rung that returns a format list
    with no playable entry has not answered the question that was asked.

    When every rung fails the **last** failure is raised, so the message a member
    reads is about the client that got closest rather than the one that was tried
    first. The rung that worked is remembered for the next call.
    """
    global _last_good_client, _last_climbed

    import yt_dlp  # noqa: PLC0415

    last: Exception = MediaError("no_stream", "The provider answered with nothing playable.")
    tried: list[str] = []
    for attempt in _attempts(options):
        client = _client_of(attempt)
        tried.append(client)
        try:
            with yt_dlp.YoutubeDL(attempt) as ytdl:
                info = ytdl.extract_info(target, download=False)
        except Exception as exc:
            last = exc
            if not _worth_climbing(exc):
                raise
            continue
        if require_url and not _has_playable_url(info):
            last = MediaError(
                "no_stream", "The provider gave no playable stream for that recording."
            )
            continue
        climbed = len(tried) > 1
        if climbed and not _last_climbed:
            # A line the *first* time it happens and not on every song: the
            # transition into "this host is being refused by the default client" is
            # the news, and repeating it per track is a log nobody reads.
            print(
                f"[MUSIC] the default client was refused; the extractor settled on "
                f"{client!r}",
                flush=True,
            )
        _last_good_client = _client_tuple(attempt)
        _last_climbed = climbed
        return info

    if len(tried) > 1:
        print(f"[MUSIC] every client set failed ({', '.join(tried)}): {last}", flush=True)
    raise last


def _client_of(options: dict) -> str:
    """A readable name for the client set an attempt uses."""
    args = options.get("extractor_args") or {}
    clients = (args.get("youtube") or {}).get("player_client") or []
    return "+".join(clients) if clients else "default"


def _client_tuple(options: dict) -> tuple[str, ...]:
    """The same shape :data:`CLIENT_LADDER` stores, so the memo can be compared."""
    args = options.get("extractor_args") or {}
    return tuple((args.get("youtube") or {}).get("player_client") or ())


def _looks_like_format_refusal(exc: Exception) -> bool:
    """Whether a failure is the *client set* rather than the network."""
    text = str(exc).lower()
    return "requested format" in text or "not available" in text


def looks_unreachable(exc: Exception) -> bool:
    """Whether a failure is *this host's network*, rather than an answer about the request.

    The distinction is the one thing that keeps the weekly self-check honest, and
    it is the same one :mod:`utils.media_health` already draws for links: a
    provider that says "you are a bot" is a finding, and a DNS failure is not.
    Reporting the second as the first sends somebody to upgrade a package when
    their host has no route out — so an unreachable extractor is *unchecked*, and
    the module that reports it has to be able to tell.
    """
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "urlopen error", "unable to download webpage", "temporary failure in name",
            "name or service not known", "getaddrinfo", "nodename nor servname",
            "network is unreachable", "no route to host", "connection refused",
            "connection reset", "timed out", "timeout", "ssl:", "certificate",
        )
    )


def is_blocked(exc: Exception) -> bool:
    """Recognise a provider refusing this host, without false-positiving.

    Word-bounded on purpose: the archived version matched the bare substring
    ``"bot"``, which is inside ``bootstrap`` and every other innocent word.
    """
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "429", "too many requests", "sign in to confirm", "not a bot",
            "confirm you\u2019re not a bot", "confirm you're not a bot",
            "this video is unavailable in your country", "geo restricted",
        )
    )


# --------------------------------------------------------------------------- #
# Scoring: which result is the song
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    """One search result, before it is trusted."""

    provider: str
    video_id: str
    title: str
    duration: int = 0
    channel: str = ""
    verified: bool = False
    views: int = 0
    webpage_url: str = ""
    live: bool = False
    thumbnail: str = ""

    @property
    def too_short(self) -> bool:
        return bool(self.duration) and self.duration < MIN_TRACK_SECONDS


def similarity(a: str, b: str) -> float:
    """How alike two strings are, 0..1. Case-insensitive."""
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def junk_penalty(title: str, target: str) -> float:
    """Penalty for words meaning "this is a different recording".

    Word-bounded (``discover`` is not a ``cover``) and skipping anything the
    member asked for, so searching for a remix still returns one.
    """
    lowered = (title or "").lower()
    asked = (target or "").lower()
    total = 0.0
    for marker in JUNK_MARKERS:
        if marker in asked:
            continue
        if re.search(rf"\b{re.escape(marker)}\b", lowered):
            total += JUNK_PENALTY
    return total


def clean_artist(artist: str | None) -> str | None:
    """Reduce a credit list to the primary artist, dropping placeholders.

    Providers join collaborators (``Daft Punk, Pharrell Williams``) and mark
    features inline; the upload lives on the primary artist's channel.
    """
    text = (artist or "").strip()
    if not text:
        return None
    primary = _ARTIST_SPLIT.split(text, 1)[0].strip()
    if not primary or primary.lower() in ("unknown", "unknown artist", "various artists", "va"):
        return None
    return primary


def artist_from_query(query: str) -> str | None:
    """Best-effort artist out of an ``Artist - Title`` query."""
    if not query or query.startswith(("http://", "https://")) or " - " not in query:
        return None
    head = query.split(" - ", 1)[0].strip()
    return clean_artist(head if len(head) <= 60 else None)


def artist_strength(candidate: Candidate, artist: str | None) -> int:
    """How confidently this upload belongs to the artist: 0 none, 1 likely, 2 own.

    A channel that *is* the artist — or their VEVO/Topic feed, or a verified
    channel carrying their name — is 2. A channel that merely mentions the name
    (the fan re-upload) is 1, so it can never outrank a real one.
    """
    artist = clean_artist(artist)
    if not artist:
        return 0
    tokens = [word for word in re.split(r"[^\w]+", artist.lower()) if len(word) > 1]
    if not tokens:
        return 0
    joined = "".join(tokens)
    channel = (candidate.channel or "").lower()
    flat = channel.replace(" ", "")
    who = f"{channel} {candidate.channel}".lower()
    if flat in (joined, f"{joined}vevo", f"{joined}topic", f"{joined}-topic"):
        return 2
    if candidate.verified and all(token in who for token in tokens):
        return 2
    if joined in who.replace(" ", ""):
        return 1
    return 0


def score(candidate: Candidate, target: str, expected_sec: int = 0, artist: str | None = None) -> float:
    """Lower is better. One scorer, used by search and by the confidence test."""
    title = candidate.title or ""
    lowered = title.lower()
    blob = f"{lowered} {candidate.channel or ''}".lower()

    sim = similarity(target, title)
    tokens = [word for word in re.split(r"[^\w]+", (target or "").lower()) if len(word) > 2]
    covered = sum(1 for word in tokens if word in blob) / len(tokens) if tokens else 0.0

    official = 0.0
    for marker, weight in OFFICIAL_MARKERS:
        if marker in lowered or (marker == "topic" and marker in (candidate.channel or "").lower()):
            official += weight

    penalty = junk_penalty(lowered, target)
    if "live" not in (target or "").lower() and re.search(r"\blive\b", lowered) and not candidate.live:
        penalty += JUNK_PENALTY

    # Popularity and verification: the canonical upload of a song is almost
    # always the verified artist channel with the largest audience, which is how
    # a re-upload farm is ruled out when the titles look alike.
    audience = 0.0
    if candidate.verified:
        audience -= 20.0
    if candidate.views:
        audience -= min(math.log10(candidate.views + 1) * 6, 54.0)

    # Duration is a tiebreaker, not the deciding vote: an uncapped seconds-delta
    # let any re-upload that matched the album length outrank the artist's own
    # shorter radio edit.
    duration_penalty = 0.0
    if expected_sec > 0 and candidate.duration > 0:
        delta = abs(candidate.duration - expected_sec)
        tolerance = max(8, expected_sec * 0.05)
        if delta > tolerance:
            duration_penalty = min(delta - tolerance, 60.0)
        if delta > 45:
            duration_penalty += 40.0
    if 0 < candidate.duration < MIN_TRACK_SECONDS:
        duration_penalty += 200.0

    strength = artist_strength(candidate, artist)
    strength_bonus = {2: -120.0, 1: -40.0}.get(strength, 0.0)

    return -(sim * 70.0) - (covered * 60.0) - official + audience + duration_penalty + penalty + strength_bonus


@dataclass
class Resolution:
    """A query, resolved: one track, or a shortlist to choose from.

    ``alternatives`` is non-empty when the scorer could not separate the top
    answers — which is the case that used to become a confident wrong answer.
    """

    query: str
    track: Track | None = None
    alternatives: list[Track] = field(default_factory=list)

    @property
    def needs_choice(self) -> bool:
        return self.track is None or bool(self.alternatives)


def pick(
    candidates: list[Candidate],
    target: str,
    expected_sec: int = 0,
    artist: str | None = None,
    *,
    requester: str = "Someone",
    query: str = "",
) -> Resolution:
    """The song, or the shortlist when the scorer is not sure.

    Short results are removed first: a 30-second upload answering a song request
    is a preview, and offering one as an alternative would be offering the bug.
    If *everything* found is short, that is a real answer, reported as one — some
    things genuinely are clips.
    """
    usable = [c for c in candidates if c and (c.video_id or c.webpage_url)]
    reasonable = [c for c in usable if not c.too_short]
    pool = reasonable or usable
    if not pool:
        return Resolution(query=query)

    ranked = sorted(pool, key=lambda c: score(c, target, expected_sec, artist))
    best = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    margin = (score(runner, target, expected_sec, artist) - score(best, target, expected_sec, artist)) if runner else 999.0

    track = to_track(best, requester=requester, query=query, confidence=margin)
    if margin >= CONFIDENT_MARGIN or runner is None:
        return Resolution(query=query, track=track)

    # Not decided: hand back the shortlist and let a person choose. Capped at
    # three because that is what a Discord select renders without scrolling, and
    # because a longer list is not a choice, it is another search.
    shortlist = [track] + [
        to_track(c, requester=requester, query=query, confidence=margin) for c in ranked[1:3]
    ]
    return Resolution(query=query, track=None, alternatives=shortlist)


def to_track(candidate: Candidate, *, requester: str, query: str, confidence: float = 1.0) -> Track:
    """A search result as a queue entry, carrying the identity it was found by."""
    return Track(
        title=candidate.title or query,
        provider=candidate.provider,
        video_id=candidate.video_id,
        duration=candidate.duration,
        requester=requester,
        thumbnail=candidate.thumbnail,
        webpage_url=candidate.webpage_url or candidate.thumbnail,
        search_query=query,
        expected_artist=artist_from_query(query),
        confidence=confidence,
        live=candidate.live,
    )


# --------------------------------------------------------------------------- #
# The track
# --------------------------------------------------------------------------- #
@dataclass
class Track:
    """One thing in the queue, with everything needed to play it *again*.

    ``video_id`` is the load-bearing field: it is what makes a re-resolution a
    lookup instead of a second search.
    """

    title: str
    provider: str = "youtube"
    video_id: str = ""
    duration: int = 0
    requester: str = "Someone"
    thumbnail: str = ""
    webpage_url: str = ""
    stream_url: str = ""
    is_radio: bool = False
    is_opus: bool = False
    abr: float = 0.0
    resolved_at: float = 0.0
    search_query: str = ""
    expected_artist: str | None = None
    confidence: float = 1.0
    live: bool = False
    #: How a stream URL started, so a truncated stream can name what it was.
    engine: str = ""
    #: The provider that actually handed over the stream. Usually the same as
    #: ``provider``; different when the platform that *found* the recording would not
    #: stream it and another one would.
    found_on: str = ""
    #: True when that provider is not the one at the front of this host's search
    #: order — i.e. the recording had to be fetched somewhere other than where the
    #: obvious answer lives. The **only** thing that decides whether the card says so.
    moved: bool = False

    @property
    def source_note(self) -> str:
        """Where this came from, in the words a **member** needs — or ``""``.

        One clause, and only when it is news. A track fetched from the platform the
        bot usually uses is the normal case and says nothing; one fetched elsewhere
        says so, because an unmentioned substitution is how a member wonders whether
        they are listening to the right upload.

        Written for somebody who *asked for a song* rather than for whoever runs the
        bot: no command, no setting, nothing to configure. The deployment's own
        reading is on the operator's surfaces, where somebody can act on it.
        """
        if not self.moved or not self.found_on:
            return ""
        return f"the usual source would not stream it, so this one came from {self.found_on}"

    @property
    def identity(self) -> str:
        """The stable name of this recording, or an empty string for a radio."""
        if self.is_radio:
            return ""
        return f"{self.provider}:{self.video_id}" if self.video_id else ""

    @property
    def fresh(self) -> bool:
        """Whether the resolved URL is recent enough to reuse without asking again."""
        return bool(self.stream_url) and (time.monotonic() - self.resolved_at) < STREAM_URL_TTL

    def describe(self) -> str:
        """A one-line label: artist and title, for a footer or a log."""
        return f"{self.title} · {self.requester}" if self.requester else self.title


@dataclass
class Stream:
    """A playable URL and what the provider says is behind it."""

    url: str
    is_opus: bool = False
    abr: float = 0.0
    ext: str = ""
    codec: str = ""
    duration: int = 0
    title: str = ""
    webpage_url: str = ""
    live: bool = False


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #
@dataclass
class Parsed:
    """What a query is, before anything is fetched."""

    #: ``link`` (an address to one recording), ``spotify`` (metadata to look up),
    #: or ``search`` (words).
    kind: str
    provider: str = "youtube"
    video_id: str = ""
    url: str = ""
    text: str = ""

    @property
    def is_search(self) -> bool:
        return self.kind == "search"


def is_spotify(query: str) -> bool:
    return bool(_SPOTIFY_HOST.search(query or ""))


def parse(query: str) -> Parsed:
    """Classify a query without touching the network."""
    text = (query or "").strip()
    if not text:
        return Parsed(kind="search", text="")
    if is_spotify(text):
        return Parsed(kind="spotify", provider="spotify", url=text, text=text)

    match = _YOUTUBE_ID.search(text)
    if match:
        video_id = match.group(1)
        return Parsed(
            kind="link",
            provider="youtube",
            video_id=video_id,
            url=text,
            # The identity came out of the URL, so a refresh asks for this exact
            # recording rather than searching the address as a phrase.
            text=f"https://www.youtube.com/watch?v={video_id}",
        )
    if "soundcloud.com/" in text.lower():
        matched = _SOUNDCLOUD_ID.search(text)
        return Parsed(
            kind="link",
            provider="soundcloud",
            video_id=matched.group(1) if matched else "",
            url=text,
            text=text,
        )
    if text.startswith(("http://", "https://")):
        return Parsed(kind="link", provider="url", url=text, text=text)
    return Parsed(kind="search", text=text)


def watch_url(track: Track) -> str:
    """The address that names *this recording again*, for a refresh.

    A YouTube id is turned back into a watch URL — the one form that cannot drift
    — and anything else falls back to the address it was found at, and only then
    to the words it was searched with.
    """
    if track.video_id and track.provider == "youtube":
        return f"https://www.youtube.com/watch?v={track.video_id}"
    if track.video_id and track.provider == "audius":
        # The id is the drift-proof form here too, and it is the address the stream
        # endpoint takes — no numeric-id trap like SoundCloud's below.
        return track.webpage_url or f"https://audius.co/tracks/{track.video_id}"
    if track.video_id and track.provider == "soundcloud":
        if track.video_id.startswith("http"):
            return track.video_id
        # An id rather than a page: the page it came from is the only addressable
        # form, and a numeric id here is an API id, not a path.
        if track.webpage_url and not _NUMERIC.match(track.webpage_url):
            return track.webpage_url
    return track.webpage_url or track.search_query or track.title


# --------------------------------------------------------------------------- #
# Search and resolve
# --------------------------------------------------------------------------- #
def _candidate_from_entry(entry: dict) -> Candidate | None:
    """One flat search entry in this module's own shape."""
    if not isinstance(entry, dict):
        return None
    url = entry.get("url") or entry.get("webpage_url") or ""
    video_id = str(entry.get("id") or "")
    provider = "youtube"
    if "soundcloud" in str(entry.get("ie_key") or "").lower() or "soundcloud.com" in url:
        provider = "soundcloud"
        # Identity by page address, not by id. A SoundCloud search returns the
        # numeric track id *and* the page it lives on, and only the second one is
        # addressable — the first is an API id that resolves as a user.
        page = str(entry.get("webpage_url") or "")
        if not page and "soundcloud.com" in url:
            page = url
        if page.startswith("http"):
            video_id = page
    title = entry.get("title") or ""
    if not title and not video_id:
        return None
    return Candidate(
        provider=provider,
        video_id=video_id,
        title=title,
        duration=int(entry.get("duration") or 0),
        channel=entry.get("channel") or entry.get("uploader") or "",
        verified=bool(entry.get("channel_is_verified")),
        views=int(entry.get("view_count") or 0),
        webpage_url=entry.get("webpage_url") or url or (f"https://www.youtube.com/watch?v={video_id}" if provider == "youtube" else url),
        live=str(entry.get("live_status") or "") in ("is_live", "is_upcoming"),
        thumbnail=entry.get("thumbnail") or "",
    )


#: Every provider a *search* can run against, as ``(name, yt-dlp prefix)``.
#:
#: The declaration order is the default, and :func:`source_order` is what actually
#: decides — because the right order is a property of **this host's network**
#: rather than of the product, and the failure that order exists for is invisible
#: from everywhere except the host.
SEARCH_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("youtube", "ytsearch"),
    ("soundcloud", "scsearch"),
    # An empty prefix: this one is not yt-dlp's search at all, and that is the whole
    # reason it is here. See :func:`audius_search`.
    ("audius", ""),
)

#: A provider's name as a person writes it, for a sentence somebody has to read.
PROVIDER_LABELS: dict[str, str] = {
    "youtube": "YouTube",
    "soundcloud": "SoundCloud",
    "audius": "Audius",
}

#: Audius: an open catalogue whose API hands over a **direct audio URL** and asks
#: for no credential of any kind.
#:
#: It is here because of a fact about networks rather than about music. Two
#: providers is a choice between two doors, and both of the others are hostile to a
#: datacenter address: YouTube answers one with a bot check, SoundCloud answers one
#: with a 403, and an operator who has configured neither cookies nor a PO token has
#: nowhere left to go. Audius answers, and it answers with the URL itself — no player
#: client, no signature deciphering, no check to pass — which makes it the one
#: provider a flagged host can reach *without* anybody being told to go and fetch a
#: credential. It is third rather than first because a search here returns the
#: catalogue's own uploads, which is a narrower answer than YouTube's for a
#: mainstream request; being third means it is reached exactly when the other two
#: fail, which is the case it exists for.
AUDIUS_API = "https://api.audius.co/v1"
AUDIUS_TIMEOUT = 12.0
#: The API asks callers to name themselves. It is a courtesy rather than a
#: credential, and it is the same string the bot's own user-agent carries.
AUDIUS_APP = "meow-discord-bot"
AUDIUS_USER_AGENT = "MeowBot/1.0 (+https://meow-bot.pages.dev)"

#: ``YTDL_SOURCE_ORDER`` — the operator's own order, comma-separated. A deployment
#: whose network is refused by one platform can say so once instead of paying for
#: the discovery on every song.
SOURCE_ORDER_SETTING = "YTDL_SOURCE_ORDER"

#: How long a provider that refused this host is sent to the back of the order.
#: Not forever, and not per request either: long enough that somebody typing four
#: songs in a row does not pay four times for one refusal, short enough that a
#: provider which relents is noticed in the same session.
REFUSAL_COOLDOWN = 900.0

#: ``provider -> the monotonic moment it refused``, for this process. Deliberately
#: not persisted: it is a reading about the network *now*, and a restart is exactly
#: when that reading should be thrown away.
_refused_at: dict[str, float] = {}

#: The provider that last produced a *playable* track, so the next search begins
#: where the last one succeeded. This is what turns "refused, rescued, played" into
#: "just played" for everyone after the first song.
_last_good_source: str = ""


def note_refusal(provider: str) -> None:
    """Record that a provider would not answer this host, for :data:`REFUSAL_COOLDOWN`."""
    if not provider:
        return
    _refused_at[provider] = time.monotonic()
    print(f"[MUSIC] {provider} refused this host; other sources move up", flush=True)


def note_source(provider: str) -> None:
    """Record that a provider produced a playable track."""
    global _last_good_source
    if provider and provider != _last_good_source:
        if _last_good_source:
            print(f"[MUSIC] searches now prefer {provider}", flush=True)
        _last_good_source = provider
    _refused_at.pop(provider, None)


def _refusing(provider: str) -> bool:
    """Whether this provider refused this host recently."""
    when = _refused_at.get(provider)
    return bool(when) and (time.monotonic() - when) < REFUSAL_COOLDOWN


def source_order() -> list[tuple[str, str]]:
    """The provider order for *this host*, right now — and why it is not a constant.

    Three inputs, in order of authority:

    1. **The operator's** ``YTDL_SOURCE_ORDER`` if it names anything, because a
       deployment knows its own network better than this module can infer it.
    2. **The provider that last answered**, which is the whole point: a host
       YouTube refuses should search SoundCloud on the *first* attempt of the next
       song rather than discover the refusal again. This is what makes the fix
       permanent rather than a per-song rescue.
    3. **The declaration order** for everything else.

    A provider that refused recently is *moved to the back*, never dropped. A
    refusal is a statement about the last few minutes, and a list that forgets a
    provider cannot recover on its own when the refusal lifts.
    """
    prefixes = dict(SEARCH_PROVIDERS)
    names = [name for name, _prefix in SEARCH_PROVIDERS]
    configured = [
        part.strip().lower()
        for part in (config_audit.get(SOURCE_ORDER_SETTING) or "").split(",")
        if part.strip()
    ]
    order = [name for name in configured if name in prefixes]
    order += [name for name in names if name not in order]
    if _last_good_source in order:
        order.remove(_last_good_source)
        order.insert(0, _last_good_source)
    refusing = [name for name in order if _refusing(name)]
    willing = [name for name in order if name not in refusing]
    return [(name, prefixes[name]) for name in willing + refusing]


def _audius_json(path: str, params: dict[str, Any]) -> Any:
    """One GET against the open catalogue, as JSON.

    ``urllib`` rather than a new dependency: this is three lines of request and one
    of parsing, and a bot that already ships yt-dlp does not need a second HTTP
    client to ask one endpoint.
    """
    query = urllib.parse.urlencode({"app_name": AUDIUS_APP, **params})
    request = urllib.request.Request(
        f"{AUDIUS_API}{path}?{query}", headers={"User-Agent": AUDIUS_USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=AUDIUS_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _candidate_from_audius(row: dict) -> Candidate | None:
    """One catalogue row as a candidate, with the id that re-resolves it.

    ``title`` carries the artist the way a YouTube upload title does, so the same
    scorer sees the same shape from every provider; ``channel`` keeps it separately
    because that is what the artist check reads.
    """
    track_id = str(row.get("id") or "").strip()
    title = str(row.get("title") or "").strip()
    if not track_id or not title:
        return None
    user = row.get("user") if isinstance(row.get("user"), dict) else {}
    artist = str(user.get("name") or "").strip()
    artwork = row.get("artwork") if isinstance(row.get("artwork"), dict) else {}
    permalink = str(row.get("permalink") or "").strip()
    return Candidate(
        provider="audius",
        video_id=track_id,
        title=f"{artist} - {title}" if artist else title,
        duration=int(row.get("duration") or 0),
        channel=artist,
        verified=bool(user.get("is_verified")),
        views=int(row.get("play_count") or 0),
        webpage_url=permalink or f"https://audius.co/tracks/{track_id}",
        thumbnail=str(artwork.get("480x480") or artwork.get("150x150") or ""),
    )


def audius_search(text: str, *, limit: int = SEARCH_LIMIT) -> list[Candidate]:
    """The catalogue's own search, in the shape every other provider returns.

    A failure here is raised as a :class:`MediaError` naming the platform, because
    the chain's job is to move on to the next door and it can only do that if it
    knows which door did not open.
    """
    try:
        payload = _audius_json("/tracks/search", {"query": text, "limit": limit})
    except Exception as exc:  # noqa: BLE001 — a provider that fails is a provider to skip
        # Recorded as a refusal so the *order* learns: a catalogue that is down should
        # be asked last rather than first, and it should come back on its own when it
        # returns, which is what the cooldown does and a permanent drop would not.
        note_refusal("audius")
        raise MediaError(
            "provider_failed",
            f"Audius did not answer ({type(exc).__name__}).",
            provider="audius",
        ) from exc
    rows = payload.get("data") if isinstance(payload, dict) else None
    found = [_candidate_from_audius(row) for row in rows or [] if isinstance(row, dict)]
    return [candidate for candidate in found if candidate is not None]


def _audius_stream(track: Track) -> Stream:
    """A direct audio URL for one catalogue track, and the answer is checked.

    The endpoint redirects to a CDN file; ffmpeg follows the redirect, so the API
    address is the URL to keep — the CDN one is the kind that expires. It is probed
    with a one-byte range request first, because "this provider will hand it over"
    is the promise :func:`stream` makes, and an answer that turns out to be an error
    page has to reach the chain as a failure so the next door is tried.
    """
    if not track.video_id:
        raise MediaError(
            "no_stream", "That catalogue entry has no id, so it cannot be played.",
            provider="audius",
        )
    url = (
        f"{AUDIUS_API}/tracks/{urllib.parse.quote(track.video_id)}/stream"
        f"?app_name={AUDIUS_APP}"
    )
    request = urllib.request.Request(
        url, headers={"User-Agent": AUDIUS_USER_AGENT, "Range": "bytes=0-1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=AUDIUS_TIMEOUT) as response:
            kind = str(response.headers.get("Content-Type") or "")
    except Exception as exc:  # noqa: BLE001 — an unanswered promise is a reason to ask elsewhere
        raise MediaError(
            "no_stream",
            f"Audius would not hand over that recording ({type(exc).__name__}).",
            provider="audius",
        ) from exc
    if not kind.lower().startswith("audio/"):
        raise MediaError(
            "no_stream",
            f"Audius answered with {kind or 'no content type'} rather than audio.",
            provider="audius",
        )
    return Stream(
        url=url,
        is_opus=False,
        ext="mp3",
        codec="mp3",
        duration=track.duration,
        title=track.title,
        webpage_url=track.webpage_url,
    )


def search(
    text: str,
    *,
    limit: int = SEARCH_LIMIT,
    provider: str | None = None,
) -> list[Candidate]:
    """Flat-search a phrase. Metadata only: no stream URL, so this is the fast half.

    ``provider`` names the one to use; by default it is the first willing entry in
    :func:`source_order`, so the caller does not have to know which platforms this
    host can still reach. A provider that refuses this host is recorded and the
    search returns nothing — the *caller* decides whether to move on, because only
    it knows whether it has an answer already.
    """
    if provider is None:
        order = source_order()
        if not order:
            return []
        provider = order[0][0]
    if provider == "audius":
        # Not a yt-dlp search at all: no extractor, no player client, no check. The
        # provider that exists for the hosts the other two refuse is asked directly.
        return audius_search(text, limit=limit)
    prefix = dict(SEARCH_PROVIDERS).get(provider)
    if not prefix:
        return []
    target = f"{prefix}{limit}:{text}"
    try:
        info = ytdl_extract(_base_options(flat=True), target)
    except Exception as exc:  # noqa: BLE001 — which provider failed is the caller's question
        if is_blocked(exc):
            note_refusal(provider)
        raise
    if not info:
        return []
    entries = info.get("entries") if isinstance(info, dict) else None
    if entries is None:
        entries = [info]
    found = [_candidate_from_entry(entry) for entry in entries or []]
    return [candidate for candidate in found if candidate is not None]


def stream(track: Track, *, target: str | None = None) -> Stream:
    """Resolve a playable URL **for this recording**.

    ``target`` defaults to the track's own identity: its id, or the address it
    was found at for a provider whose ids are not portable. It is never the
    search query unless there is nothing else, which is the difference between
    re-resolving a song and searching for one again.
    """
    if track.provider == "audius" and not target:
        # The one provider that does not go through the extractor, so there is no
        # ladder to climb and nothing to negotiate: the id *is* the address.
        return _audius_stream(track)
    address = target or watch_url(track) or track.title
    # ``require_url`` is the difference between "the provider replied" and "the
    # provider replied with something a player can open", and it is what makes the
    # client ladder climb: a rung that answers with a format *list* and no direct
    # URL has not answered this question.
    info = ytdl_extract(_base_options(flat=False), address, require_url=True)
    if not info:
        raise MediaError("no_stream", "The provider answered with nothing playable.")
    if isinstance(info, dict) and info.get("entries"):
        entries = [entry for entry in info["entries"] if entry]
        if not entries:
            raise MediaError("no_stream", "That link contains nothing to play.")
        info = entries[0]
    url = info.get("url")
    if not url:
        raise MediaError("no_stream", "The provider gave no playable stream for that recording.")

    codec = (info.get("acodec") or "").lower()
    ext = (info.get("ext") or "").lower()
    try:
        abr = float(info.get("abr") or 0)
    except (TypeError, ValueError):
        abr = 0.0
    return Stream(
        url=url,
        is_opus=codec == "opus" or (ext == "webm" and codec != "mp4a"),
        abr=abr,
        ext=ext,
        codec=codec,
        duration=int(info.get("duration") or 0),
        title=info.get("title") or track.title,
        webpage_url=info.get("webpage_url") or track.webpage_url,
        live=str(info.get("live_status") or "") in ("is_live", "is_upcoming"),
    )


def apply_stream(track: Track, resolved: Stream) -> Track:
    """Record a resolved stream on the track, in place, and return it."""
    track.stream_url = resolved.url
    track.is_opus = resolved.is_opus
    track.abr = resolved.abr
    if resolved.duration and not track.duration:
        track.duration = resolved.duration
    if resolved.webpage_url:
        track.webpage_url = resolved.webpage_url
    if resolved.title and track.is_radio is False and not track.title:
        track.title = resolved.title
    if resolved.live:
        track.live = True
    track.resolved_at = time.monotonic()
    return track


# --------------------------------------------------------------------------- #
# The rescue: the same song, somewhere else
# --------------------------------------------------------------------------- #
# YouTube is the one provider that refuses a datacenter host, and it is also the
# one this resolver prefers — so the failure mode nobody can see from a laptop is
# "every song dies on the host and works for whoever tests it". The archived cog
# had no answer to that. This is the answer: when the provider that *found* a
# track will not stream it, the same artist and title are looked for on a second
# provider that will.
#
# SoundCloud is the second provider because it is one this resolver already
# understands end to end (``parse``, ``_candidate_from_entry`` and the scorer all
# know it) and because it is markedly harder to refuse a datacenter IP on. The
# result is scored against the *track's own* title **and its known duration**,
# which is the strongest signal available at this point: the recording was already
# identified when it was queued, so this is a lookup for that recording rather
# than another search.

#: The providers a track can be rescued *from*, which is the question the memo
#: below is asked in reverse: a rescue means "find this on a provider other than
#: the one that failed", so a track already on the only fallback has nowhere to go
#: to and asking again would just be the same host refusing twice.
RESCUABLE_PROVIDERS: tuple[str, ...] = ("youtube",)

#: How close a rescue candidate's *title* must be when the recording's length is
#: not known. Only the fallback of the fallback: a candidate that matches neither
#: the length nor the name is not this recording, and the honest answer is then
#: silence — the scorer's own margin cannot decide that, because it compares
#: candidates to *each other* and one candidate always looks confident on its own.
RESCUE_TITLE_FLOOR = 0.6


def _rescue_candidates(text: str, limit: int = SEARCH_LIMIT, *, exclude: str = "") -> list[Candidate]:
    """A flat search of one phrase on every provider this host can still reach.

    ``exclude`` is the provider that just failed — asking it again is the definition
    of not trying something else.
    """
    found: list[Candidate] = []
    for name, _prefix in source_order():
        if name == exclude:
            continue
        try:
            found.extend(search(text, limit=limit, provider=name))
        except Exception as exc:  # noqa: BLE001 — one provider refusing is not the end
            print(f"[MUSIC] rescue search on {name} failed: {type(exc).__name__}", flush=True)
            continue
    return found


#: How far a rescue candidate's own length may be from the recording's before it is
#: a different recording. Proportional with a floor, because providers round and
#: truncate: 12% of a three-minute song is 21 seconds and 12% of a nine-minute one
#: is 65, which is the right shape — but a short track should not be gated by a
#: 5-second window, so the floor takes over below about three minutes.
RESCUE_DURATION_SLACK = 0.12
RESCUE_DURATION_FLOOR = 25.0


def _agrees_on_length(candidate: Candidate, expected: float) -> bool:
    """Whether a candidate is the same recording by its own length.

    The one signal a rescue has that a search does not: the recording was already
    identified when it was queued, so its length is a fact about the answer rather
    than a hint about the question.
    """
    if not expected or not candidate.duration or candidate.too_short:
        return False
    return abs(candidate.duration - expected) <= max(
        RESCUE_DURATION_FLOOR, expected * RESCUE_DURATION_SLACK
    )


def rescue(track: Track, *, requester: str | None = None) -> Track | None:
    """Find the *same recording* on another provider, resolved and ready to play.

    Returns a playable :class:`Track`, or ``None`` when nothing comparable was
    found — in which case the caller keeps the original failure, because a
    substitution nobody asked for is a worse answer than an honest refusal.

    **The acceptance rule is different from a search's on purpose.** A search asks
    "which of these did they mean?" and must refuse to guess; a rescue asks "which
    of these *is* the recording already chosen?" — a question with a hard answer,
    because the length is known. So a candidate is accepted when its length agrees
    with the recording's, and the scorer only decides the *order* among the ones
    that agree. Without this the rescue inherited the search's confidence margin
    and returned nothing for a four-way tie, which is silence — the exact complaint
    a rescue exists to answer. A candidate that agrees on nothing falls back to the
    scorer's own confident pick, and if there is not one, ``None``.

    The returned track's ``title`` is the original one and its ``search_query`` is
    the original query, so the card names what the member asked for while the
    ``webpage_url`` and ``provider`` name where it actually played. The caller is
    expected to *say* that it substituted: a silent swap is the wrong-song bug
    wearing a different hat.
    """
    if track.is_radio or track.provider not in RESCUABLE_PROVIDERS:
        return None
    text = " ".join(
        part for part in (track.expected_artist or "", track.title) if part
    ).strip() or track.search_query
    if not text:
        return None
    candidates = _rescue_candidates(text, exclude=track.provider)
    expected = float(track.duration or 0)
    agreeing = [c for c in candidates if _agrees_on_length(c, expected)]
    pool = agreeing
    if not pool and not expected:
        # No length was ever recorded for this track (a live stream, a provider that
        # does not report one), so the name is the only gate there is. A *known*
        # length and no candidate that agrees with it stays empty on purpose: the
        # uploads left are the live version and the extended mix, and preferring one
        # of those over silence is how a member gets played a different recording.
        pool = [c for c in candidates if similarity(c.title, track.title) >= RESCUE_TITLE_FLOOR]
    resolution = pick(
        pool,
        track.title,
        int(expected),
        track.expected_artist or artist_from_query(track.search_query or track.title),
        requester=requester or track.requester,
        query=track.search_query or track.title,
    )
    alternative = resolution.track
    if alternative is None and pool:
        # The gap between the top two agreeing candidates was inside the margin, so
        # the scorer handed back a shortlist. Every entry on it agrees on length, so
        # the first one is the recording rather than a guess — and refusing here
        # would be refusing to answer a question that already has an answer.
        alternative = resolution.alternatives[0]
    if alternative is None:
        return None
    # A rescue that cannot itself be streamed is not a rescue. This is where the
    # ladder matters most: the second provider is being asked precisely because the
    # first one would not answer.
    try:
        apply_stream(alternative, stream(alternative))
    except MediaError:
        return None
    alternative.title = track.title
    alternative.duration = alternative.duration or track.duration
    alternative.engine = track.engine
    return alternative


# --------------------------------------------------------------------------- #
# The chain: search, and if this host cannot stream what was found, search again
# --------------------------------------------------------------------------- #
# A *search* has the same problem a rescue does and used to have no answer to it:
# the order the providers are tried in was a constant, so a host YouTube refuses
# found nothing, and the member was told the provider had refused *the host* — a
# true sentence about the deployment shown to somebody who can do nothing with it.
#
# So the order is asked, not assumed (:func:`source_order`), and a provider that
# cannot stream what it found is followed by the next one **for the same query**:
# a member typing `Billie Eilish BITTERSUITE` gets the recording, from whichever
# platform will hand it over. Which one that was is said on the card, in one clause,
# and only when it is not the obvious answer.


def _chain(text: str, *, requester: str, expected_sec: int = 0, artist: str | None = None):
    """The query, resolved against the providers in turn. Returns ``(Resolution, provider)``.

    Raises the *last* failure if every provider failed, so the message a member
    reads is about the attempt that got furthest rather than the first one tried.
    """
    order = source_order()
    if not order:
        raise MediaError("extractor_missing", "This build knows of no source to search.")
    last: Exception = MediaError("not_found", f"Nothing matched **{text}**.")
    refusal: tuple[str, Exception] | None = None
    for index, (provider, _prefix) in enumerate(order):
        try:
            candidates = search(text, provider=provider)
        except Exception as exc:  # noqa: BLE001 — a provider that fails is a provider to skip
            last = exc
            if is_blocked(exc):
                note_refusal(provider)
                refusal = refusal or (provider, exc)
            continue
        if not candidates:
            continue
        resolution = pick(
            candidates, text, expected_sec, artist, requester=requester, query=text
        )
        if resolution.track is None:
            # A shortlist is a real answer: the member is being asked which of these,
            # and there is nothing to stream yet, so the next provider is not asked.
            return resolution, provider
        if resolution.track.duration and resolution.track.duration < MIN_TRACK_SECONDS \
                and not resolution.track.live:
            last = MediaError(
                "too_short",
                f"The only matches for **{text}** are clips under {MIN_TRACK_SECONDS} seconds. "
                "Try `Artist - Title`, or paste a link.",
            )
            continue
        try:
            apply_stream(resolution.track, stream(resolution.track))
        except Exception as exc:  # noqa: BLE001 — the next provider is the answer to this
            last = exc
            if is_blocked(exc) or _looks_like_format_refusal(exc):
                note_refusal(provider)
            if is_blocked(exc):
                refusal = refusal or (provider, exc)
            continue
        resolution.track.found_on = provider
        resolution.track.moved = index > 0
        note_source(provider)
        return resolution, provider
    # Which failure is the story, and why it is not simply "the last one".
    #
    # The order is a *preference*, so the provider tried last is the one this host
    # likes least — the one whose failure is least representative of the search.
    # And :func:`source_order` deliberately moves a refusing provider to the back,
    # so "the last failure" is *usually* a refusal: a SoundCloud refusal would be
    # reported as YouTube refusing, and the member's card would blame a platform
    # that had not been asked yet. So a refusal is preferred when there is one — it
    # is a fact about the deployment, which is the kind somebody can act on — and it
    # is raised **named**, because "a provider refused this host" without saying
    # which one is a sentence with no next step in it.
    if refusal is not None:
        provider, exc = refusal
        label = PROVIDER_LABELS.get(provider, provider)
        raise MediaError(
            "blocked",
            f"**{label}** is refusing this host's address right now",
            provider=provider,
        ) from exc
    if isinstance(last, MediaError):
        raise last
    raise MediaError(
        "no_stream", f"No source could hand over **{text}** ({type(last).__name__})."
    )


def _spotify_tracks(query: str) -> list[dict]:
    """Delegates to the Spotify resolver, which needs no API key.

    Imported here rather than at module scope so that a deployment without the
    resolver still imports this module (and therefore the cog).
    """
    from utils.spotify import resolve_spotify  # noqa: PLC0415

    _, tracks = resolve_spotify(query)
    return tracks or []


def resolve(query: str, *, requester: str = "Someone") -> Resolution:
    """Everything a command needs: the track, or a shortlist, or a raised reason.

    A link is one extraction and one identity — no search, no scoring, nothing to
    be wrong. Words are a search and a score. A Spotify address is metadata and
    then a *constrained* search, because the provider has no stream of its own.
    """
    if not available():
        raise MediaError(
            "extractor_missing",
            "The audio extractor (`yt-dlp`) is not installed on this host, so nothing can be searched.",
        )
    parsed = parse(query)

    if parsed.kind == "spotify":
        return _resolve_spotify(query, requester=requester)

    if parsed.kind == "link":
        return _resolve_link(parsed, query=query, requester=requester)

    if not parsed.text:
        raise MediaError("empty", "Give me something to search for.")

    # The chain: the providers this host can still reach, in the order that is right
    # for it, until one both *finds the recording* and can hand it over.
    resolution, _provider = _chain(
        parsed.text, requester=requester, artist=artist_from_query(parsed.text)
    )
    if resolution.track is None and not resolution.alternatives:
        raise MediaError("not_found", f"Nothing usable matched **{parsed.text}**.")
    return resolution


def _resolve_link(parsed: Parsed, *, query: str, requester: str) -> Resolution:
    """An address as a track: one identity, and a second provider only if it must.

    A link is the one case where the *first* provider is not a preference but a
    fact — the member named a recording on a platform — so the identity comes from
    the address and is not searched for. What is asked of another provider is the
    *stream*: a YouTube link this host may not fetch is still one specific song, and
    by the time the fetch fails its title and length are known, which is exactly
    what :func:`rescue` needs.
    """
    try:
        info = ytdl_extract(_base_options(flat=False), parsed.text)
    except Exception as exc:  # noqa: BLE001 — a refusal here is a reason to ask elsewhere
        if is_blocked(exc):
            note_refusal(parsed.provider)
        raise
    if not info:
        raise MediaError("not_found", "That link could not be read — it may be private or gone.")
    if isinstance(info, dict) and info.get("entries"):
        entries = [entry for entry in info["entries"] if entry]
        if not entries:
            raise MediaError("not_found", "That link contains nothing to play.")
        info = entries[0]
    candidate = _candidate_from_entry(info if isinstance(info, dict) else {})
    if candidate is None:
        raise MediaError("not_found", "That link did not describe anything playable.")
    if candidate.too_short and not candidate.live:
        raise MediaError(
            "too_short",
            f"That is a **{candidate.duration}-second** recording, which is a clip rather than a song.",
        )
    track = to_track(candidate, requester=requester, query=query, confidence=999.0)
    try:
        apply_stream(track, stream(track, target=parsed.text))
        track.found_on = track.provider
        note_source(track.provider)
        return Resolution(query=query, track=track)
    except Exception as exc:  # noqa: BLE001 — the identity is known, so another provider can be asked
        print(
            f"[MUSIC] {parsed.provider} would not stream this link "
            f"({type(exc).__name__}); asking another provider",
            flush=True,
        )
        if is_blocked(exc) or _looks_like_format_refusal(exc):
            note_refusal(parsed.provider)
    rescued = rescue(track, requester=requester)
    if rescued is None:
        # Nothing anywhere could play it, so the original failure is the answer —
        # raising a "not found" here would blame the member's link for a refusal.
        raise MediaError(
            "no_stream",
            "That recording could not be streamed from any source this host can reach.",
        )
    rescued.moved = True
    note_source(rescued.provider)
    return Resolution(query=query, track=rescued)


def _resolve_spotify(query: str, *, requester: str) -> Resolution:
    """A Spotify address as tracks: metadata from them, the recording from search."""
    tracks = _spotify_tracks(query)
    if not tracks:
        raise MediaError(
            "spotify_unavailable",
            "Could not read that Spotify link — it may be private, or the resolver could not reach it.",
        )
    first = tracks[0]
    expected = int((first.get("duration_ms") or 0) / 1000)
    search_text = first.get("query") or first.get("title") or query
    artist = clean_artist(first.get("artist"))
    # The same chain a plain search uses, and for the same reason: Spotify hands over
    # metadata and no audio, so *which* platform plays it is entirely this host's
    # business — and a Spotify link is the one request that cannot be met by
    # "try another link", because the member named a recording rather than a user.
    resolution, _provider = _chain(
        search_text, requester=requester, expected_sec=expected, artist=artist
    )
    # The card should say what was asked for, not what was found: the whole point
    # of resolving a Spotify link is that the member named a specific recording.
    label = f"{first.get('artist', '')} - {first.get('title', '')}".strip(" -") or search_text
    if resolution.track is not None:
        resolution.track.title = label
        if first.get("thumbnail"):
            resolution.track.thumbnail = first["thumbnail"]
    for alternative in resolution.alternatives:
        alternative.title = alternative.title or label
        if first.get("thumbnail"):
            alternative.thumbnail = first["thumbnail"]
    # Extra tracks of an album or playlist ride along on the resolution so the
    # command can queue them without a second metadata fetch.
    resolution.query = query
    return resolution


# --------------------------------------------------------------------------- #
# The weekly self-check: is the extractor still able to resolve anything?
# --------------------------------------------------------------------------- #
#: What the self-check asks for, overridable with ``YTDL_PROBE_TARGET``. The
#: default is Blender's own *Big Buck Bunny* upload: a recording that is meant to
#: stay up, carries no age gate and belongs to nobody's catalogue. A *music video*
#: would be the obvious choice and the wrong one — probes that name a song start
#: failing when the song is pulled, and the check would then be the fault it
#: reports.
DEFAULT_PROBE_TARGET = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


@dataclass(frozen=True)
class Probe:
    """One self-check, in the shape three surfaces read.

    ``blocked`` is kept apart from the rest because it is the only failure that
    is not a fault in this deployment: the extractor is working perfectly and the
    *provider* has decided this host is a bot. Reporting the two the same way
    would send an operator to upgrade a package that is already current.
    """

    healthy: bool
    reason: str = ""
    detail: str = ""
    version: str = ""
    target: str = ""
    blocked: bool = False
    #: True when the failure was *this host's network* rather than an answer about
    #: the request. The weekly pass reports these as "could not ask" instead of
    #: "dead", because only one of those is a fault in the deployment.
    unreachable: bool = False
    #: Which player-client set answered, and what it had to do to answer — the
    #: half of a self-check that is *diagnosis* rather than a verdict. A host whose
    #: default client is refused and whose ``android_vr`` answers is working today
    #: and about to stop being, and the difference between the two is invisible in a
    #: plain "working".
    client: str = ""
    climbed: bool = False
    at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "reason": self.reason,
            "detail": self.detail,
            "version": self.version,
            "target": self.target,
            "blocked": self.blocked,
            "unreachable": self.unreachable,
            "client": self.client,
            "climbed": self.climbed,
        }

    @property
    def note(self) -> str:
        """What the client set says, in one clause, or ``""`` when nothing does.

        Only says something when it is worth saying: a host answering on the
        default client is the normal case, and a sentence about it on every check
        is a sentence nobody reads.
        """
        if not self.client:
            return ""
        if self.climbed:
            return (
                f"the default client was refused and `{self.client}` answered, so this "
                "host is being noticed — worth running `/music doctor` if songs start dying"
            )
        return ""

    def sentence(self) -> str:
        """One line for a card, with the version attached either way."""
        head = "working" if self.healthy else f"**failed** — {self.reason}"
        tail = f" · `{self.version}`" if self.version else ""
        return f"{head}{tail}"


#: How long a catalogue reading is reused. It is a fact about the network, and
#: asking on every command would make the reading cost more than it tells.
CATALOGUE_TTL = 300.0
_catalogue_at: float = 0.0
_catalogue_ok: bool = False


def catalogue_reading() -> bool | None:
    """The last catalogue reading, or ``None`` when there is no current one.

    A stale reading is reported as *no reading* rather than as old news: the whole
    value of this number is that it is about right now, and a console showing a
    five-minute-old "reachable" on a host that has since lost its route is a panel
    telling somebody the wrong thing.
    """
    if not _catalogue_at:
        return None
    if (time.monotonic() - _catalogue_at) >= CATALOGUE_TTL:
        return None
    return _catalogue_ok


def sources_state() -> dict:
    """Which providers this host will ask, and in what order — without asking one.

    Process state only, so the console and ``/health`` can read it on a page that
    polls. It is the reading that turns "the provider refused this host" into a
    sentence an operator can act on: it says which doors are left, and which ones
    are currently being knocked on last because they would not answer.
    """
    order = source_order()
    return {
        "order": [
            {"name": name, "label": PROVIDER_LABELS.get(name, name)} for name, _prefix in order
        ],
        "refusing": [name for name, _prefix in order if _refusing(name)],
        "preferred": _last_good_source,
        "catalogue": catalogue_reading(),
    }


def catalogue_reachable(*, refresh: bool = False) -> bool:
    """Whether the credential-free catalogue answers from this host.

    The one source that needs no extractor and no account, so this is the question
    that decides whether a refusal somewhere else leaves a member with silence or
    with a song. Answered from a memo because it is a fact about the network rather
    than about the request — and asked *before* it is needed, on the surfaces where
    somebody can still act on it.
    """
    global _catalogue_at, _catalogue_ok
    now = time.monotonic()
    if not refresh and _catalogue_at and (now - _catalogue_at) < CATALOGUE_TTL:
        return _catalogue_ok
    try:
        _audius_json("/tracks/search", {"query": "music", "limit": 1})
        _catalogue_ok = True
    except Exception as exc:  # noqa: BLE001 — the reading *is* whether it answered
        _catalogue_ok = False
        print(
            f"[MUSIC] the open catalogue did not answer: {type(exc).__name__}: {exc}",
            flush=True,
        )
    _catalogue_at = now
    return _catalogue_ok


def probe_target() -> str:
    """What the self-check asks for, from the environment when it is set."""
    configured = (config_audit.get("YTDL_PROBE_TARGET") or "").strip()
    return configured or DEFAULT_PROBE_TARGET


def self_check(target: str | None = None) -> Probe:
    """Ask the extractor for one recording, all the way to a playable URL.

    A ``version()`` string proves nothing: the failure this catches leaves the
    package installed and importable, and only shows when a real extraction
    happens — YouTube changes how it signs a stream and an extractor one release
    behind answers *"Sign in to confirm you're not a bot"* to everybody. So the
    probe does what ``/play`` does: one link, resolved to a stream URL, never
    cached, and never raising.

    Never raises, by design: it is called from a weekly background pass, from an
    administrator's command and from an API route, and a check that can break its
    callers is worse than no check.
    """
    address = (target or probe_target()).strip() or DEFAULT_PROBE_TARGET
    stamp = version()
    if not available():
        return Probe(
            healthy=False,
            reason="`yt-dlp` is not installed on this host, so nothing can be resolved at all",
            version=stamp,
            target=address,
            at=time.time(),
        )
    try:
        resolution = resolve(address, requester="the weekly self-check")
    except MediaError as exc:
        blocked = is_blocked(exc)
        if blocked:
            reason = (
                "the provider is refusing this host rather than answering, which is a "
                "statement about where it runs and not about the extractor"
            )
        elif exc.kind == "too_short":
            # A failure about the *check*, not about the deployment: somebody
            # pointed YTDL_PROBE_TARGET at a clip, and the fix is to point it at a
            # recording again rather than to upgrade anything.
            reason = (
                "the recording this check asks for is a clip rather than a song — "
                "`YTDL_PROBE_TARGET` needs a longer one"
            )
        elif exc.kind in ("not_found", "extractor_missing"):
            reason = f"the extractor answered nothing ({exc.message})"
        else:
            reason = f"the extractor could not turn it into a stream ({exc.message})"
        return Probe(healthy=False, reason=reason, version=stamp, target=address,
                     blocked=blocked, at=time.time())
    except Exception as exc:  # noqa: BLE001 — a check must not raise
        blocked = is_blocked(exc)
        unreachable = looks_unreachable(exc)
        if blocked:
            reason = "the provider is refusing this host rather than answering"
        elif unreachable:
            reason = f"this host could not reach the provider ({type(exc).__name__})"
        else:
            reason = f"the extractor raised {type(exc).__name__}: {exc}"[:300]
        return Probe(healthy=False, reason=reason, version=stamp, target=address,
                     blocked=blocked, unreachable=unreachable, at=time.time())

    track = resolution.track
    if track is None or not track.stream_url:
        return Probe(
            healthy=False,
            reason="the extractor read the recording but produced no stream for it",
            version=stamp,
            target=address,
            at=time.time(),
        )
    # Which client set the ladder settled on, read from the module memo the
    # extraction just wrote. This is the difference between "working" and "working
    # because one fallback is carrying the whole feature" — and it is the reading
    # that turns a next-week outage into a today ticket.
    settled = _last_good_client or ()
    return Probe(
        healthy=True,
        detail=f"{track.title} · {format_seconds(track.duration)}",
        version=stamp,
        target=address,
        client="+".join(settled) if settled else "default",
        climbed=_last_climbed,
        at=time.time(),
    )


def format_seconds(seconds: float | int) -> str:
    """``m:ss`` for a duration or for a position inside one."""
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def queued_from_spotify(query: str, *, requester: str) -> list[Track]:
    """The rest of an album or playlist, as unresolved tracks.

    Each one keeps the query it was found by *and* the duration it should be, so
    it is matched when its turn comes rather than at queue time.
    """
    tracks = _spotify_tracks(query)
    out: list[Track] = []
    for entry in tracks[1:]:
        label = f"{entry.get('artist', '')} - {entry.get('title', '')}".strip(" -")
        out.append(
            Track(
                title=(label or entry.get("title") or "")[:90],
                provider="spotify",
                duration=int((entry.get("duration_ms") or 0) / 1000),
                requester=requester,
                thumbnail=entry.get("thumbnail") or "",
                webpage_url=entry.get("query") or "",
                search_query=entry.get("query") or label,
                expected_artist=clean_artist(entry.get("artist")),
            )
        )
    return out
