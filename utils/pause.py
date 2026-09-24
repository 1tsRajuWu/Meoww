"""Bot-wide pause: the operator's own switch, and the only one of its kind.

Every other switch in this project is *per server* — that is the whole shape of
the dashboard, and it is why a hundred servers can be configured differently
without anybody's choice affecting anybody else's. This module is the single
exception, and it is a deliberate one: there are moments when the right answer
for **every** server at once is "not right now". A deploying migration, a
Discord-side problem the bot is making worse by answering, a runaway loop that
has to be stopped before it can be diagnosed. Until now the only way to do that
was to stop the process, which takes the dashboard and the console down with it —
so the one tool an operator reaches for in an emergency also removed the panel
that would tell them what was happening.

Three properties, each of which is a decision rather than an implementation
detail:

* **The flag lives in the database.** Not in memory, and not in an environment
  variable: a pause set from the console has to survive a restart, because the
  thing it is usually covering (a deploy, a migration) is a restart. It is one
  `guild_settings` row under ``guild_id = 0``, which is how this schema already
  spells "not a server".
* **It is read through a short cache, and a failed read is not a pause.** The
  check runs on *every* command a member types, so it cannot be a query per
  interaction; five seconds is the window in which a pause takes effect and the
  console says so. And a database that cannot be read returns "not paused" with a
  warning rather than "paused": the alternative is a transient lock turning into
  a global outage that only a restart clears, and a pause is a tool for the
  operator rather than a tripwire for the database.
* **It pauses commands, not the process.** The gateway stays connected, the
  scheduler keeps running, the site and both panels keep answering, and the
  dashboard can still be used to fix whatever the pause was for. What stops is a
  member's command being answered by the bot — and the answer they get says so,
  in words, instead of the bot simply going quiet, which reads as a crash.

The state is written by exactly two places: ``keep_alive`` (the operator's
console, behind the god-level gate) and nothing else. ``tests/test_pause.py``
pins the round trip and the failure direction.
"""
from __future__ import annotations

import time

from . import db

#: Where the flag and its audit live. ``guild_id = 0`` is this schema's own way
#: of saying "no server", which is also what the module state rows use.
NO_GUILD = 0

#: The row that says whether the bot is answering.
KEY = "bot_paused"
#: Who did it, why, and when — written in the same act as the flag itself, so a
#: pause cannot exist without a record of who asked for it.
KEY_BY = "bot_paused_by"
KEY_REASON = "bot_paused_reason"
KEY_AT = "bot_paused_at"

#: How long a read is trusted. Long enough that a command does not cost a query,
#: short enough that the console's own sentence ("within a few seconds") is true.
TTL_SECONDS = 5.0

_cache: dict | None = None
_cached_at = 0.0


def _truthy(value) -> bool:
    """A stored flag, read the way every other boolean row in this project is.

    ``read_setting_sync`` decodes JSON where it parses and returns the raw row
    where it does not, so a value written by this module arrives as ``True`` and
    one written by hand in the database arrives as ``"1"`` or ``"on"``.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on", "enabled")


def _read() -> dict:
    """The four rows, as one reading."""
    try:
        paused = _truthy(db.read_setting_sync(NO_GUILD, KEY, False))
        return {
            "paused": paused,
            "by": str(db.read_setting_sync(NO_GUILD, KEY_BY, "") or ""),
            "reason": str(db.read_setting_sync(NO_GUILD, KEY_REASON, "") or ""),
            "since": float(db.read_setting_sync(NO_GUILD, KEY_AT, 0) or 0),
            "read": True,
        }
    except Exception as exc:  # noqa: BLE001 — see the module docstring
        print(
            f"[PAUSE] ⚠️ Could not read the pause flag ({exc}); treating the bot "
            "as running rather than pausing every server on a database error.",
            flush=True,
        )
        return {"paused": False, "by": "", "reason": "", "since": 0.0, "read": False}


def state(refresh: bool = False) -> dict:
    """The current pause, cached for :data:`TTL_SECONDS`."""
    global _cache, _cached_at
    now = time.monotonic()
    if refresh or _cache is None or (now - _cached_at) > TTL_SECONDS:
        _cache = _read()
        _cached_at = now
    return dict(_cache)


def is_paused() -> bool:
    """Whether the bot should answer commands right now.

    The one function on the bot's own command path — kept to a dictionary read so
    that adding it to every interaction costs nothing measurable.
    """
    return bool(state()["paused"])


def set_state(paused: bool, *, actor: str = "", reason: str = "") -> dict:
    """Pause or resume, recording who asked and why in the same act.

    A resume clears the record rather than leaving the last reason behind: a
    stale \"why\" beside a running bot is the kind of residue that makes the next
    reader distrust the panel.
    """
    if paused:
        db.write_setting_sync(NO_GUILD, KEY, True)
        db.write_setting_sync(NO_GUILD, KEY_BY, actor or "the operator")
        db.write_setting_sync(NO_GUILD, KEY_REASON, reason or "")
        db.write_setting_sync(NO_GUILD, KEY_AT, time.time())
    else:
        for key in (KEY, KEY_BY, KEY_REASON, KEY_AT):
            db.write_setting_sync(NO_GUILD, key, None)
    invalidate()
    return state(refresh=True)


def invalidate() -> None:
    """Drop the cache, so the next read is the database's."""
    global _cache
    _cache = None


def describe(reading: dict | None = None) -> str:
    """One sentence for a card or an embed: what is true, and since when."""
    current = reading or state()
    if not current["paused"]:
        return "The bot is answering commands on every server."
    who = current.get("by") or "the operator"
    reason = (current.get("reason") or "").strip()
    since = float(current.get("since") or 0)
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(since)) if since else "just now"
    return (
        f"Paused by {who} at {when}"
        + (f" — {reason}" if reason else "")
        + ". Every server's commands are refused until it is resumed."
    )
