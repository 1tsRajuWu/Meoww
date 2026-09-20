"""What this deployment is actually doing, as counts a stranger may read.

The landing page's two preview cards were furniture. *11 systems* and *100
commands* are true on every copy of this bot ever started — including one in no
servers at all — so they answered a question nobody asked, and the numbers that
would make a visitor believe the thing is *used* were absent from the page: how
many servers it is in, how many people are in them, how much has been counted
since it was switched on. Those numbers exist. The gateway knows the first two;
the tracker has been counting messages and voice seconds since the day it was
enabled.

Four rules, and the first three are the ones ``/health`` already follows for the
same reason — this surface is unauthenticated:

* **Counts, never names.** No server names, no channel names, no member names.
  A landing page may say *1,669 people reached*; it may not say which twelve.
  The per-channel and per-member breakdowns stay behind the dashboard's
  sign-in, which is where they belong.
* **A reading that was never taken is absent, not zero.** A sleeping free-tier
  host has no gateway to ask, and a confident *0 servers* reads as a bot nobody
  uses rather than as a process that has not woken up. Missing figures come back
  as ``None`` and every surface decides what to say instead — which is what
  ``_home_stats`` has always done for the same reason.
* **Nothing is fetched while a request is being answered.** Both reads are local
  SQL over two indexed tables, and :func:`snapshot` is cached for a minute, so a
  page refreshing in a loop costs one query rather than one per visitor.
* **The windows belong to the tracker.** "Last 7 days" has to mean the same seven
  days ``/activity`` and the weekly digest mean. The arithmetic is repeated here
  rather than imported, the way ``utils/digest`` already does it, and
  ``tests/test_figures.py`` compares the two so they cannot drift apart quietly.
"""
from __future__ import annotations

import datetime
import sqlite3
import threading
import time

from utils import db, digest

#: The windows the public page offers, in the order it offers them, as
#: ``(id, label, days)`` — ``None`` days meaning "everything kept". The ids are
#: the tracker's own period names (``today``/``week``/``month``/``all``); the
#: labels are how the page words them, and deliberately say the number of days
#: rather than "week", because a visitor reading a count deserves to know the
#: window it covers without having to guess where a week starts.
PERIODS: tuple[tuple[str, str, int | None], ...] = (
    ("today", "Today", 1),
    ("week", "Last 7 days", 7),
    ("month", "Last 30 days", 30),
    ("all", "All time", None),
)

#: The floor ``all`` uses, matching the tracker's own "everything" floor rather
#: than ``0000-01-01`` — the dates in these tables are ISO strings written by
#: that module, and comparing against a date it would never write is how a
#: window silently stops covering the oldest rows.
EPOCH_FLOOR = "2000-01-01"

#: How long a snapshot may be reused. The tracker's numbers move at the speed of
#: conversation, not the speed of a page view, and the gateway's move when a
#: server is joined; a minute keeps a landing page fresh without turning a
#: refresh loop — or a hundred visitors at once — into a hundred queries.
CACHE_SECONDS = 60

_lock = threading.RLock()
_cache: dict = {"at": 0.0, "payload": None}


def period_start(period: str, *, day: datetime.date | None = None) -> str:
    """The first date a window covers, inclusive, as the tracker stores it.

    Inclusive on both ends and counted *including* today, which is what the
    tracker's ranges do: ``week`` is today and the six days before it, not "the
    last seven complete days".
    """
    today = day or datetime.datetime.now(datetime.timezone.utc).date()
    for pid, _label, days in PERIODS:
        if pid != period:
            continue
        if days is None:
            return EPOCH_FLOOR
        return (today - datetime.timedelta(days=days - 1)).isoformat()
    raise KeyError(period)


def _scalar(conn, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


#: What a window reads as when the tracker's tables are not there to read. A
#: reading that was never taken is absent, not zero — the same rule the gateway
#: figures below follow, at its extreme, and the page draws an em dash for one.
#:
#: This is not a hypothetical. `bot.db` is git-ignored, so the Pages build clones
#: a checkout with no database in it and renders the real app's pages to make the
#: published copy; the front door answered **500** on the first real publish
#: because a *reporting* read found no schema. A marketing page must not be
#: brought down by a table it only wanted a count from — and the built copy is the
#: fallback anyway, since `/api/figures` fills the same card in from a host that
#: does have one.
_ABSENT_TOTALS: dict = {
    "messages": None,
    "messages_text": "",
    "voice_seconds": None,
    "voice_text": "",
    "message_channels": None,
    "voice_channels": None,
    "channels_text": "",
    "people": None,
    "people_text": "",
}


def tracker_totals(*, period: str = "all", connect=None) -> dict:
    """What the tracker has counted in this window, across every server.

    Synchronous on purpose: it runs on the web process's request path, and the
    established pattern for that side of the project is a short synchronous read
    (``utils/shop``, ``utils/loans``, ``utils/digest``) rather than a second
    ``aiosqlite`` path into the same file from a Flask worker.

    Every number is returned twice: raw, for anything that needs to compare or
    sum it, and spelled the way ``/activity`` and the weekly digest spell it.
    The second copy is not decoration — the browser has no business owning a
    formatter, and a page that reimplemented "3.5 h" in JavaScript would be the
    third spelling of one duration in this project, which is how readers
    conclude the numbers disagree.
    """
    start = period_start(period)
    conn = (connect or db.connect)()
    try:
        try:
            messages = _scalar(
                conn,
                "SELECT COALESCE(SUM(count), 0) FROM activity_messages WHERE date >= ?",
                (start,),
            )
            voice_seconds = _scalar(
                conn,
                "SELECT COALESCE(SUM(seconds), 0) FROM activity_voice WHERE date >= ?",
                (start,),
            )
            message_channels = _scalar(
                conn,
                "SELECT COUNT(DISTINCT channel_id) FROM activity_messages WHERE date >= ?",
                (start,),
            )
            voice_channels = _scalar(
                conn,
                "SELECT COUNT(DISTINCT channel_id) FROM activity_voice WHERE date >= ?",
                (start,),
            )
            # People *seen*, not people reachable: the union of everyone who said
            # something or spent a minute in voice in the window. The gateway's own
            # member count is a different number (everyone in the servers, whether or
            # not they have ever used the bot) and both are shown, labelled.
            people = _scalar(
                conn,
                "SELECT COUNT(*) FROM ("
                "  SELECT user_id FROM activity_messages WHERE date >= ?"
                "  UNION"
                "  SELECT user_id FROM activity_voice WHERE date >= ?"
                ")",
                (start, start),
            )
        except sqlite3.OperationalError:
            # No schema to count in — see `_ABSENT_TOTALS`. Narrow on purpose:
            # an `OperationalError` is the table (or the file) not being there,
            # which is the one case where "I have no reading" is the true answer.
            # Anything else is a real fault in a real database and belongs to the
            # caller.
            return {"period": period, "started": start, **_ABSENT_TOTALS}
    finally:
        conn.close()
    return {
        "period": period,
        "started": start,
        "messages": messages,
        "messages_text": digest.human_count(messages),
        "voice_seconds": voice_seconds,
        "voice_text": digest.human_voice(voice_seconds),
        "message_channels": message_channels,
        "voice_channels": voice_channels,
        "channels_text": digest.human_count(message_channels + voice_channels),
        "people": people,
        "people_text": digest.human_count(people),
    }


def deployment(*, gateway: dict | None = None, connect=None, now: float | None = None) -> dict:
    """The public payload: gateway counts, tracker totals, and the gateway state.

    ``gateway`` is handed in rather than read here, because the only process that
    has one is the bot's own loop and this module must stay importable — and
    testable — without a Discord connection. It carries ``servers``, ``members``,
    ``systems``, ``commands`` and ``connected``; anything it cannot answer is
    absent, and this reports that as ``None`` rather than as a zero.
    """
    figures = {}
    spelled = {}
    for key in ("servers", "members", "systems", "commands"):
        value = (gateway or {}).get(key)
        try:
            number = None if value is None else int(value)
        except (TypeError, ValueError):
            number = None
        figures[key] = number
        # Absent, not zero: a template prints nothing where a figure is missing
        # rather than printing a measurement nobody took.
        spelled[key] = "" if number is None else digest.human_count(number)
    figures["text"] = spelled
    figures["connected"] = bool((gateway or {}).get("connected"))
    figures["periods"] = []
    for pid, label, days in PERIODS:
        totals = tracker_totals(period=pid, connect=connect)
        figures["periods"].append({
            "id": pid,
            "label": label,
            "days": days,
            **{key: value for key, value in totals.items()
               if key not in ("period", "started")},
        })
    # The moment the reading was *taken*, which is the moment it was asked for:
    # handed in by :func:`snapshot` so the stamp and the cache window it was
    # stored under are the same instant. It used to read the wall clock here
    # while the cache used the caller's, so a caller that asked for a reading
    # "now" got one stamped with a different second than it had just used to
    # decide whether the cache was still good.
    figures["at"] = int(time.time() if now is None else now)
    return figures


def snapshot(*, gateway: dict | None = None, connect=None, now: float | None = None) -> dict:
    """The payload, reused for :data:`CACHE_SECONDS`.

    A cached reading and a live one are the same dict, so no caller can end up
    reading a differently-shaped thing depending on whether the cache hit — the
    class of defect a hand-written stub keeps producing in this repository.
    """
    moment = time.time() if now is None else now
    with _lock:
        cached = _cache["payload"]
        if cached is not None and moment - _cache["at"] < CACHE_SECONDS:
            return cached
    payload = deployment(gateway=gateway, connect=connect, now=moment)
    with _lock:
        _cache["at"] = moment
        _cache["payload"] = payload
    return payload


def reset() -> None:
    """Forget the cached snapshot. The tests reset it in one place."""
    with _lock:
        _cache["at"] = 0.0
        _cache["payload"] = None
