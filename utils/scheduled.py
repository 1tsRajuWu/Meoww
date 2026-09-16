"""Scheduled dashboard messages — the durable queue behind "send this later".

A scheduled message is a promise the panel makes *after* the browser is closed,
so the record has to outlive the process and survive being interrupted. It lives
in SQLite (and therefore in the cloud backup) rather than in memory, and every
state change is a single conditional ``UPDATE`` whose affected row count is the
answer — "was this still pending?" — rather than a read followed by a hopeful
write. Two panels, a restart and the delivery loop all touching the same row is
the normal case here, not an edge case.

The Flask thread and the bot's event loop both talk to this module. Every call is
short and synchronous (the same pattern as ``utils.shop`` and ``utils.loans``),
and ``db.connect`` gives each one the shared busy timeout so a write from the
dashboard waits for the bot instead of failing.
"""
import json

from utils import db

#: How far in the future a message may be scheduled (a year), how soon it may
#: fire, and how much clock skew between the browser and the server is forgiven.
MAX_HORIZON_SECONDS = 365 * 24 * 3600
MIN_LEAD_SECONDS = 5
PAST_GRACE_SECONDS = 300

#: Repeats are a closed set, not a free-form interval: the UI offers three
#: choices, and an interval the UI cannot express is an interval nobody can fix.
REPEAT_CHOICES = ((0, "Once"), (1440, "Daily"), (10080, "Weekly"))

#: More than this many pending messages in one server is not a schedule any more.
MAX_PENDING_PER_GUILD = 25

#: A run more than this late is skipped rather than sent: after a long outage a
#: three-day-old "good morning" is noise, and one that arrives at the wrong time
#: is worse than one that says it was skipped.
MISS_CATCHUP_SECONDS = 24 * 3600

#: A row claimed for delivery and never finished means the process died in the
#: middle of sending. It is settled as failed, not retried — the message may have
#: landed, and a duplicate announcement is worse than a missing one.
STALE_CLAIM_SECONDS = 10 * 60

#: A repeating schedule that keeps failing is stopped instead of nagging forever.
FAIL_LIMIT = 3

#: Finished rows are kept as history for this long.
HISTORY_DAYS = 90


def repeat_label(minutes) -> str:
    for value, label in REPEAT_CHOICES:
        if int(minutes or 0) == value:
            return label
    return f"Every {int(minutes)}m"


def next_occurrence(run_at: int, repeat_minutes, now: int):
    """The next run strictly after ``now``, on the schedule's own grid.

    Computed rather than stepped: after a long outage a daily post has missed a
    hundred slots, and the answer must be the next slot, not the hundredth.
    Anchoring to the original ``run_at`` is what keeps "daily at 09:00" at 09:00
    instead of drifting to whenever the bot happened to wake up.
    """
    step = int(repeat_minutes or 0) * 60
    if step <= 0:
        return None
    if run_at > now:
        return run_at
    missed = (now - run_at) // step + 1
    return run_at + missed * step


def payload_summary(payload: dict) -> str:
    """A one-line preview of a stored payload, for the dashboard list."""
    if not isinstance(payload, dict):
        return ""
    body = str(payload.get("content") or "").strip()
    embed = payload.get("embed") or {}
    if not body and isinstance(embed, dict):
        body = str(embed.get("title") or embed.get("description") or "").strip()
    body = " ".join(body.split())
    return body[:140]


def create(
    guild_id: int,
    channel_ids,
    payload: dict,
    run_at: int,
    *,
    repeat_minutes: int = 0,
    created_by=None,
    now: int | None = None,
) -> int | None:
    """Queue one message. ``None`` when the guild already holds the maximum.

    The cap is checked and written in one connection so two open panels cannot
    both read "24 pending" and both add one past the limit.
    """
    import time

    now = int(now if now is not None else time.time())

    def work(cur):
        pending = cur.execute(
            "SELECT COUNT(*) FROM scheduled_messages WHERE guild_id = ? AND status = 'pending'",
            (guild_id,),
        ).fetchone()[0]
        if pending >= MAX_PENDING_PER_GUILD:
            return None
        cur.execute(
            """INSERT INTO scheduled_messages
               (guild_id, channel_ids, payload, preview, run_at, repeat_minutes,
                status, created_by, created_at, fire_count, fail_streak)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, 0, 0)""",
            (
                guild_id,
                json.dumps([int(cid) for cid in channel_ids]),
                json.dumps(payload),
                payload_summary(payload),
                int(run_at),
                int(repeat_minutes or 0),
                int(created_by) if created_by is not None else None,
                now,
            ),
        )
        return cur.lastrowid

    try:
        # One transaction for the cap check and the insert, through the same
        # helper every balance move uses: two open panels must not both read
        # "24 pending" and both write the 26th message.
        return db.run_atomic(work)
    except Exception as e:
        print(f"[SCHEDULE] Could not store a scheduled message: {e}", flush=True)
        return None


def pending_count(guild_id: int) -> int:
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM scheduled_messages WHERE guild_id = ? AND status = 'pending'",
            (guild_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def _row_to_dict(row) -> dict:
    (
        msg_id, guild_id, channel_ids, payload, preview, run_at, repeat_minutes,
        status, created_by, created_at, claimed_at, fired_at, last_run_at,
        fire_count, fail_streak, last_error, last_results,
    ) = row
    try:
        channels = json.loads(channel_ids)
    except (TypeError, ValueError):
        channels = []
    try:
        results = json.loads(last_results) if last_results else []
    except (TypeError, ValueError):
        results = []
    if not isinstance(results, list):
        results = []
    # Ids leave this module as strings for the same reason everywhere else in the
    # dashboard: a Discord snowflake does not survive a JavaScript number.
    results = [
        {**item, "channel_id": str(item.get("channel_id"))}
        for item in results
        if isinstance(item, dict)
    ]
    try:
        body = json.loads(payload)
    except (TypeError, ValueError):
        body = {}
    return {
        "id": msg_id,
        "guild_id": guild_id,
        "channel_ids": [str(cid) for cid in channels],
        "channel_count": len(channels),
        "payload": body if isinstance(body, dict) else {},
        "preview": preview or "",
        "run_at": int(run_at or 0),
        "repeat_minutes": int(repeat_minutes or 0),
        "repeat": repeat_label(repeat_minutes),
        "status": status,
        "created_by": str(created_by) if created_by is not None else None,
        "created_at": int(created_at or 0),
        "fired_at": int(fired_at) if fired_at else None,
        "last_run_at": int(last_run_at) if last_run_at else None,
        "fire_count": int(fire_count or 0),
        "fail_streak": int(fail_streak or 0),
        "last_error": last_error or "",
        "results": results,
        "has_everyone": bool(body.get("everyone")) if isinstance(body, dict) else False,
    }


_SELECT = """SELECT id, guild_id, channel_ids, payload, preview, run_at, repeat_minutes,
                    status, created_by, created_at, claimed_at, fired_at, last_run_at,
                    fire_count, fail_streak, last_error, last_results
             FROM scheduled_messages"""


def get(guild_id: int, msg_id: int) -> dict | None:
    """One message, but only if it belongs to ``guild_id``.

    Every read and write is scoped this way: the session proves an administrator
    signed in, and the guild id is what proves they are looking at *this* server's
    schedule.
    """
    conn = db.connect()
    try:
        row = conn.execute(f"{_SELECT} WHERE id = ? AND guild_id = ?", (msg_id, guild_id)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def list_for_guild(guild_id: int, limit: int = 25) -> dict:
    """``{"upcoming": [...], "recent": [...]}`` for the dashboard panel."""
    conn = db.connect()
    try:
        upcoming = conn.execute(
            f"{_SELECT} WHERE guild_id = ? AND status IN ('pending', 'sending') "
            "ORDER BY run_at ASC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
        # Anything that has actually run at least once, whichever way it went:
        # a repeat stays pending and still carries its last delivery, which is the
        # only place an administrator can see that yesterday's post failed.
        recent = conn.execute(
            f"{_SELECT} WHERE guild_id = ? AND last_run_at IS NOT NULL "
            "ORDER BY last_run_at DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return {
        "upcoming": [_row_to_dict(r) for r in upcoming],
        "recent": [_row_to_dict(r) for r in recent],
    }


def due(now: int, limit: int = 20) -> list[dict]:
    """Pending messages whose time has come, oldest first."""
    conn = db.connect()
    try:
        rows = conn.execute(
            f"{_SELECT} WHERE status = 'pending' AND run_at <= ? ORDER BY run_at ASC LIMIT ?",
            (int(now), limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def claim(msg_id: int, now: int | None = None) -> bool:
    """Take ownership of a due message. ``False`` means somebody else already did.

    This conditional update *is* the exactly-once guard: the loop, a second bot
    process, or the same message seen twice all funnel through it, and only the
    one whose statement reports an affected row goes on to post.
    """
    import time

    now = int(now if now is not None else time.time())
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE scheduled_messages SET status = 'sending', claimed_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now, msg_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def cancel(guild_id: int, msg_id: int) -> bool:
    """Stop a pending message. ``False`` when it was not pending any more.

    Cancelled, not deleted: a message that vanished from the list without a trace
    is indistinguishable from one that was never created.
    """
    import time

    now = int(time.time())
    conn = db.connect()
    try:
        # ``last_run_at`` is stamped here as well as ``fired_at``: the dashboard's
        # "recently sent" list is exactly the rows that have a ``last_run_at``, and
        # a cancellation that left it NULL would delete the message from the panel
        # without ever saying it was cancelled.
        cur = conn.execute(
            "UPDATE scheduled_messages SET status = 'cancelled', fired_at = ?, "
            "last_run_at = ?, last_error = 'cancelled from the dashboard' "
            "WHERE id = ? AND guild_id = ? AND status = 'pending'",
            (now, now, msg_id, guild_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _settle(
    msg_id: int,
    now: int,
    *,
    ok: bool,
    error: str = "",
    results=None,
    missed: bool = False,
    final: bool = False,
):
    """Record what happened to a claimed message, and advance a repeat.

    A repeat is not finished by being sent — it is finished by being cancelled —
    so success moves ``run_at`` to the next slot past ``now`` instead of closing
    the row. A one-shot becomes ``sent`` or ``failed`` and stays as history.
    """
    results = results or []
    conn = db.connect()
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT repeat_minutes, run_at, fire_count, fail_streak "
            "FROM scheduled_messages WHERE id = ?",
            (msg_id,),
        ).fetchone()
        if row is None:
            return
        repeat, run_at, fire_count, fail_streak = row
        repeat, run_at, fire_count, fail_streak = (
            int(repeat or 0), int(run_at or 0), int(fire_count or 0), int(fail_streak or 0),
        )
        failing = not ok and not missed
        streak = fail_streak + 1 if failing else (0 if ok else fail_streak)
        nxt = next_occurrence(run_at, repeat, now) if repeat > 0 else None
        if nxt is not None and not final and (ok or missed or streak < FAIL_LIMIT):
            cur.execute(
                "UPDATE scheduled_messages SET status = 'pending', run_at = ?, "
                "claimed_at = NULL, last_run_at = ?, fire_count = ?, fail_streak = ?, "
                "last_error = ?, last_results = ? WHERE id = ?",
                (nxt, now, fire_count + 1, streak, error or None, json.dumps(results), msg_id),
            )
        else:
            if not ok and failure_is_final(repeat, streak) and not error:
                error = "stopped after repeated failures"
            cur.execute(
                "UPDATE scheduled_messages SET status = ?, claimed_at = NULL, "
                "fired_at = ?, last_run_at = ?, fire_count = ?, fail_streak = ?, "
                "last_error = ?, last_results = ? WHERE id = ?",
                (
                    "sent" if ok else "failed",
                    now,
                    now,
                    fire_count + 1,
                    streak,
                    error or None,
                    json.dumps(results),
                    msg_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def failure_is_final(repeat, streak) -> bool:
    """A repeat stops only after :data:`FAIL_LIMIT` failures in a row."""
    return bool(repeat) and int(streak or 0) >= FAIL_LIMIT


def mark_sent(msg_id: int, now: int, results=None, error: str = ""):
    _settle(msg_id, now, ok=True, error=error, results=results)


def mark_failed(msg_id: int, now: int, error: str, results=None, *, final: bool = False):
    """Record a failure. ``final`` closes a repeat that can never work again."""
    _settle(msg_id, now, ok=False, error=error, results=results, final=final)


def mark_missed(msg_id: int, now: int, reason: str):
    """A run that was too late to be worth sending.

    Not a failure: a repeat keeps its streak and its place in the queue, and only
    the reason it did not go out is recorded.
    """
    _settle(msg_id, now, ok=False, error=reason, missed=True)


def sweep_stale(now: int | None = None) -> list[int]:
    """Settle messages left mid-flight by a restart. Returns their ids."""
    import time

    now = int(now if now is not None else time.time())
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM scheduled_messages WHERE status = 'sending' "
            "AND claimed_at IS NOT NULL AND claimed_at <= ?",
            (now - STALE_CLAIM_SECONDS,),
        ).fetchall()
    finally:
        conn.close()
    ids = [r[0] for r in rows]
    for msg_id in ids:
        mark_failed(
            msg_id,
            now,
            "the bot restarted while this was being sent — check the channel "
            "before scheduling it again",
        )
    if ids:
        print(f"[SCHEDULE] Settled {len(ids)} interrupted message(s) as failed.", flush=True)
    return ids


def prune(now: int | None = None, days: int = HISTORY_DAYS) -> int:
    """Drop old history, so the queue table does not grow forever."""
    import time

    now = int(now if now is not None else time.time())
    conn = db.connect()
    try:
        cur = conn.execute(
            "DELETE FROM scheduled_messages WHERE status IN ('sent', 'failed', 'cancelled') "
            "AND last_run_at IS NOT NULL AND last_run_at <= ?",
            (now - int(days) * 86400,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def describe(msg: dict) -> str:
    """A message named for the log without spilling its JSON into it."""
    when = int(msg.get("run_at") or 0)
    return f"#{msg.get('id')} ({msg.get('status')}, run_at {when}, {msg.get('repeat')})"
