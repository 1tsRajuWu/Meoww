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
import math
import os
import re
import tempfile
import time
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
_SOUNDCLOUD_ID = re.compile(r"soundcloud\.com/[\w-]+/[\w-]+", re.IGNORECASE)
_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|&|;|\bfeat\.?\b|\bft\.?\b|\bwith\b|\bx\b)\s*", re.IGNORECASE
)
_SPOTIFY_HOST = re.compile(r"spotify\.(?:com|link)", re.IGNORECASE)


class MediaError(Exception):
    """A query that could not become a playable track, said in words a member reads.

    ``kind`` is for the code and the operator log; the message is for the person
    who typed the command, and every one of them names something they can do
    instead of restating the failure.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


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


def ytdl_extract(options: dict, target: str):
    """Run one extraction, retrying with the legacy client set if the defaults fail.

    The retry exists because YouTube's bot checks vary by network: the modern
    clients are the right ones (they negotiate audio-only Opus) and the legacy
    ``android``/``web`` pair is what still answers on a network that has been
    flagged. It is a **fallback**, never the default — hardcoding it was the
    quality bug this project already paid for, because that pair exposes only a
    muxed 360p MP4.
    """
    import yt_dlp  # noqa: PLC0415

    try:
        with yt_dlp.YoutubeDL(options) as ytdl:
            return ytdl.extract_info(target, download=False)
    except Exception as exc:
        if options.get("extractor_args") or not _looks_like_format_refusal(exc):
            raise
        print(f"[MUSIC] default clients failed ({type(exc).__name__}); retrying legacy", flush=True)
        legacy = dict(options)
        legacy["extractor_args"] = {"youtube": {"player_client": ["android", "web"]}}
        with yt_dlp.YoutubeDL(legacy) as ytdl:
            return ytdl.extract_info(target, download=False)


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
            video_id=matched.group(0) if matched else "",
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
    if track.video_id and track.provider == "soundcloud":
        return track.video_id if track.video_id.startswith("http") else f"https://soundcloud.com/{track.video_id}"
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


def search(text: str, *, limit: int = SEARCH_LIMIT) -> list[Candidate]:
    """Flat-search a phrase. Metadata only: no stream URL, so this is the fast half."""
    target = f"ytsearch{limit}:{text}"
    info = ytdl_extract(_base_options(flat=True), target)
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
    address = target or watch_url(track) or track.title
    info = ytdl_extract(_base_options(flat=False), address)
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
        info = ytdl_extract(_base_options(flat=False), parsed.text)
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
        apply_stream(track, stream(track, target=parsed.text))
        return Resolution(query=query, track=track)

    if not parsed.text:
        raise MediaError("empty", "Give me something to search for.")

    candidates = search(parsed.text)
    if not candidates:
        raise MediaError("not_found", f"Nothing matched **{parsed.text}**.")
    resolution = pick(
        candidates,
        parsed.text,
        artist=artist_from_query(parsed.text),
        requester=requester,
        query=parsed.text,
    )
    if resolution.track is None and not resolution.alternatives:
        raise MediaError("not_found", f"Nothing usable matched **{parsed.text}**.")
    if resolution.track is not None:
        # A plain-text search that came back clip-sized is reported before the
        # stream is fetched, so the member is told rather than played a preview.
        if resolution.track.duration and resolution.track.duration < MIN_TRACK_SECONDS and not resolution.track.live:
            raise MediaError(
                "too_short",
                f"The only matches for **{parsed.text}** are clips under "
                f"{MIN_TRACK_SECONDS} seconds. Try `Artist - Title`, or paste a link.",
            )
        apply_stream(resolution.track, stream(resolution.track))
    return resolution


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
    candidates = search(search_text)
    if not candidates:
        raise MediaError("not_found", f"Nothing matched **{search_text}**.")
    resolution = pick(
        candidates, search_text, expected, artist, requester=requester, query=search_text
    )
    # The card should say what was asked for, not what was found: the whole point
    # of resolving a Spotify link is that the member named a specific recording.
    label = f"{first.get('artist', '')} - {first.get('title', '')}".strip(" -") or search_text
    if resolution.track is not None:
        resolution.track.title = label
        if first.get("thumbnail"):
            resolution.track.thumbnail = first["thumbnail"]
        apply_stream(resolution.track, stream(resolution.track))
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
        }

    def sentence(self) -> str:
        """One line for a card, with the version attached either way."""
        head = "working" if self.healthy else f"**failed** — {self.reason}"
        tail = f" · `{self.version}`" if self.version else ""
        return f"{head}{tail}"


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
    return Probe(
        healthy=True,
        detail=f"{track.title} · {format_seconds(track.duration)}",
        version=stamp,
        target=address,
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
