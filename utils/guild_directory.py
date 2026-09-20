"""Every server the bot has been in, written down where the console can read it.

The console draws two things straight from Discord's gateway cache: the name of
the server it is showing, and the list an operator picks from. Both were read as
``BOT_INSTANCE.guilds`` and nothing else, and that cache is **empty in a fresh
process** until the handshake finishes. So for the first tens of seconds after
every restart — which on a host that stops the process when it is idle is the
window a visitor is most likely to arrive in — every console page read
``Unknown server`` at the top, and an operator signing in with Discord was
offered no servers at all.

So the names are written down. Whenever the gateway *does* know a server — boot,
a join, a page render that found it — its name and member count are stored, and
the console falls back to them when the cache is cold. The gateway stays the
authority while it is warm, so a rename shows up immediately; this is only the
answer for when there is no answer yet.

What is stored is what the bot already published in Discord: a server's name and
how many members it has. Nothing new is collected about anybody, which is why
``/health`` can keep counting rather than naming.

**A server the bot has left must not linger here.** ``_visible_guilds`` derives
from this list, so a stale row would offer an operator a server the bot is no
longer in. ``forget`` runs on the leave event and ``reconcile`` runs on every
boot, which between them cover both ways a server can go away: while the bot is
connected, and while the process was down.

The table is created at boot *and* defensively on first use, like
``utils/meowfee.py``'s bank: a database restored from a backup taken before this
existed would otherwise make every console page raise rather than simply start
with an empty directory.
"""
from __future__ import annotations

import threading
import time

from utils import db

TABLE = "guild_directory"

#: The shape is deliberately the smallest thing that answers the console's two
#: questions. ``seen_at`` is epoch seconds so it needs no parsing and cannot be
#: read in the wrong timezone — it exists to make a stale row diagnosable, not
#: to expire anything.
CREATE_TABLE = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    "guild_id INTEGER PRIMARY KEY, "
    "name TEXT NOT NULL, "
    "member_count INTEGER NOT NULL DEFAULT 0, "
    "seen_at INTEGER NOT NULL DEFAULT 0)"
)

#: guild_id -> row, filled on the first read and written through on every
#: change. The bot's loop and the Flask thread are the same process here, so one
#: dict and one lock is the whole story — the same reasoning as
#: ``utils/modules.py``'s cache, and for the same reason: two readers that can
#: disagree about which servers exist are worse than a slightly slower read.
_CACHE: dict[int, dict] | None = None
_lock = threading.RLock()


def _ensure_table(conn) -> None:
    """Create the directory if it is missing. Safe to call before any read."""
    conn.execute(CREATE_TABLE)


def reset() -> None:
    """Drop the in-process copy, so the next read comes from the database.

    For tests, and for the same reason ``utils.figures.reset()`` exists: the
    cache is process-wide while a test's database is per-test, so a row left
    behind by the previous test is a row about a database that no longer exists.
    """
    global _CACHE
    with _lock:
        _CACHE = None


def _row(guild_id: int, name: str, member_count: int, seen_at: int) -> dict:
    return {
        "id": int(guild_id),
        "name": str(name),
        "member_count": int(member_count or 0),
        "seen_at": int(seen_at or 0),
    }


def read_all() -> list[dict]:
    """Every remembered server, oldest name first. Never raises.

    A failure here must not take a console page down: the caller has a live
    gateway cache to fall back on, and a page that renders without a server name
    is a smaller problem than a page that does not render.
    """
    global _CACHE
    with _lock:
        if _CACHE is not None:
            return [dict(row) for row in _CACHE.values()]
    rows: dict[int, dict] = {}
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            cur = conn.execute(
                f"SELECT guild_id, name, member_count, seen_at FROM {TABLE}"
            )
            for guild_id, name, members, seen_at in cur.fetchall():
                rows[int(guild_id)] = _row(guild_id, name, members, seen_at)
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - depends on the filesystem
        print(f"[DIRECTORY] ⚠️ Could not read the server directory: {exc}", flush=True)
        return []
    with _lock:
        _CACHE = dict(rows)
    return [dict(row) for row in rows.values()]


def read(guild_id: int | None) -> dict | None:
    """One remembered server, or ``None`` if it was never written down."""
    if not guild_id:
        return None
    for row in read_all():
        if row["id"] == int(guild_id):
            return row
    return None


def count() -> int:
    """How many servers the directory knows. Used by the health read."""
    return len(read_all())


def remember(guild_id: int, name: str | None, member_count: int | None = None) -> bool:
    """Write down one server. Returns whether anything actually changed.

    Called on every console render as well as at boot, so it has to be cheap and
    idempotent: an unchanged row is not written at all, which is what keeps a
    page load from becoming a write. ``name`` is required — a server whose name
    Discord has not given us yet is not worth remembering, and storing an empty
    one would let a nameless row answer ``read`` and undo the whole point.

    ``member_count`` of ``None`` means **Discord did not say**, and the stored
    figure is kept. That is not a nicety: ``Guild.member_count`` is ``None``
    without the members intent, which this bot keeps off unless it is explicitly
    asked for, so treating an absent count as zero would replace a number the
    console shows with one nobody measured — and it would do it on the boot
    reconcile, silently, for every server at once.
    """
    global _CACHE
    if not guild_id or not name:
        return False
    guild_id = int(guild_id)
    name = str(name)[:200]
    # Read first, always. Two things depend on it and neither survives an
    # unloaded cache: an absent count must resolve to the count on file rather
    # than to zero, and the comparison below must be against what the database
    # actually holds — because an unloaded cache looks *exactly* like "no row",
    # which would make every call look like a change and rewrite the row on
    # every console page load.
    known = read(guild_id)
    if member_count is None:
        member_count = int(known["member_count"]) if known else 0
    else:
        member_count = int(member_count)
    if known and known["name"] == name and known["member_count"] == member_count:
        return False
    now = int(time.time())
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            conn.execute(
                f"INSERT INTO {TABLE} (guild_id, name, member_count, seen_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET "
                "name = excluded.name, "
                "member_count = excluded.member_count, "
                "seen_at = excluded.seen_at",
                (guild_id, name, member_count, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[DIRECTORY] ⚠️ Could not remember {guild_id}: {exc}", flush=True)
        return False
    with _lock:
        # ``read`` above filled this, so the write-through is never skipped and
        # two calls in a row cannot disagree about what is stored.
        if _CACHE is not None:
            _CACHE[guild_id] = _row(guild_id, name, member_count, now)
    return True


def forget(guild_id: int | None) -> bool:
    """Drop one server — the bot left it, so it must stop being offered."""
    global _CACHE
    if not guild_id:
        return False
    guild_id = int(guild_id)
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            cur = conn.execute(f"DELETE FROM {TABLE} WHERE guild_id = ?", (guild_id,))
            conn.commit()
            removed = cur.rowcount > 0
        finally:
            conn.close()
    except Exception as exc:
        print(f"[DIRECTORY] ⚠️ Could not forget {guild_id}: {exc}", flush=True)
        return False
    with _lock:
        if _CACHE is not None:
            _CACHE.pop(guild_id, None)
    return removed


def reconcile(guilds) -> tuple[int, int]:
    """Make the directory match the servers the bot is actually in.

    ``guilds`` is an iterable of anything with ``id``, ``name`` and
    ``member_count`` — the caller passes ``bot.guilds``. Returns
    ``(written, forgotten)``.

    **An empty list is refused rather than trusted.** ``bot.guilds`` is empty
    during the same window this module exists to paper over, so a reconcile that
    believed it would delete every remembered server exactly when they are the
    only copy — turning the fix into a worse version of the bug. The guard is
    the whole reason this is a function rather than a loop in ``bot.py``.
    """
    rows = [g for g in guilds if getattr(g, "id", None)]
    if not rows:
        print("[DIRECTORY] Refusing to reconcile against an empty server list.", flush=True)
        return (0, 0)
    written = 0
    for guild in rows:
        # ``None`` when Discord did not say, and ``remember`` keeps what it has
        # rather than writing a zero nobody measured. Not ``or 0``: that is the
        # bug this argument's optionality exists to avoid.
        members = getattr(guild, "member_count", None)
        if remember(guild.id, getattr(guild, "name", None), members):
            written += 1
    present = {int(g.id) for g in rows}
    forgotten = 0
    for row in read_all():
        if row["id"] not in present and forget(row["id"]):
            forgotten += 1
    return (written, forgotten)
