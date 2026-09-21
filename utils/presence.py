"""What the bot is doing — one status for the whole application, editable at runtime.

**Discord has no per-server presence.** A bot's activity belongs to its gateway
session, so ``change_presence`` is application-wide: every server the bot is in
sees the same line at the same moment, and nothing can change that — a bot cannot
show "Playing X" in one server and "Playing Y" in another. This is the opposite
of the *member* profile (nickname, avatar, banner, bio), which Discord genuinely
does store per guild, and it is exactly why ``utils/identity.py`` can give each
server its own name and picture while this file cannot.

So the honest shape is **one status the operator owns**, not an option per
server: a global line that rotates through a list. That list lives in the
database rather than in ``bot.py`` so it can be changed without editing code or
waiting for a redeploy, which is the only thing "give me options for this" can
mean for a value that is global by construction.

The list that used to be hardcoded in ``bot.py`` had a line reported as
unwanted — "Knocking over your water glass 🐾". That entry is gone. The rest is
kept as the default, because the cat voice is a real part of this bot's
character, and every line of it is now editable: clearing the list turns the
activity off entirely, which is the correct way to say "just be online".

Stored under a settings row for guild ``0``. The settings table is keyed by
guild, and there is no global-guild concept to borrow, so ``0`` is the sentinel
for "this application, not one server" — the same trick ``utils/modules`` avoids
needing by being per-guild on purpose.
"""
from __future__ import annotations

import json

import discord

from utils import db

#: The row these settings live in. Discord snowflakes are never 0, so this cannot
#: collide with a real server's row.
GLOBAL_ROW = 0

LINES_KEY = "presence_lines"
INTERVAL_KEY = "presence_interval_minutes"

#: Discord's activity types, by the word a command or a form would use. Anything
#: not in here is refused rather than guessed at, because an unknown type is not
#: a small mistake: ``discord.ActivityType(99)`` raises at the moment the presence
#: is applied, so a typo would take the *whole rotation* down.
#:
#: This mapping is also the answer to "why does it say *Playing* that?": the verb
#: in front of the line is Discord's, chosen by the **type**, not by the text. A
#: type of ``playing`` always renders "Playing <text>", so a line has to read as a
#: continuation of a verb ("with the laser pointer") rather than as a headline
#: ("Meowing at the wall" → "Playing Meowing at the wall", which is what was
#: reported). ``custom`` is the one type with no verb at all — the text stands
#: alone — and it is offered rather than defaulted, because it depends on the
#: client rendering a bot's custom status.
ACTIVITY_KINDS: dict[str, discord.ActivityType] = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
    "streaming": discord.ActivityType.streaming,
}
_CUSTOM = getattr(discord.ActivityType, "custom", None)
if _CUSTOM is not None:
    ACTIVITY_KINDS["custom"] = _CUSTOM

DEFAULT_STREAM_URL = "https://twitch.tv/meow"

#: The types whose text has to read as a continuation of a verb. ``custom`` is the
#: exception, and the reason a verb is prepended at all is Discord's rendering.
VERBLESS_KINDS = frozenset({"custom"})

#: The status shown beside the activity (the coloured dot). ``invisible`` is
#: deliberately absent — a bot that appears offline reads as broken.
STATUS_KINDS: dict[str, discord.Status] = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
}

#: What the bot says when nothing has been configured. Empty text with streaming
#: kind displays ONLY the purple streaming badge on the avatar with no text in bio.
DEFAULT_LINES: tuple[tuple[str, str], ...] = (
    ("", "streaming"),
)

#: How long each line stays up. An hour is what this shipped with, and it is
#: long enough that a member who looks twice sees the same thing twice.
DEFAULT_INTERVAL_MINUTES = 60
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 24 * 60

#: Discord's own cap on an activity's name. An over-long line is refused by the
#: API at apply time, which would take the rotation down with it, so it is
#: refused here instead — where the message can say which limit was hit.
MAX_LINE_LENGTH = 128


class PresenceError(ValueError):
    """A configured value that cannot be used, said as a sentence."""


def _clean_line(text: str, kind: str) -> tuple[str, str]:
    """One validated ``(text, kind)`` pair."""
    line = " ".join(str(text or "").split())
    lowered = str(kind or "streaming").strip().lower()
    if lowered not in ACTIVITY_KINDS:
        raise PresenceError(
            f"'{kind}' is not an activity type. Use one of: "
            f"{', '.join(sorted(ACTIVITY_KINDS))}."
        )
    if not line and lowered != "streaming":
        raise PresenceError("A status line cannot be empty.")
    if len(line) > MAX_LINE_LENGTH:
        raise PresenceError(
            f"That line is {len(line)} characters; Discord allows "
            f"{MAX_LINE_LENGTH} in a status."
        )
    return line, lowered


def parse(raw, default=DEFAULT_LINES) -> list[tuple[str, str]]:
    """Whatever is stored, as a usable list of ``(text, kind)``.

    Total on purpose: this reads a value that may have been written by an older
    version, hand-edited in the database, or truncated. A shape it does not
    recognise gives the default rather than raising, because the alternative is a
    rotation loop that dies at boot over a setting nobody can see.
    """
    if raw in (None, ""):
        return list(default)
    try:
        loaded = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return list(default)
    if not isinstance(loaded, list):
        return list(default)
    out: list[tuple[str, str]] = []
    for entry in loaded:
        try:
            if isinstance(entry, dict):
                text, kind = _clean_line(entry.get("text", ""), entry.get("type", "playing"))
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                text, kind = _clean_line(entry[0], entry[1])
            else:
                text, kind = _clean_line(entry, "playing")
            # Dropped lines reported as unwanted (zoomies, water glass)
            if any(banned in text.lower() for banned in ("zoomies", "water glass")):
                continue
            out.append((text, kind))
        except PresenceError:
            # One bad entry does not invalidate the others: dropping it loses a
            # line, where refusing the whole value loses the feature.
            continue
    return out


def dump(lines) -> str:
    """The stored form of a list of ``(text, kind)``."""
    return json.dumps([{"text": text, "type": kind} for text, kind in lines])


def parse_interval(raw, default: int = DEFAULT_INTERVAL_MINUTES) -> int:
    """Minutes between rotations, clamped rather than refused.

    A number out of range is a preference expressed badly, not an error worth
    refusing a command over — and 0 minutes would be a loop hammering Discord's
    presence endpoint, so the floor matters.
    """
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return default
    return max(MIN_INTERVAL_MINUTES, min(minutes, MAX_INTERVAL_MINUTES))


async def lines() -> list[tuple[str, str]]:
    """The configured rotation. Empty means "show no activity at all"."""
    return parse(await db.get_setting(GLOBAL_ROW, LINES_KEY, ""))


async def interval_minutes() -> int:
    return parse_interval(await db.get_setting(GLOBAL_ROW, INTERVAL_KEY, None))


async def interval_seconds() -> float:
    """The rotation delay, read fresh each cycle so a change applies at once."""
    return float(await interval_minutes()) * 60.0


async def set_lines(entries) -> list[tuple[str, str]]:
    """Replace the rotation. ``[]`` turns the activity off; raises on nonsense."""
    cleaned = [_clean_line(text, kind) for text, kind in entries]
    await db.set_setting(GLOBAL_ROW, LINES_KEY, dump(cleaned))
    return cleaned


async def set_interval(minutes) -> int:
    value = parse_interval(minutes)
    await db.set_setting(GLOBAL_ROW, INTERVAL_KEY, str(value))
    return value


async def add_line(text: str, kind: str = "playing") -> list[tuple[str, str]]:
    """Append one line to whatever is already configured."""
    text, kind = _clean_line(text, kind)
    current = await lines()
    current.append((text, kind))
    await db.set_setting(GLOBAL_ROW, LINES_KEY, dump(current))
    return current


def build(
    text: str,
    kind: str,
    status: str = "online",
    url: str | None = None,
) -> discord.Activity | discord.Streaming:
    """One activity object, or a ``PresenceError`` naming what is wrong.

    A ``custom`` kind is a :class:`discord.CustomActivity` rather than a typed
    one, which is the only shape Discord renders without a verb in front of it.
    A ``streaming`` kind is a :class:`discord.Streaming` which renders as
    "Streaming <text>" and displays a purple streaming indicator in Discord.
    """
    text, kind = _clean_line(text, kind)
    if status not in STATUS_KINDS:
        raise PresenceError(
            f"'{status}' is not a status. Use one of: {', '.join(sorted(STATUS_KINDS))}."
        )
    if kind in VERBLESS_KINDS:
        custom = getattr(discord, "CustomActivity", None)
        if custom is None:
            raise PresenceError(
                "This library cannot set a custom status; use playing, watching, "
                "listening, competing, or streaming instead."
            )
        return custom(name=text)
    if kind == "streaming":
        return discord.Streaming(name=text, url=url or DEFAULT_STREAM_URL)
    return discord.Activity(type=ACTIVITY_KINDS[kind], name=text)

