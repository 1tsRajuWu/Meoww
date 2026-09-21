"""Every picture and sound this bot sends, checked weekly, so a dead link reports itself.

The bug this exists for left no trace anywhere. Every roleplay card pointed at a
hand-typed ``media.tenor.com`` URL, all of them eventually answered **404**, and
what the bot sends in that case is a message Discord accepts and draws as an
empty box. Nothing raised, nothing logged, nothing looked wrong in code review —
and the same shape of rot was still in the tree when this module was written
(the ``/marry`` proposal and the wedding card, both 404).

So the check is not "do the links work today" — it is "will I be told when one
stops". Two kinds of thing are probed, because the bot sends two kinds:

* **Hardcoded media** — image and audio URLs written into the source. These are
  *found*, not listed: a hand-written inventory drifts within a release, so the
  files that may hold one are declared and scanned, and the scan skips tests,
  docs and retired cogs (a deliberately broken URL in a test is not a fault to
  report).
* **Resolved media** — the ten roleplay actions and the pet pictures, which are
  fetched at use time and verified before they are sent (:mod:`utils.gifs`). The
  question here is whether the *resolver* still produces something: a provider
  that quietly changed its payload shape fails this while answering ``200``.
* **The extractor itself** — one recording asked for the whole way to a stream
  URL (:func:`utils.media_source.self_check`). This is the only target that is
  not media at all: it is the *machine that finds* media, and it fails in a way
  no link can. yt-dlp stays installed, importable and reporting a version while
  answering *"Sign in to confirm you're not a bot"* to every search, because a
  provider changed how it signs a stream and the extractor is one release behind
  — and every picture and sound in this module would still be healthy on that
  day. Asking for one known recording weekly is what turns "music is broken" into
  a sentence naming the fix.

Three rules, all learned from what went wrong:

* **Absent is not the same as fine, and neither is a ``200``.** A URL is healthy
  only if it answers with a body whose content type matches what it claims to be;
  an HTML error page served as ``200`` is reported as ``not an image``.
* **A clean week is silence.** The card is for dead links. A weekly "everything
  is fine" message is how a channel gets muted, and this check's whole value is
  that its message is read when it does arrive.
* **Never raise.** This runs in a background loop; every probe is best-effort and
  a failure to ask is reported as *not checked* rather than as *dead*, because
  those are different sentences and only one of them is a fault in the link.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field

import aiohttp

from . import config_audit, db, gifs, media_source

#: Where this check's clock lives. Guild ``0`` is the application's own row, the
#: same sentinel ``utils.presence`` uses for a value that belongs to no server.
GLOBAL_ROW = 0
LAST_RUN_KEY = "media_health_last_run"

#: How long a pass stays good for. A week is what this promises, and it is a
#: *setting* rather than a constant so a deployment that wants a daily pass does
#: not need a new build.
DEFAULT_INTERVAL_HOURS = 168


def interval_seconds() -> float:
    """The configured interval, in seconds. Absence means weekly.

    Floored at an hour: a misconfigured ``0`` would otherwise mean a pass on every
    wake of the loop, which is a network round trip per target per cycle.
    """
    try:
        hours = float(config_audit.get("MEDIA_CHECK_INTERVAL_HOURS") or DEFAULT_INTERVAL_HOURS)
    except (TypeError, ValueError):
        hours = DEFAULT_INTERVAL_HOURS
    return max(3600.0, hours * 3600.0)
#: A boot is a deploy, so the first pass is soon — but not instantly, because the
#: gateway, the database restore and the command sync all happen at boot and this
#: is the least urgent of them.
FIRST_CHECK_SECONDS = 900
#: Long enough for a slow CDN, short enough that one dead host cannot stall a pass
#: over forty URLs.
REQUEST_TIMEOUT = 8.0
#: Enough for the content type and the magic bytes; a 2 MB GIF is not downloaded
#: to be looked at.
PROBE_BYTES = 64
#: The check identifies itself, so a page of hits in a host's log is recognisable.
USER_AGENT = "MeowMediaWatch/1.0 (+checks that the bot's pictures still exist)"

#: Extensions that mean media. A URL with one of these and no ``{placeholder}``
#: in it is something this bot would put in a card or an attachment.
MEDIA_EXTENSIONS: tuple[str, ...] = (
    ".gif", ".png", ".jpg", ".jpeg", ".webp", ".avif",
    ".mp4", ".webm", ".mov", ".mp3", ".ogg", ".oga", ".wav", ".m4a",
)
#: Hosts that serve media at extension-less paths. Asking for a URL on one of
#: these is worth it even when the path has no dot in it.
CDN_HOSTS: tuple[str, ...] = (
    "media.tenor.com", "c.tenor.com", "i.imgur.com", "i.redd.it",
    "cdn.purrbot.site", "cdn.otakugifs.xyz", "cataas.com",
    "media.discordapp.net", "cdn.discordapp.com",
)
#: Which content types count as what. A GIF that answers ``image/png`` is fine
#: (CDNs do it), but an HTML page is never media whatever extension it was asked
#: for under.
MEDIA_PREFIXES: tuple[str, ...] = ("image/", "audio/", "video/", "application/ogg")

#: Hosts that serve media at a URL with **no file extension**, which is the one shape
#: the extension test below cannot recognise.
#:
#: This list exists because of the radio catalogue, and the size of the gap is the
#: argument for it: of 122 stations, the extension test saw the 31 ending in
#: ``.mp3``/``.aac`` and skipped the other **91** — ``https://stream.laut.fm/lofi`` and
#: ``https://listen.moe/stream`` are a host and a word. That is this module's own bug,
#: one level down from the one it was written for: a dead stream that nothing checks,
#: because nothing recognised it as a stream. Nothing was *broken* while it went
#: unnoticed; the point is that nothing would have *said* so, which is the entire job.
#:
#: Ports are part of the host here, because :func:`_is_media_url` compares what the
#: URL says. Streaming hosts are usually not the CDN hosts above — they are icecast,
#: Shoutcast and Azura servers — so this is a different kind of name and is kept
#: apart from them deliberately.
STREAM_HOSTS: tuple[str, ...] = (
    "admin.stream.rinse.fm",  # Kool FM London
    "drive.uber.radio",  # the Hindi chart stations
    "ice1.somafm.com",  # the SomaFM catalogue
    "icecast.radiofrance.fr",  # France Musique's opera channel
    "kpophits.stream.laut.fm",
    "listen.moe",  # J-Pop and K-Pop
    "maggie.torontocast.com:2020",
    "manager7.streamradio.fr:1435",
    "radio.plaza.one",
    "stream-143.zeno.fm",
    "stream-37.zeno.fm",
    "stream.brokenbeats.net",
    "stream.cinz.net",
    "stream.laut.fm",  # the largest single group: one station per genre
    "stream.radioparadise.com",
    "stream.srg-ssr.ch",  # Swiss Jazz/Classic/Pop and SRF 3
    "stream.zeno.fm",
    "stream0.wfmu.org",
    "uk2.streamingpulse.com",
    "www.ukbassradio.com",
)

#: Both kinds, kept separate above so a reader can tell a CDN name from a streaming
#: one — they need different rules, because an icecast root *is* the stream.
MEDIA_HOSTS: tuple[str, ...] = CDN_HOSTS + STREAM_HOSTS

#: Where a hardcoded URL can live. Declared rather than walked at large: tests
#: hold deliberately broken URLs, ``docs/`` quotes examples, and
#: ``deleted_features/`` is the retirement home — probing any of them would
#: manufacture findings. Checked against the repository in
#: ``tests/test_media_health.py``, so a renamed directory cannot leave this list
#: pointing at nothing.
SOURCE_PATHS: tuple[str, ...] = (
    "bot.py",
    "keep_alive.py",
    "cogs",
    "utils",
    "templates",
    "static",
)
SOURCE_SUFFIXES: tuple[str, ...] = (".py", ".html", ".css", ".js")
SKIP_PARTS: tuple[str, ...] = (
    "tests", "docs", "deleted_features", "_site", "_hub", "_owner",
    "node_modules", "__pycache__", ".venv", "console", "scripts",
)

#: One URL literal. Stops at the characters that end a URL inside source: a
#: quote, a parenthesis, a backtick, whitespace, or an angle bracket.
URL_PATTERN = re.compile(r"""https?://[^\s"'`<>)\]}]+""")
#: A URL built from a format string is not a URL yet — ``{uid}``/``{reaction}``
#: placeholders are skipped rather than probed, because asking about a template
#: would report the placeholder as a dead link.
TEMPLATE_MARKERS = ("{", "}", "%s", "${")


def _is_media_url(url: str) -> bool:
    """Whether a URL is something this bot would send as media."""
    if any(marker in url for marker in TEMPLATE_MARKERS):
        return False
    path = url.split("?", 1)[0].split("#", 1)[0]
    lowered = path.lower()
    if lowered.endswith(MEDIA_EXTENSIONS):
        return True
    # A bare host names a *service*, not a picture. Found by running this scan
    # against the real tree: ``CDN_HOST = "https://cdn.discordapp.com/"`` in
    # ``utils/discord_auth.py`` is the prefix every avatar URL is built from, and
    # asking for it answers 403 — a finding about a constant, reported as a dead
    # picture, on a link that was never meant to be fetched.
    after_host = path.split("//", 1)[-1]
    host = after_host.split("/", 1)[0].lower()
    if any(host == known or host.endswith("." + known) for known in STREAM_HOSTS):
        # Declared, so it needs no shape test at all. An icecast server's *root* is
        # the stream — ``https://stream.cinz.net/`` is one — and the empty-path rule
        # below exists to keep an *inferred* constant out of the probe list, which is
        # a different question from a host somebody wrote down as a station.
        return True
    if "/" not in after_host or not after_host.split("/", 1)[1].strip("/"):
        return False
    return any(host == known or host.endswith("." + known) for known in CDN_HOSTS)


@dataclass(frozen=True)
class Target:
    """One thing to ask about: what it is, where it is written or how it is made."""

    name: str
    kind: str  # "file" | "action" | "pet"
    url: str = ""
    source: str = ""

    @property
    def label(self) -> str:
        return self.name


def scan_source(root: str | None = None) -> tuple[Target, ...]:
    """Every hardcoded media URL in the files that may hold one.

    Deterministic (sorted by source) and free of duplicates, so two runs of the
    same tree produce the same list and a diff means the tree changed.
    """
    base = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    found: dict[str, Target] = {}
    for entry in SOURCE_PATHS:
        path = os.path.join(base, entry)
        for file_path in _walk(path):
            relative = os.path.relpath(file_path, base)
            if any(part in SKIP_PARTS for part in relative.split(os.sep)[:-1]):
                continue
            if os.path.splitext(file_path)[1] not in SOURCE_SUFFIXES:
                continue
            try:
                with open(file_path, encoding="utf-8", errors="replace") as handle:
                    lines = handle.read().splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, 1):
                for match in URL_PATTERN.finditer(line):
                    url = match.group(0).rstrip(".,;:")
                    if not _is_media_url(url) or url in found:
                        continue
                    found[url] = Target(
                        name=f"{relative}:{number}",
                        kind="file",
                        url=url,
                        source=relative,
                    )
    return tuple(found[url] for url in sorted(found))


def _walk(path: str) -> tuple[str, ...]:
    """Every file under ``path``, or the path itself when it is one."""
    if os.path.isfile(path):
        return (path,)
    out: list[str] = []
    for directory, subdirectories, files in os.walk(path):
        subdirectories[:] = [d for d in subdirectories if d not in SKIP_PARTS and d != "__pycache__"]
        out.extend(os.path.join(directory, name) for name in files)
    return tuple(out)


def declared() -> tuple[Target, ...]:
    """The media this bot *makes*: one target per action, one per pet — plus the
    extractor, which makes the media possible.

    Read off the resolver's own tables, so an action added there is checked
    without anybody remembering to add it here — and a test fails if the two ever
    drift apart. The extractor target is the same idea one level up: its probe
    target is read from :func:`utils.media_source.probe_target`, so a deployment
    that overrides ``YTDL_PROBE_TARGET`` is checked against what it configured.
    """
    targets = [
        Target(name=f"action:{action}", kind="action", source="utils/gifs.py")
        for action in sorted(gifs.ROUTES)
    ]
    targets += [
        Target(name=f"pet:{kind}", kind="pet", source="utils/gifs.py")
        for kind in sorted(gifs.PET_SOURCES)
    ]
    targets.append(
        Target(
            name=EXTRACTOR_TARGET,
            kind="extractor",
            url=media_source.probe_target(),
            source="utils/media_source.py",
        )
    )
    return tuple(targets)


#: The extractor's own target, named once because three places read it: the
#: target list, the console's music panel and this module's own reading of the
#: last pass.
EXTRACTOR_TARGET = "extractor:yt-dlp"


def targets(root: str | None = None) -> tuple[Target, ...]:
    """Everything one pass asks about: what is written down, and what is made."""
    return declared() + scan_source(root)


@dataclass(frozen=True)
class Verdict:
    """One pass. Counts are always real; a list is never a guess."""

    at: int
    checked: int = 0
    #: ``target -> reason``, for the ones that failed. Reason is a sentence.
    dead: dict[str, str] = field(default_factory=dict)
    #: ``target -> url``, so the card can name what to replace.
    places: dict[str, str] = field(default_factory=dict)
    #: Targets that could not be asked (no answer at all). Kept apart from
    #: ``dead`` because "this link is broken" and "I could not reach anybody" are
    #: different claims, and reporting the second as the first would send somebody
    #: to fix a link that is fine.
    unchecked: dict[str, str] = field(default_factory=dict)
    #: Which kind each failure was, so the card can say "a hardcoded link" or "the
    #: resolver, not a link".
    kinds: dict[str, str] = field(default_factory=dict)
    #: ``target -> what is worth knowing``, for the things that are **not failures**.
    #: A third bucket because the first two would both lie about this one: it is not
    #: dead (it answered) and it is not unchecked (it was asked). What lives here is
    #: the extractor working *through a fallback client* — true, working today, and
    #: one provider change away from being neither.
    noted: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> int:
        return max(0, self.checked - len(self.dead) - len(self.unchecked) - len(self.noted))

    @property
    def healthy(self) -> bool:
        return not self.dead

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.dead))

    @property
    def counts(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "ok": self.ok,
            "dead": len(self.dead),
            "unchecked": len(self.unchecked),
            "noted": len(self.noted),
        }


async def _probe(session, url: str) -> tuple[bool, str]:
    """``(healthy, reason)`` for one URL. Never raises.

    A partial body read is enough: the content type is on the response, and the
    first bytes say whether the body is really an image.
    """
    try:
        response = await session.get(
            url,
            headers={"User-Agent": USER_AGENT, "Range": f"bytes=0-{PROBE_BYTES - 1}"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:
        return False, f"no answer ({type(exc).__name__})"
    try:
        status = int(getattr(response, "status", 0) or 0)
        if status >= 400 or status == 0:
            return False, f"HTTP {status or 'no status'}"
        headers = getattr(response, "headers", None) or {}
        content_type = str(headers.get("Content-Type", "")).lower()
        if not content_type.startswith(MEDIA_PREFIXES):
            return False, f"not media (answered {content_type or 'no content type'})"
        content = getattr(response, "content", None)
        if content is not None:
            try:
                await content.read(PROBE_BYTES)
            except Exception:
                pass
        return True, ""
    finally:
        close = getattr(response, "release", None) or getattr(response, "close", None)
        if callable(close):
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass


#: The one reason a resolver failure carries. A module-level constant because it
#: is asserted on: "the resolver produced nothing" and "this URL is 404" are the
#: two different sentences this watch exists to keep apart.
LEFT_EMPTY = "the resolver produced nothing (provider or file)"


#: One reason per way the extractor can be unable to do its job. Asserted on,
#: like :data:`LEFT_EMPTY`: the card's whole value is that the *kind* of failure
#: is legible, because "upgrade the package" and "this host needs cookies" are
#: different afternoons.
EXTRACTOR_FAILED = "the extractor could not resolve a recording it should always be able to"


#: Filed when the extractor answers, but only because a *fallback* player client
#: did. Not a dead link and not yet an outage: it is the reading that says the
#: default client has been refused, which is one provider change away from the
#: extractor answering nothing at all. It rides in ``unchecked`` rather than in
#: ``dead`` for the same reason a transport failure does — there is nothing to
#: blame and nothing to fix *today*, and the card's job is to be legible about which
#: of those it is.
EXTRACTOR_CLIMBING = "a fallback player client is answering for the default one"


async def _extract() -> tuple[bool, str]:
    """Ask the extractor for one recording, off the event loop.

    ``self_check`` is synchronous yt-dlp work (a network call inside a package
    that does its own retrying), so it goes to a thread rather than blocking the
    pass — a check that stutters the gateway while it runs is a check that gets
    switched off.
    """
    try:
        probe = await asyncio.to_thread(media_source.self_check)
    except Exception as exc:  # noqa: BLE001 — the pass must not raise
        return False, f"the extractor check itself failed ({type(exc).__name__})"
    if probe.healthy:
        if probe.climbed:
            # The early warning, and the only one this pass can give before songs
            # start dying: the extractor is working *through a fallback*, which is
            # what a host being refused by the default client looks like from here.
            return False, f"{EXTRACTOR_CLIMBING} (`{probe.client}`) ({probe.note})" if probe.note \
                else f"{EXTRACTOR_CLIMBING} (`{probe.client}`)"
        return True, ""
    if probe.unreachable:
        # Deliberately shaped like the link probe's own transport failure, so the
        # pass files it as ``unchecked``: a host that cannot reach the provider is
        # not a finding about the extractor, and the two must not be told apart by
        # a reader of the card.
        return False, f"no answer ({probe.reason})"
    # The version is part of the finding: "one release behind" is the common case
    # and it is invisible without it.
    suffix = f" (yt-dlp `{probe.version}`)" if probe.version else ""
    return False, f"{probe.reason or EXTRACTOR_FAILED}{suffix}"


async def _resolve(target: Target) -> tuple[str | None, str]:
    """The URL the resolver produces for an action/pet, or the reason it did not.

    No second probe here on purpose: the resolver already asked for the URL and
    checked that it answers with an image before handing it back, so an empty
    answer *is* the finding — and it is the more precise one, because it says the
    provider or the file behind it failed rather than naming a URL to blame.
    """
    if target.kind == "action":
        action = target.name.split(":", 1)[1]
        url = await gifs.action_gif(action)
        return url, "" if url else LEFT_EMPTY
    kind = target.name.split(":", 1)[1]
    url = await gifs.pet_picture(kind)
    return url, "" if url else LEFT_EMPTY


async def check(*, session=None, root: str | None = None, now: int | None = None) -> Verdict:
    """One pass over every target. Injectable session, so it is testable.

    Total by construction: a target that cannot be asked lands in ``unchecked``
    with its reason, and the pass carries on. One dead CDN must not cost the other
    thirty-nine answers.
    """
    moment = int(time.time() if now is None else now)
    everything = targets(root)
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    dead: dict[str, str] = {}
    unchecked: dict[str, str] = {}
    noted: dict[str, str] = {}
    places: dict[str, str] = {}
    kinds: dict[str, str] = {}
    try:
        for target in everything:
            if target.kind == "file":
                healthy, reason = await _probe(session, target.url)
                url = target.url
            elif target.kind == "extractor":
                healthy, reason = await _extract()
                url = ""
            else:
                url, reason = await _resolve(target)
                healthy = bool(url)
            if healthy:
                continue
            if url:
                places[target.name] = url
            kinds[target.name] = target.kind
            if str(reason).startswith("no answer"):
                unchecked[target.name] = reason
            elif reason == EXTRACTOR_CLIMBING or str(reason).startswith(EXTRACTOR_CLIMBING):
                # Working, and saying so: a *note* rather than a finding, because
                # filing it as dead would send somebody to fix a link that resolves
                # and filing it as unchecked would say the extractor was never asked.
                noted[target.name] = reason
            else:
                dead[target.name] = reason
    finally:
        if own_session:
            try:
                await session.close()
            except Exception:
                pass
    return Verdict(
        at=moment,
        checked=len(everything),
        dead=dead,
        places=places,
        unchecked=unchecked,
        kinds=kinds,
        noted=noted,
    )


def describe(verdict: Verdict) -> str:
    """One line for the host log, which is read after the fact and by grep."""
    counts = verdict.counts
    if verdict.dead:
        return (
            f"{counts['dead']}/{counts['checked']} media targets are dead: "
            + ", ".join(f"{name} ({verdict.dead[name]})" for name in verdict.names)
        )
    if verdict.unchecked:
        return (
            f"media check could not ask about {counts['unchecked']}/{counts['checked']} "
            f"targets: {', '.join(sorted(verdict.unchecked))}"
        )
    if verdict.noted:
        return (
            f"media check clean, with {counts['noted']} note(s): "
            + ", ".join(f"{name} ({verdict.noted[name]})" for name in sorted(verdict.noted))
        )
    return f"clean — {counts['ok']}/{counts['checked']} media targets answered with real media"


#: The last verdict, so ``/health`` and the console can report it without
#: repeating a pass that takes a network round trip per target.
_state: dict = {"last": None}
#: Whether the *card* has been sent for the standing condition it describes. A
#: dead link is worth saying once per pass, not once per pass plus every reader
#: that asks about it.
_announced: dict = {"at": None}


def record(verdict: Verdict) -> None:
    _state["last"] = verdict


def last() -> Verdict | None:
    return _state["last"]


def reset() -> None:
    """Forget everything remembered. For tests."""
    _state["last"] = None
    _announced["at"] = None


def note(verdict: Verdict) -> str | None:
    """The sentence to send about this pass, or ``None`` to stay quiet.

    A clean pass is silent, and so is a second look at a pass already reported —
    the loop may be woken by a restart or a manual call, and the same four dead
    links should not produce four cards.

    A pass whose only finding is a *note* is also silent, deliberately. A note
    means the target **answered** — the extractor working through a fallback client
    is the one it can raise — and a weekly card about something that works is how a
    channel gets muted. The note is discoverable where somebody is already looking:
    ``/health``, the console's extractor panel and ``/music doctor`` all read it,
    and the moment it stops answering the *dead* alert fires with the full story.
    """
    if not verdict.dead:
        return None
    if _announced["at"] == verdict.at:
        return None
    _announced["at"] = verdict.at
    return describe(verdict)


def _sentence(verdict: Verdict) -> str:
    """How bad this is, in one sentence, for a card and for the console."""
    if verdict.dead:
        return (
            f"**{len(verdict.dead)}** of {verdict.checked} media targets are dead. "
            "Every one of them is drawn as an empty box or a silent card, and "
            "nothing in Discord says why."
        )
    if verdict.unchecked:
        return (
            f"**{len(verdict.unchecked)}** of {verdict.checked} targets could not "
            "be asked about, so this pass proves nothing about them."
        )
    if verdict.noted:
        return (
            f"Every target answered, and **{len(verdict.noted)}** has something worth "
            "knowing: the extractor is working *through a fallback*, which is what a "
            "host being refused by the default player client looks like from here."
        )
    return f"All {verdict.checked} media targets answered with real media."


def card_body(verdict: Verdict) -> str:
    """The findings as text: one block per dead target, with where it lives."""
    lines = []
    for name in verdict.names:
        place = verdict.places.get(name, "")
        lines.append(f"**`{name}`** — {verdict.dead[name]}" + (f"\n{place}" if place else ""))
    for name in sorted(verdict.unchecked):
        lines.append(f"**`{name}`** — could not be asked: {verdict.unchecked[name]}")
    for name in sorted(verdict.noted):
        lines.append(f"**`{name}`** — working, worth knowing: {verdict.noted[name]}")
    return "\n\n".join(lines)[:MAX_CARD_CHARS]


#: Discord's description limit, minus room for the sentence above it.
MAX_CARD_CHARS = 3800


def extractor_state() -> dict:
    """What the last pass found about the extractor, without asking again.

    The console panel and ``/health`` both want this, and neither may trigger a
    network call: one is a page that polls, the other is unauthenticated. So it
    reads the last verdict and says plainly when there is not one yet — *not
    checked* is a state a reader can act on (``/music doctor`` asks now), and it
    is the honest answer for a process that has just restarted.

    The verdict is merged with the **provider order** because the two answer one
    question between them. "The provider refused this host" is only the whole story
    when there is nowhere behind it, and the sources say whether there is: a host
    refused by YouTube may still have an open catalogue to fall through to, and that
    is the difference between a member hearing silence and hearing a song.
    """
    verdict = _extractor_verdict()
    try:
        from . import media_source  # noqa: PLC0415

        verdict["sources"] = media_source.sources_state()
    except Exception:  # noqa: BLE001 — a panel without the source list is still a panel
        pass
    return verdict


def _extractor_verdict() -> dict:
    """The extractor's last verdict on its own, before the provider order is added."""
    verdict = _state["last"]
    if verdict is None:
        return {
            "status": "unknown",
            "detail": "the extractor has not been asked in this process yet",
            "checked_seconds_ago": None,
        }
    age = max(0, int(time.time()) - verdict.at)
    if EXTRACTOR_TARGET in verdict.dead:
        return {"status": "failed", "detail": verdict.dead[EXTRACTOR_TARGET], "checked_seconds_ago": age}
    if EXTRACTOR_TARGET in verdict.unchecked:
        return {
            "status": "unreachable",
            "detail": verdict.unchecked[EXTRACTOR_TARGET],
            "checked_seconds_ago": age,
        }
    if EXTRACTOR_TARGET in verdict.noted:
        # Its own status rather than `ok`, because it is not one: the extractor
        # answers, and it answers through a fallback. A reader who sees `ok` on a
        # host that is being refused by the default client has been told the wrong
        # thing about a deployment that is one provider change from silence.
        return {
            "status": "climbing",
            "detail": verdict.noted[EXTRACTOR_TARGET],
            "checked_seconds_ago": age,
        }
    return {"status": "ok", "detail": "", "checked_seconds_ago": age}


def health_json() -> dict:
    """Counts for ``/health``: no names, on the same rule as everything else there.

    ``/health`` is unauthenticated. Naming a dead *URL* is not a secret, but this
    endpoint publishes counts by policy — ``config``, ``site_guard`` and
    ``uptime`` beside it all do — and the card with the names goes to the
    operator's own channel.
    """
    verdict = _state["last"]
    if verdict is None:
        return {"status": "unknown", "detail": "not checked yet in this process"}
    counts = verdict.counts
    return {
        "status": "attention" if verdict.dead else "ok",
        "targets": counts["checked"],
        "ok": counts["ok"],
        "dead": counts["dead"],
        "unchecked": counts["unchecked"],
        "checked_seconds_ago": max(0, int(time.time()) - verdict.at),
    }


async def due(now: int | None = None) -> bool:
    """Whether a pass is owed, read from the database so a restart does not reset it.

    Render restarts this process often enough that an in-memory timer would mean
    "weekly" in practice was "on every deploy".
    """
    moment = int(time.time() if now is None else now)
    try:
        stored = await db.get_setting(GLOBAL_ROW, LAST_RUN_KEY, None)
    except Exception:
        return True  # a database that cannot answer is not a reason to skip the check
    try:
        last_run = int(stored)
    except (TypeError, ValueError):
        return True
    return moment - last_run >= interval_seconds()


async def mark_ran(now: int | None = None) -> None:
    """Remember when the last pass was, best-effort."""
    moment = int(time.time() if now is None else now)
    try:
        await db.set_setting(GLOBAL_ROW, LAST_RUN_KEY, int(moment))
    except Exception:
        pass
