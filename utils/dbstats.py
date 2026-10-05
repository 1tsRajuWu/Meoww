"""The database health report, one home for the question and one for the answer.

``/dbstatus`` used to be a top-level command whose whole body was this: count the
rows, ask Supabase how the last backup went, build a card. It is now the verb
``/bot dbstatus``, because Discord allows a hundred top-level commands and this
bot is at exactly that — and the command that had to make room is the one whose
job is to *report*, not to change anything.

The move is worth more than the slot it freed. Everything the card says is now
gathered by :func:`collect` and rendered by :func:`embed`, so the two halves are
testable without Discord, without a cog and without an interaction: the row
counts, the integrity check, the size, the backup's age and the reason an upload
is being refused are all facts about a file and a cloud bucket, and a fact that
can only be observed through a slash command is a fact nobody checks.

Nothing here writes. The repair half of the pair — ``/bot db`` — is still the one
that creates tables; this module reads what is there and says so honestly,
including when the answer is "unreadable".
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time
from typing import Any

#: A table name is an *identifier*, and an identifier cannot be a bound parameter
#: — ``?`` stands for a value, never for a name — so a query that counts one is
#: necessarily built by hand. The rule is that only names that are *nothing but*
#: letters, digits and underscores are counted at all: every table this bot
#: creates is one of those, so a name that needs quoting is either somebody
#: else's table or a name shaped like SQL, and a row count is not worth being the
#: place that finds out which.
PLAIN_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")


def local_table_counts(db_path: str) -> tuple[dict[str, int], float, float, str]:
    """Row counts, sizes and an integrity check, with blocking work done off-loop.

    ``SELECT COUNT(*)`` is a full scan, and the tables that grow here are the ones
    with the most rows — called inline this held the event loop, and therefore
    every other command and the gateway heartbeat, for as long as the biggest
    table took to count.
    """
    counts: dict[str, int] = {}
    size_kb = 0.0
    wal_kb = 0.0
    integrity = "unknown"
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.Error as e:
        return counts, size_kb, wal_kb, str(e)
    try:
        try:
            size_kb = os.path.getsize(db_path) / 1024
        except OSError:
            size_kb = 0.0
        try:
            wal_kb = os.path.getsize(f"{db_path}-wal") / 1024
        except OSError:
            wal_kb = 0.0  # no WAL file is the normal case after a clean checkpoint
        cur = conn.cursor()
        try:
            integrity = str(cur.execute("PRAGMA quick_check(1)").fetchone()[0])
        except sqlite3.Error as e:
            integrity = str(e)
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        for (name,) in cur.fetchall():
            table = str(name)
            if not PLAIN_IDENTIFIER.fullmatch(table):
                continue
            try:
                counts[table] = cur.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except sqlite3.Error:
                counts[table] = -1  # unreadable
    except sqlite3.Error as e:
        print(f"[DBSTATUS] Could not read the local database: {e}", flush=True)
    finally:
        conn.close()
    return counts, size_kb, wal_kb, integrity


def _local(db_path: str | None) -> tuple[dict[str, int], float, float, str]:
    if db_path is None:
        from utils import db as _db

        db_path = _db.DB_PATH
    return local_table_counts(db_path)


def _cloud() -> dict[str, Any]:
    """What the backup side knows, without deciding what the card should say."""
    try:
        from utils import cloud_sync as cs
    except Exception as e:  # noqa: BLE001 — a report may not fail because of a report
        return {"configured": False, "error": str(e)}
    configured = bool(cs.SUPABASE_URL and cs.SUPABASE_KEY)
    files: list[dict] = []
    if configured:
        try:
            files = cs._storage_list()
        except Exception:  # noqa: BLE001 — unreachable is a state, not a crash
            files = []
    try:
        refusal = cs.upload_refusal_reason(False)
    except Exception as e:  # noqa: BLE001
        refusal = str(e)
    return {
        "configured": configured,
        "bucket": cs.BUCKET,
        "files": files,
        "refusal": refusal,
        "uploaded": cs.last_upload_ts(),
        "checked": bool(cs._remote_checked),
        "hash": bool(cs._last_sync_hash),
        "restored_from": cs._restored_from,
        "restored_rows": sum(cs._restored_counts.values()),
    }


async def collect(db_path: str | None = None) -> dict[str, Any]:
    """Everything the card says, gathered with the blocking halves off the loop.

    Both halves talk to something slow — SQLite and Supabase — so both run in a
    worker thread. Called inline this held the event loop, and therefore every
    other command and the gateway heartbeat, for the length of a round trip.
    """
    tables, size_kb, wal_kb, integrity = await asyncio.to_thread(_local, db_path)
    cloud = await asyncio.to_thread(_cloud)
    return {
        "tables": tables,
        "size_kb": size_kb,
        "wal_kb": wal_kb,
        "integrity": integrity,
        "cloud": cloud,
    }


def sync_line(cloud: dict[str, Any], *, now: float | None = None) -> str:
    """The one-line answer to "is the backup actually happening?".

    The refusal reason is the *whole* decision — an upload silently not happening
    is how a backup turns out to be days old — so this is the place a human can
    see why. The age is included for the same reason: a sync that stopped an hour
    ago and one that ran twelve seconds ago read identically without it.
    """
    if cloud.get("refusal"):
        return f"🛑 Not uploading: {cloud['refusal']}"
    if cloud.get("error"):
        return f"⚠️ Backup state unreadable: {cloud['error']}"
    if not cloud.get("configured"):
        return "❌ Not configured (env vars missing)"
    if cloud.get("checked") and cloud.get("hash"):
        line = "✅ Synced"
        uploaded = cloud.get("uploaded")
        if uploaded:
            age = max(0, int((now if now is not None else time.time()) - float(uploaded)))
            line += f"\nLast upload: {age}s ago"
    elif cloud.get("checked"):
        line = "⚠️ Restored but not yet uploaded"
    else:
        line = "⏳ Not yet checked"
    if cloud.get("restored_from"):
        line += f"\nRestored from `{cloud['restored_from']}` ({cloud.get('restored_rows', 0)} rows)"
    return line


def backup_field(cloud: dict[str, Any]) -> tuple[str, str]:
    """``(name, value)`` for the cloud half of the card."""
    if not cloud.get("configured"):
        return "☁️ Supabase Backup", "❌ Not configured (env vars missing)"
    current = next((f for f in cloud.get("files", []) if f.get("name") == "bot.db"), None)
    snapshots = [
        f for f in cloud.get("files", [])
        if str(f.get("name", "")).startswith("bot-db-") and str(f.get("name", "")).endswith(".db")
    ]
    if current is None:
        return "☁️ Supabase Backup", "⚠️ Connected but no `bot.db` found"
    size_b = current.get("metadata", {}).get("size", 0)
    return (
        "☁️ Supabase Backup",
        f"✅ Connected to `{cloud['bucket']}`\nCurrent: `{size_b / 1024:.1f} KB`\nSnapshots: **{len(snapshots)}**",
    )


def embed(
    report: dict[str, Any],
    *,
    actor_name: str,
    actor_icon: str | None = None,
    bot_icon: str | None = None,
    now: float | None = None,
) -> discord.Embed:
    """The card, built from the pieces :func:`collect` gathered.

    ``discord`` is imported here rather than at the top of this module, and the
    reason is the whole point of the split: everything above this function is a
    fact about a file, and a fact about a file should be readable — and testable —
    on a machine that has never loaded a gateway. The import that needs the
    library is the one that draws the card.
    """
    import discord

    tables: dict[str, int] = report.get("tables", {})
    integrity = str(report.get("integrity", "unknown"))
    cloud: dict[str, Any] = report.get("cloud", {})

    card = discord.Embed(
        title="🗄️ Database Status",
        color=0x5865F2,
        timestamp=discord.utils.utcnow(),
    )
    if bot_icon:
        card.set_author(name="DaCat — System", icon_url=bot_icon)

    # Integrity and the WAL are here because they are the two things a row count
    # cannot tell you: a corrupt restore and an un-checkpointed WAL both look
    # perfectly healthy as a count of rows.
    card.add_field(
        name="💾 Local Database",
        value=(
            f"`{float(report.get('size_kb', 0.0)):.1f} KB` on disk\n"
            f"{len(tables)} table(s)\n"
            f"WAL: `{float(report.get('wal_kb', 0.0)):.1f} KB`\n"
            f"Integrity: {'✅ ok' if integrity == 'ok' else f'⚠️ {integrity[:60]}'}"
        ),
        inline=True,
    )

    name, value = backup_field(cloud)
    card.add_field(name=name, value=value, inline=True)

    line = sync_line(cloud, now=now)
    if len(line) > 1024:
        line = line[:1021] + "…"
    card.add_field(name="🔄 Sync Status", value=line, inline=True)

    if tables:
        rows = []
        for name_, count in sorted(tables.items()):
            rows.append(f"`{name_}` — ❌" if count < 0 else f"`{name_}` — **{count:,}** rows")
        # Split into chunks of 15 lines each to stay under 1024 chars.
        for i in range(0, len(rows), 15):
            chunk = rows[i : i + 15]
            card.add_field(
                name="📊 Table Rows" + (f" ({i // 15 + 1})" if i else ""),
                value="\n".join(chunk),
                inline=False,
            )

    card.set_footer(text=f"Requested by {actor_name}", icon_url=actor_icon)
    return card
