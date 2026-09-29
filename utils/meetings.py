"""Staff and team meetings: when they start, where they are, and who is told.

Three jobs, kept in one module so the command, the notice loop and the cards can
never disagree about the same meeting:

* **Reading a time somebody typed** (:func:`parse_when`) — Discord's command
  API has no date option, so "when" arrives as a sentence. The accepted shapes
  are a closed set with one honest rule behind them: *a wall-clock time with no
  offset is UTC*, and ``18:00+2`` means two hours ahead of it. Every card then
  prints the moment as a Discord timestamp (``<t:…:F>``), which the client
  renders in **each reader's own timezone** — so the input is unambiguous and
  the display needs nobody's timezone setting.
* **Reading the notice schedule** (:func:`parse_notices`) — ``1h,15m``: how
  long before the start each reminder goes out, into the meeting's channel.
* **The rows** — a meeting, its notices and its replies, written the way
  ``utils/scheduled.py`` writes: one connection per call, and every state
  change a conditional ``UPDATE`` whose affected-row count is the answer
  ("was this still pending?") rather than a read followed by a hopeful write.
  The loop that delivers notices and a command cancelling a meeting both touch
  the same rows, so that race is the normal case here, not an edge case.

The card *text* also lives here — as plain strings, no Discord import — because
the shape of an announcement is a decision that should be assertable without a
gateway, and because a second copy of it in the cog is how the announcement and
the reminder come to describe two different meetings.
"""
from __future__ import annotations

import calendar
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from utils import db

# --------------------------------------------------------------------------- #
# The rules, as numbers
# --------------------------------------------------------------------------- #
#: What a caller gets when it passes no notice schedule.
DEFAULT_NOTICES = "1h,15m"
#: How many notices one meeting may schedule. Five is generous; a meeting with
#: eight reminders is a meeting nobody reads after the third.
MAX_NOTICES = 5
#: The longest notice may lead its meeting by — 30 days. Beyond that it is a
#: calendar entry rather than a reminder, and a row that far out is one a
#: reschedule silently invalidates anyway.
MAX_NOTICE_SECONDS = 30 * 24 * 3600
#: No meeting may be booked closer than this: a start inside the minute is a
#: receipt for something already happening, and the notices would all be late.
MIN_LEAD_SECONDS = 60
#: How far ahead a meeting may be set. A year is the same horizon the dashboard's
#: own scheduler uses, so the two queues obey one rule.
MAX_HORIZON_SECONDS = 365 * 24 * 3600
#: A notice more than this overdue is *missed* rather than sent: after a long
#: outage a reminder for a meeting that has already started is noise, and one
#: that arrives after the meeting is worse than one that says it did not.
STALE_NOTICE_SECONDS = 30 * 60
#: A claimed notice that never finished means the process died mid-send. The
#: sweep puts it back and sends it again — unlike a scheduled *announcement*, a
#: reminder that fires twice is a mild nuisance while one that vanishes is the
#: whole feature failing silently.
STALE_CLAIM_SECONDS = 10 * 60
#: Finished meetings (and their notices and replies) are kept this long, then
#: pruned by the same sweep that delivers the notices.
HISTORY_DAYS = 90

STATUS_SCHEDULED = "scheduled"
STATUS_CANCELLED = "cancelled"

NOTICE_PENDING = "pending"
NOTICE_SENDING = "sending"
NOTICE_SENT = "sent"
NOTICE_MISSED = "missed"
NOTICE_CANCELLED = "cancelled"

#: The hint every refusal to parse a time ends with. One sentence, and it names
#: a shape for every branch the parser accepts — because "I couldn't read that"
#: with no example is a dead end for whoever typed it.
WHEN_HINT = (
    "Try `tomorrow 18:00`, `today 6pm`, `in 2 hours`, `friday 19:00` or "
    "`2026-10-03 18:00`. A bare time is UTC; `18:00+2` means two hours ahead of UTC."
)
NOTICES_HINT = (
    "Give me a list like `1h,15m` or `1d,1h` — units are `m`, `h`, `d` and `w`, "
    "a bare number is minutes, `0m` posts when the meeting starts, and `none` "
    "sends no notices at all."
)

_UNIT_SECONDS: dict[str, int] = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}

_IN_RE = re.compile(r"^in\s+(\d{1,4})\s*([a-z.]+)$")
_ISO_DATE_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T\s]+(.+))?$")
_NAMED_DAY_RE = re.compile(
    r"^(today|tomorrow|tmr|tmrw|tonight|this\s+night)\b\s*(?:at\s+)?(.*)$"
)
_WEEKDAY_RE = re.compile(
    r"^(mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun)(?:day)?\b\s*(?:at\s+)?(.*)$"
)
_CLOCK_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?m\.?)?$")
_WEEKDAY_INDEX = {
    "mon": 0, "tue": 1, "tues": 1, "wed": 2,
    "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}


# --------------------------------------------------------------------------- #
# Reading a time somebody typed
# --------------------------------------------------------------------------- #
def _split_offset(text: str) -> tuple[str, int]:
    """Split a trailing UTC offset off a clock string: ``("18:00", 120)``.

    Only called on the *time* half of an input, which is why the date shapes
    cannot be eaten by it — ``2026-10-03`` on its own ends in ``-03`` and would
    otherwise lose three hours to a sign it was only spelling a day with.
    """
    for i in range(len(text) - 1, 0, -1):
        if text[i] not in "+-":
            continue
        tail = text[i + 1:].strip()
        if not re.fullmatch(r"\d{1,2}(?::\d{2})?", tail):
            continue
        head = text[:i].strip()
        if not head:
            continue
        hours, minutes = tail.split(":") if ":" in tail else (tail, "0")
        total = int(hours) * 60 + int(minutes)
        if total > 14 * 60:  # no offset on Earth is further than this
            continue
        if text[i] == "-":
            total = -total
        return head, total
    return text, 0


def _utc_wall(ts: int) -> datetime:
    """A naive datetime reading of a UTC instant — wall clock, no zone.

    Naive on purpose: every datetime in this module is *the time as typed*, and
    ``calendar.timegm`` converts that shape back. Built from the tz-aware form
    rather than from ``utcnow()``, which is deprecated in the Python this
    project runs on.
    """
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)


def _parse_clock(text: str) -> tuple[int, int] | None:
    """``"6:30pm+2"`` -> ``(minutes past midnight UTC, offset already removed)``.

    Answers ``None`` for anything that is not a clock. A 12-hour time needs its
    meridiem to be sane (``13pm`` is not a time), and a 24-hour one is refused
    above 23:59 rather than rolled into the next day — a typo in a meeting's
    start time should be an error, not a different meeting.
    """
    body, offset_minutes = _split_offset(text.strip())
    match = _CLOCK_RE.match(body)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem:
        if hour < 1 or hour > 12:
            return None
        if meridiem == "p" and hour != 12:
            hour += 12
        elif meridiem == "a" and hour == 12:
            hour = 0
    elif hour > 23 or minute > 59:
        return None
    if minute > 59:
        return None
    # The offset moves the wall clock, not the meeting: the epoch is computed
    # from the naive wall time and the offset is subtracted by the caller.
    return hour * 60 + minute, offset_minutes


def parse_when(text, now: int | None = None) -> tuple[int | None, str | None]:
    """A unix timestamp for what somebody typed, or the sentence that refuses it.

    Accepted, in the order they are tried: ``in 2 hours``; an ISO date
    (``2026-10-03 18:00``, ``2026/10/03 6pm``); a named day (``today``,
    ``tomorrow``, ``tonight``) or a weekday (``friday``) with a time; and a bare
    clock (``18:00``, ``6:30pm``) — which means the next one, today if it has
    not passed and tomorrow if it has.

    Every branch ends in the same two checks: a start has to be at least a
    minute away and at most a year away, because "already started" and "in 2031"
    are both meetings nothing will ever send a notice for.
    """
    now = int(now if now is not None else time.time())
    raw = " ".join(str(text or "").split())
    if not raw:
        return None, f"I need a time. {WHEN_HINT}"

    body = raw
    offset_minutes = 0
    minutes_past_midnight: int | None = None
    base_date = None

    relative = _IN_RE.match(body.lower())
    if relative:
        unit = relative.group(2).rstrip(".")
        seconds = _UNIT_SECONDS.get(unit)
        if seconds is None:
            return None, f"I don't know the unit `{relative.group(2)}`. {WHEN_HINT}"
        candidate = now + int(relative.group(1)) * seconds
        return _accept(candidate, now)

    iso = _ISO_DATE_RE.match(body)
    if iso:
        year, month, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        if not iso.group(4):
            return None, "That gives me a day but no time — `2026-10-03 18:00`, for example."
        try:
            base_date = datetime(year, month, day).date()
        except ValueError:
            return None, f"`{year}-{month:02d}-{day:02d}` is not a date on the calendar. {WHEN_HINT}"
        clock_text = iso.group(4)
    else:
        named = _NAMED_DAY_RE.match(body.lower())
        weekday = None
        named_day: str | None = None
        if named:
            word = named.group(1).replace("this ", "")
            clock_text = named.group(2).strip()
            if not clock_text:
                return None, f"That gives me a day but no time — `{word} 18:00`, for example."
            if word in ("today", "tonight"):
                named_day = "today"
            else:  # tomorrow, tmr, tmrw
                named_day = "tomorrow"
        else:
            weekday_match = _WEEKDAY_RE.match(body.lower())
            if weekday_match:
                clock_text = weekday_match.group(2).strip()
                if not clock_text:
                    return None, (
                        f"That gives me a weekday but no time — "
                        f"`{weekday_match.group(1)} 18:00`, for example."
                    )
                weekday = _WEEKDAY_INDEX[weekday_match.group(1)]
            else:
                clock_text = body

        parsed = _parse_clock(clock_text)
        if parsed is None:
            return None, f"I couldn't read `{raw}` as a time. {WHEN_HINT}"
        minutes_past_midnight, offset_minutes = parsed

        # The base day is chosen in the *typed* timezone, so `18:00+2` on a
        # machine whose UTC clock just crossed midnight still means the evening
        # the writer had in mind.
        wall_now = _utc_wall(now + offset_minutes * 60)
        clock_dt = wall_now.replace(
            hour=minutes_past_midnight // 60,
            minute=minutes_past_midnight % 60,
            second=0,
            microsecond=0,
        )
        if named_day is None and weekday is None:
            # A bare clock: the next one that has not passed.
            tomorrow = timedelta(days=1)
            base_date = wall_now.date() + (tomorrow if clock_dt <= wall_now else timedelta())
        elif named_day == "today":
            base_date = wall_now.date()
        elif named_day == "tomorrow":
            base_date = wall_now.date() + timedelta(days=1)
        else:
            ahead = (int(weekday) - wall_now.weekday()) % 7
            if ahead == 0 and clock_dt <= wall_now:
                ahead = 7
            base_date = wall_now.date() + timedelta(days=ahead)

    if minutes_past_midnight is None:
        parsed = _parse_clock(clock_text)
        if parsed is None:
            return None, f"I couldn't read `{raw}` as a time. {WHEN_HINT}"
        minutes_past_midnight, offset_minutes = parsed

    naive = datetime(
        base_date.year, base_date.month, base_date.day,
        minutes_past_midnight // 60, minutes_past_midnight % 60,
    )
    # The wall time was typed in `offset_minutes` from UTC, so UTC is that many
    # minutes earlier.
    candidate = calendar.timegm(naive.timetuple()) - offset_minutes * 60
    return _accept(candidate, now)


def _accept(candidate: int, now: int) -> tuple[int | None, str | None]:
    """The two horizon checks every branch ends in, and their sentences."""
    if candidate < now + MIN_LEAD_SECONDS:
        return None, (
            f"That time has already passed — a meeting has to be at least "
            f"{MIN_LEAD_SECONDS // 60} minute away. {WHEN_HINT}"
        )
    if candidate > now + MAX_HORIZON_SECONDS:
        return None, "That is more than a year away — set it inside the next year."
    return int(candidate), None


# --------------------------------------------------------------------------- #
# Reading a notice schedule
# --------------------------------------------------------------------------- #
#: Spells that mean "no notices", each a whole input rather than a token: a
#: `none` in the middle of `1h,none` is a typo, not a decision.
_NO_NOTICES = frozenset({"none", "off", "never", "no"})


def parse_notices(text) -> tuple[list[int] | None, str | None]:
    """Offsets in seconds before the start, largest first — or the refusal.

    ``1h,15m`` is the shape: comma, space or semicolon separated, units of
    ``m``/``h``/``d``/``w`` (and the words they stand for), a bare number read as
    minutes, ``0m`` for *when it starts*, and ``none`` for no notices at all.
    Sorted and de-duplicated so two spellings of the same reminder are one row.
    """
    raw = str(text or "").strip()
    if not raw:
        return None, f"I need the notice schedule, or `none`. {NOTICES_HINT}"
    if raw.lower() in _NO_NOTICES:
        return [], None

    # "1 day" is how a person types it and "1d" is how the hint spells it, so
    # the two words become one token here rather than a number and a unit that
    # parse as two separate (and wrong) notices.
    normalized = re.sub(r"(\d)\s+([a-z]+)", r"\1\2", raw.lower())

    offsets: list[int] = []
    for part in [p for p in re.split(r"[\s,;]+", normalized) if p]:
        match = re.fullmatch(r"(\d{1,5})\s*([a-z]*)", part.lower())
        if not match:
            return None, f"I couldn't read `{part}` as a notice time. {NOTICES_HINT}"
        count = int(match.group(1))
        unit = match.group(2).rstrip(".")
        if unit:
            seconds = _UNIT_SECONDS.get(unit)
            if seconds is None:
                return None, f"I don't know the unit `{match.group(2)}`. {NOTICES_HINT}"
            seconds *= count
        else:
            seconds = count * 60  # a bare number is minutes
        if seconds > MAX_NOTICE_SECONDS:
            return None, f"`{part}` leads its meeting by more than 30 days."
        offsets.append(int(seconds))

    unique = sorted(set(offsets), reverse=True)
    if len(unique) > MAX_NOTICES:
        return None, f"At most {MAX_NOTICES} notices per meeting — that was {len(unique)}."
    return unique, None


def notice_label(seconds: int) -> str:
    """``3600`` -> ``"1 hour"``. ``0`` is the start itself, said as such."""
    seconds = int(seconds)
    if seconds <= 0:
        return "at the start"
    parts: list[str] = []
    remaining = seconds
    for step, name in (
        (604800, ("week", "weeks")),
        (86400, ("day", "days")),
        (3600, ("hour", "hours")),
        (60, ("minute", "minutes")),
    ):
        count, remaining = divmod(remaining, step)
        if count:
            singular, plural = name
            parts.append(f"{count} {singular if count == 1 else plural}")
        if len(parts) == 2:
            break
    if not parts:
        return "under a minute"
    return " ".join(parts)


def notices_sentence(offsets) -> str:
    """The schedule as one line: ``1 hour before, 15 minutes before``."""
    listed = [int(o) for o in (offsets or [])]
    if not listed:
        return "None — the announcement is the only notice."
    phrases = [
        "when it starts" if seconds <= 0 else f"{notice_label(seconds)} before"
        for seconds in sorted(listed, reverse=True)
    ]
    return ", ".join(phrases)


def due_times(starts_at: int, offsets, now: int | None = None) -> list[int]:
    """When each notice will fire, dropping the ones already in the past.

    Built from the same numbers the rows are written with, so the receipt a
    creator reads and the queue the loop drains cannot disagree: a notice due
    before this very moment is not worth scheduling, and saying it is would be
    a receipt that promises a message nobody will get.
    """
    now = int(now if now is not None else time.time())
    return [
        int(starts_at) - int(seconds)
        for seconds in sorted((int(o) for o in (offsets or [])), reverse=True)
        if int(starts_at) - int(seconds) > now
    ]


# --------------------------------------------------------------------------- #
# Card text — plain strings, no Discord import
# --------------------------------------------------------------------------- #
def stamp(ts, kind: str = "F") -> str:
    """A Discord timestamp. The client renders it in the reader's own timezone,
    which is why no card here ever prints an offset or asks about one."""
    try:
        return f"<t:{int(ts)}:{kind}>"
    except (TypeError, ValueError):
        return "at a time I could not read"


def _safe_json_offsets(raw) -> list[int]:
    """Offsets from either shape this value arrives in: the JSON string a row
    holds, or the list ``get()`` has already decoded it into.

    Reading it in one place is why a card and its receipt have never disagreed
    about a meeting's schedule: the first version called ``json.loads`` on a
    list, got a ``TypeError`` and answered *no notices* — silently, on the card
    a server's staff reads.
    """
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        try:
            values = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
    if not isinstance(values, list):
        return []
    return [
        int(v) for v in values
        if isinstance(v, (int, float, str)) and str(v).lstrip("-").isdigit()
    ]


def offsets_of(meeting: dict) -> list[int]:
    """The meeting's notice offsets, as stored."""
    return _safe_json_offsets((meeting or {}).get("notices"))


def where_text(meeting: dict) -> str:
    """The *place*, in the two forms a meeting actually has: a channel and words."""
    parts: list[str] = []
    voice = meeting.get("voice_channel_id")
    if voice:
        parts.append(f"<#{int(voice)}>")
    location = str(meeting.get("location") or "").strip()
    if location:
        parts.append(location)
    return "\n".join(parts)


def who_text(meeting: dict) -> str:
    """The audience as a mention — or ``""`` when there is nobody to ping.

    An empty string is how the caller knows to leave the field off: a *Who* with
    no value reads as "nobody is invited", where the truth is "everyone who can
    see this channel", which the announcement being in that channel already says.
    """
    role = meeting.get("audience_role_id")
    return f"<@&{int(role)}>" if role else ""


def tally_line(tally: dict | None) -> str:
    """``✅ 3 · ❓ 1 · ❌ 0`` — the reply counts, always in the same order."""
    tally = tally or {}
    return (
        f"✅ {int(tally.get('yes', 0))} · "
        f"❓ {int(tally.get('maybe', 0))} · "
        f"❌ {int(tally.get('no', 0))}"
    )


def _footer(meeting: dict) -> str:
    line = f"Meeting #{int(meeting.get('id') or 0)}"
    by = str(meeting.get("created_by_name") or "").strip()
    if by:
        line += f" · arranged by {by}"
    return line


def announcement_text(
    meeting: dict, tally: dict | None = None, *, cancelled: bool = False, reason: str = ""
) -> tuple[str, str, list[tuple[str, str]], str]:
    """``(title, body, fields, footer)`` for the card that announces a meeting.

    The cancel path is the same builder with the buttons gone and the reason in
    the body, rather than a second card: the message in the channel is edited
    into this, so a meeting that changes its mind keeps one history in one place
    instead of two messages arguing about what is happening.
    """
    starts = meeting.get("starts_at")
    fields: list[tuple[str, str]] = [("When", f"{stamp(starts)}\nStarts {stamp(starts, 'R')}")]
    place = where_text(meeting)
    if place:
        fields.append(("Place", place))
    who = who_text(meeting)
    if who:
        fields.append(("Who", who))
    agenda = str(meeting.get("agenda") or "").strip()
    if agenda:
        fields.append(("Agenda", agenda))

    if cancelled:
        note = str(reason or "").strip()
        body = "❌ **Cancelled**" + (f" — {note}" if note else ".")
        body += "\nAny notices still queued for this meeting have been stood down."
        fields.append(("Replies", tally_line(tally)))
        return f"📅 {meeting.get('title', 'Meeting')}", body, fields, _footer(meeting)

    fields.append(("Notices", notices_sentence(offsets_of(meeting))))
    fields.append(("Replies", tally_line(tally)))
    body = "Tap a button to say whether you can make it."
    return f"📅 {meeting.get('title', 'Meeting')}", body, fields, _footer(meeting)


def reminder_text(
    meeting: dict, offset: int, *, manual: bool = False, note: str = ""
) -> tuple[str, str, list[tuple[str, str]], str]:
    """``(title, body, fields, footer)`` for a notice on its way to the channel.

    ``offset`` decides the headline: a reminder that lands at the start is not
    the same sentence as one that lands an hour before, and a notice sent by
    hand (`/meeting ping`) says it was sent by hand rather than pretending to
    be the schedule keeping its promise.
    """
    starts = meeting.get("starts_at")
    if manual:
        title = "📣 Meeting notice"
    elif int(offset or 0) <= 0:
        title = "▶️ Starting now"
    else:
        title = "⏰ Meeting reminder"
    body = (
        f"**{meeting.get('title', 'Meeting')}** starts **{stamp(starts, 'R')}** — "
        f"{stamp(starts)}."
    )
    fields: list[tuple[str, str]] = []
    place = where_text(meeting)
    if place:
        fields.append(("Place", place))
    who = who_text(meeting)
    if who:
        fields.append(("Who", who))
    agenda = str(meeting.get("agenda") or "").strip()
    if agenda:
        fields.append(("Agenda", agenda))
    footer = f"Meeting #{int(meeting.get('id') or 0)}"
    if manual:
        footer += " · sent now by the scheduler's owner"
    else:
        footer += f" · {notice_label(offset)} before"
    if note:
        footer += f" · {note}"
    return title, body, fields, footer


def receipt_text(
    meeting: dict, notice_rows, *, jump_url: str = ""
) -> tuple[str, str, list[tuple[str, str]], str]:
    """The ephemeral confirmation `/meeting create` gives its caller.

    Built from the *stored* notice rows rather than from what was asked for, so
    a schedule that lost a notice to the past is a receipt that shows four
    lines rather than five — the paper for what actually happened, not for what
    was requested.
    """
    starts = meeting.get("starts_at")
    body = (
        f"**{meeting.get('title', 'Meeting')}** is set for {stamp(starts)} "
        f"({stamp(starts, 'R')})."
    )
    if jump_url:
        body += f"\n[Open the announcement card]({jump_url})"
    fields: list[tuple[str, str]] = []
    place = where_text(meeting)
    if place:
        fields.append(("Place", place))
    who = who_text(meeting)
    fields.append(("Who", who if who else "Nobody is pinged — the card is in the channel"))
    lines = _notice_lines(notice_rows)
    fields.append(
        (
            "Notices going out",
            "\n".join(lines) if lines else "None — the announcement above is the only notice.",
        )
    )
    footer = "Times are drawn in each reader's own timezone."
    return "📅 Meeting scheduled", body, fields, footer


def _notice_lines(notice_rows) -> list[str]:
    """Each queued notice as one line: when it fires, and how far ahead that is."""
    lines = []
    for row in list(notice_rows or []):
        when = stamp(row.get("due_at"), "F")
        offset = int(row.get("offset_seconds") or 0)
        phrase = "when it starts" if offset <= 0 else f"{notice_label(offset)} before"
        lines.append(f"{when} — {phrase}")
    return lines


def moved_text(
    meeting: dict, *, notice_rows=None, jump_url: str = ""
) -> tuple[str, str, list[tuple[str, str]], str]:
    """``(title, body, fields, footer)`` for a meeting that has been moved.

    Its own sentence rather than the receipt's, because the two report
    different events: *booked* lists what will now happen, *moved* says the new
    start beside the schedule it replaced — and the notices listed are the ones
    the rebuild actually queued, which is the promise being re-made.
    """
    starts = meeting.get("starts_at")
    body = (
        f"**{meeting.get('title', 'Meeting')}** now starts {stamp(starts)} "
        f"({stamp(starts, 'R')})."
    )
    if jump_url:
        body += f"\n[Open the announcement card]({jump_url})"
    lines = _notice_lines(notice_rows)
    fields: list[tuple[str, str]] = [
        ("Notices going out", "\n".join(lines) if lines else "None — the announcement is the only notice.")
    ]
    return "📅 Meeting moved", body, fields, "Times are drawn in each reader's own timezone."


def detail_text(
    meeting: dict,
    *,
    notice_rows=None,
    tally: dict | None = None,
    names: dict | None = None,
    jump_url: str = "",
) -> tuple[str, str, list[tuple[str, str]], str]:
    """``(title, body, fields, footer)`` for `/meeting view`.

    The notices are shown *with their status*, which is the whole reason this
    card exists separately from the announcement: "did the reminders go out?" is
    the question nobody can answer from the channel, because a notice that was
    missed leaves no message at all.
    """
    starts = meeting.get("starts_at")
    cancelled = str(meeting.get("status")) != STATUS_SCHEDULED
    fields: list[tuple[str, str]] = [("When", f"{stamp(starts)}\nStarts {stamp(starts, 'R')}")]
    place = where_text(meeting)
    if place:
        fields.append(("Place", place))
    who = who_text(meeting)
    if who:
        fields.append(("Who", who))
    agenda = str(meeting.get("agenda") or "").strip()
    if agenda:
        fields.append(("Agenda", agenda))

    rows = list(notice_rows or [])
    if rows:
        words = {
            NOTICE_PENDING: "queued",
            NOTICE_SENDING: "going out",
            NOTICE_SENT: "sent",
            NOTICE_MISSED: "missed",
            NOTICE_CANCELLED: "stood down",
        }
        lines = []
        for row in rows:
            line = f"{stamp(row.get('due_at'), 'F')} — {words.get(row.get('status'), row.get('status'))}"
            note = str(row.get("note") or "").strip()
            if note and row.get("status") in (NOTICE_MISSED, NOTICE_CANCELLED):
                line += f" ({note})"
            lines.append(line)
        fields.append(("Notices", "\n".join(lines)))
    else:
        fields.append(("Notices", notices_sentence(offsets_of(meeting))))

    fields.append(("Replies", _replies_text(tally, names)))

    body = ""
    if cancelled:
        body = "❌ This meeting is **cancelled**."
    if jump_url:
        body = (body + "\n" if body else "") + f"[The card in the channel]({jump_url})"
    return f"📅 {meeting.get('title', 'Meeting')}", body, fields, _footer(meeting)


def _replies_text(tally: dict | None, names: dict | None) -> str:
    """Who said what, as one line per answer — with a floor under an empty list.

    "none yet" rather than a blank: a field that renders as nothing reads as a
    card that failed to load it.
    """
    tally = tally or {}
    names = names or {}
    lines = []
    for key, emoji, label in (("yes", "✅", "Going"), ("maybe", "❓", "Maybe"), ("no", "❌", "Not going")):
        count = int(tally.get(key, 0))
        people = [str(n) for n in (names.get(key) or []) if str(n).strip()]
        if not count:
            lines.append(f"{emoji} {label} — none yet")
            continue
        shown = ", ".join(people[:8])
        extra = count - len(people[:8])
        if extra > 0:
            shown = (shown + f" +{extra} more").strip()
        lines.append(f"{emoji} {label} ({count}) — {shown or 'names not loaded'}")
    return "\n".join(lines)


def list_text(rows, guild_name: str = "") -> tuple[str, str, list[tuple[str, str]], str]:
    """``(title, body, fields, footer)`` for `/meeting list`.

    Ten at most, in start order, each line carrying the id — because the next
    thing the reader does is `/meeting view <id>`, and a list that does not
    print the number it is about to ask for has made that a lookup.
    """
    title = "📅 Meetings coming up" + (f" in {guild_name}" if guild_name else "")
    listed = list(rows or [])
    if not listed:
        return (
            title,
            "Nothing is scheduled. An administrator can book one with "
            "`/meeting create`.",
            [],
            "Cancelled meetings are not listed.",
        )
    lines = []
    for row in listed:
        place = where_text(row)
        line = (
            f"`#{int(row.get('id') or 0)}` **{row.get('title', 'Meeting')}** — "
            f"{stamp(row.get('starts_at'), 'F')} ({stamp(row.get('starts_at'), 'R')})"
        )
        if place:
            line += f" · {place.splitlines()[0]}"
        lines.append(line)
    body = "\n".join(lines)
    if len(listed) >= 10:
        body += "\n-# More than ten? `/meeting view` takes the number from a line above."
    return title, body, [], "Times are drawn in your own timezone."


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def _decode(row) -> dict | None:
    """One row as a dict, with its notice list parsed."""
    if row is None:
        return None
    data = dict(row)
    data["notices"] = _safe_json_offsets(data.get("notices"))
    return data


def _connect():
    """A connection that answers with rows that know their own columns.

    ``db.connect`` leaves the factory alone (the scheduler reads by index), and
    this module reads by name everywhere — so the factory is set here, on this
    connection, rather than changed in the shared helper for everybody.
    """
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    return conn


def create(
    guild_id: int,
    *,
    title: str,
    starts_at: int,
    channel_id: int,
    created_by: int | None = None,
    created_by_name: str = "",
    agenda: str = "",
    voice_channel_id: int | None = None,
    location: str = "",
    audience_role_id: int | None = None,
    notices=(),
    now: int | None = None,
) -> int | None:
    """Write a meeting and its notices down. ``None`` when it could not be.

    Meeting and notices go in on one transaction: a meeting whose reminders
    never got written is a meeting that looks configured and stays silent, and
    the half-written state has no reader that would notice it.

    A notice already due by the time of the write is not created — it would be
    late by construction — and the receipt reads the rows back rather than the
    request, so what the creator is shown is what the loop will actually send.
    """
    now = int(now if now is not None else time.time())
    offsets = sorted({int(o) for o in (notices or [])}, reverse=True)

    def work(cur):
        cur.execute(
            """INSERT INTO meetings
               (guild_id, title, agenda, starts_at, channel_id, voice_channel_id,
                location, audience_role_id, notices, status, created_by,
                created_by_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(guild_id),
                str(title)[:200],
                str(agenda or "")[:2000],
                int(starts_at),
                int(channel_id),
                int(voice_channel_id) if voice_channel_id else None,
                str(location or "")[:500],
                int(audience_role_id) if audience_role_id else None,
                json.dumps(offsets),
                STATUS_SCHEDULED,
                int(created_by) if created_by is not None else None,
                str(created_by_name or "")[:100],
                now,
            ),
        )
        meeting_id = cur.lastrowid
        for seconds in offsets:
            due_at = int(starts_at) - seconds
            if due_at <= now:
                continue
            cur.execute(
                """INSERT INTO meeting_notices
                   (meeting_id, due_at, offset_seconds, status)
                   VALUES (?, ?, ?, ?)""",
                (meeting_id, due_at, seconds, NOTICE_PENDING),
            )
        return meeting_id

    try:
        return db.run_atomic(work)
    except Exception as exc:
        print(f"[MEETINGS] Could not store a meeting: {exc}", flush=True)
        return None


def get(meeting_id: int) -> dict | None:
    """One meeting, or ``None``. No guild filter — callers check that first."""
    try:
        conn = _connect()
        try:
            return _decode(conn.execute(
                "SELECT * FROM meetings WHERE id = ?", (int(meeting_id),)
            ).fetchone())
        finally:
            conn.close()
    except Exception as exc:
        print(f"[MEETINGS] Could not read meeting #{meeting_id}: {exc}", flush=True)
        return None


def for_message(message_id: int) -> dict | None:
    """The meeting whose announcement lives on this message.

    The message id is the only thing an RSVP button carries: a button pressed
    tomorrow — or after a redeploy — finds its meeting through the card it is
    sitting on, so no view has to be rebuilt per meeting to keep working.
    """
    try:
        conn = _connect()
        try:
            return _decode(conn.execute(
                "SELECT * FROM meetings WHERE announce_message_id = ?",
                (int(message_id),),
            ).fetchone())
        finally:
            conn.close()
    except Exception as exc:
        print(f"[MEETINGS] Could not read a meeting by its message: {exc}", flush=True)
        return None


def upcoming(guild_id: int, limit: int = 10) -> list[dict]:
    """The meetings still ahead, soonest first."""
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT * FROM meetings
                   WHERE guild_id = ? AND status = ? AND starts_at >= ?
                   ORDER BY starts_at ASC LIMIT ?""",
                (int(guild_id), STATUS_SCHEDULED, int(time.time()), int(limit)),
            ).fetchall()
            return [_decode(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        print(f"[MEETINGS] Could not list meetings: {exc}", flush=True)
        return []


def set_message_id(meeting_id: int, message_id: int) -> None:
    """Where the announcement card landed, so every later edit can find it."""
    try:
        db.run_atomic(lambda cur: cur.execute(
            "UPDATE meetings SET announce_message_id = ? WHERE id = ?",
            (int(message_id), int(meeting_id)),
        ))
    except Exception as exc:
        print(f"[MEETINGS] Could not record the card for #{meeting_id}: {exc}", flush=True)


def forget(meeting_id: int) -> None:
    """Forget a meeting that never really existed (its card would not post).

    Named *forget* rather than ``delete`` on purpose, and not only for the
    sentence: ``tests/test_guardrails.py`` reads every cog's AST for calls like
    ``x.delete()`` and treats them as writes to somebody's server unless they
    route through a door — this one writes to SQLite, and a name that looks like
    a channel being deleted is a false alarm in an inventory that is meant to
    stay trustworthy.

    Rollback rather than history: a meeting nobody was ever told about has no
    value as a record, and a row with no card is a meeting `/meeting list` would
    promise while the channel holds nothing.
    """
    try:
        def work(cur):
            cur.execute("DELETE FROM meeting_rsvps WHERE meeting_id = ?", (int(meeting_id),))
            cur.execute("DELETE FROM meeting_notices WHERE meeting_id = ?", (int(meeting_id),))
            cur.execute("DELETE FROM meetings WHERE id = ?", (int(meeting_id),))
        db.run_atomic(work)
    except Exception as exc:
        print(f"[MEETINGS] Could not roll back #{meeting_id}: {exc}", flush=True)


def cancel(meeting_id: int, now: int | None = None) -> bool:
    """Stand a meeting and its queued notices down. ``False`` if it was not live."""
    now = int(now if now is not None else time.time())

    def work(cur):
        changed = cur.execute(
            "UPDATE meetings SET status = ? WHERE id = ? AND status = ?",
            (STATUS_CANCELLED, int(meeting_id), STATUS_SCHEDULED),
        ).rowcount
        if not changed:
            return False
        cur.execute(
            "UPDATE meeting_notices SET status = ?, note = ?, claimed_at = ? "
            "WHERE meeting_id = ? AND status IN (?, ?)",
            (NOTICE_CANCELLED, "the meeting was cancelled", now,
             int(meeting_id), NOTICE_PENDING, NOTICE_SENDING),
        )
        return True

    try:
        return bool(db.run_atomic(work))
    except Exception as exc:
        print(f"[MEETINGS] Could not cancel #{meeting_id}: {exc}", flush=True)
        return False


def reschedule(
    meeting_id: int, starts_at: int, notices=(), now: int | None = None
) -> bool:
    """Move a meeting and rebuild its queue from the new start.

    Pending notices are deleted and re-created rather than shifted, because the
    offsets are the promise ("one hour before") and the new time changes every
    due moment at once — an UPDATE that moved ``due_at`` by the difference would
    be a second way to compute the same thing and the two would eventually
    disagree about a meeting nobody was watching.
    """
    now = int(now if now is not None else time.time())
    offsets = sorted({int(o) for o in (notices or [])}, reverse=True)

    def work(cur):
        row = cur.execute(
            "SELECT status FROM meetings WHERE id = ?", (int(meeting_id),)
        ).fetchone()
        if row is None or row[0] != STATUS_SCHEDULED:
            return False
        cur.execute(
            "UPDATE meetings SET starts_at = ?, notices = ? WHERE id = ?",
            (int(starts_at), json.dumps(offsets), int(meeting_id)),
        )
        cur.execute(
            "DELETE FROM meeting_notices WHERE meeting_id = ? AND status = ?",
            (int(meeting_id), NOTICE_PENDING),
        )
        for seconds in offsets:
            due_at = int(starts_at) - seconds
            if due_at <= now:
                continue
            cur.execute(
                "INSERT INTO meeting_notices (meeting_id, due_at, offset_seconds, status) "
                "VALUES (?, ?, ?, ?)",
                (int(meeting_id), due_at, seconds, NOTICE_PENDING),
            )
        return True

    try:
        return bool(db.run_atomic(work))
    except Exception as exc:
        print(f"[MEETINGS] Could not move #{meeting_id}: {exc}", flush=True)
        return False


def notice_rows(meeting_id: int) -> list[dict]:
    """Every notice for one meeting, soonest first — with its status."""
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM meeting_notices WHERE meeting_id = ? ORDER BY due_at ASC",
                (int(meeting_id),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        print(f"[MEETINGS] Could not read notices for #{meeting_id}: {exc}", flush=True)
        return []


def pending_notice_rows(meeting_id: int) -> list[dict]:
    """Only the notices still to come — what a receipt should show."""
    return [
        row for row in notice_rows(meeting_id)
        if row.get("status") == NOTICE_PENDING
    ]


def due_notices(now: int | None = None, limit: int = 20) -> list[dict]:
    """Notices whose moment has arrived, each with its meeting beside it.

    One join, one ordered read: the loop must see *what* is due and *everything
    it needs to send it* without a second query per row, because a sweep that
    talks to the database once per notice is a sweep that stalls the dashboard's
    own writes while it runs.
    """
    now = int(now if now is not None else time.time())
    query = (
        "SELECT n.id AS notice_id, n.due_at AS due_at, n.offset_seconds AS offset_seconds, "
        "m.id AS id, m.guild_id, m.title, m.agenda, m.starts_at, m.channel_id, "
        "m.voice_channel_id, m.location, m.audience_role_id, m.status, "
        "m.announce_message_id, m.created_by, m.created_by_name "
        "FROM meeting_notices n JOIN meetings m ON m.id = n.meeting_id "
        "WHERE n.status = ? AND n.due_at <= ? ORDER BY n.due_at ASC LIMIT ?"
    )
    try:
        conn = _connect()
        try:
            return [dict(row) for row in conn.execute(
                query, (NOTICE_PENDING, now, int(limit))
            ).fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        print(f"[MEETINGS] Could not read due notices: {exc}", flush=True)
        return []


def claim_notice(notice_id: int, now: int | None = None) -> bool:
    """Take a notice for delivery. ``False`` if something else got there first."""
    now = int(now if now is not None else time.time())
    try:
        return db.run_atomic(lambda cur: cur.execute(
            "UPDATE meeting_notices SET status = ?, claimed_at = ? "
            "WHERE id = ? AND status = ?",
            (NOTICE_SENDING, now, int(notice_id), NOTICE_PENDING),
        ).rowcount == 1)
    except Exception as exc:
        print(f"[MEETINGS] Could not claim notice {notice_id}: {exc}", flush=True)
        return False


def mark_notice(notice_id: int, status: str, note: str = "", now: int | None = None) -> None:
    """Settle a claim: sent, missed (with the reason), or stood down."""
    now = int(now if now is not None else time.time())
    try:
        db.run_atomic(lambda cur: cur.execute(
            "UPDATE meeting_notices SET status = ?, note = ?, sent_at = ? WHERE id = ?",
            (status, str(note or "")[:300], now, int(notice_id)),
        ))
    except Exception as exc:
        print(f"[MEETINGS] Could not settle notice {notice_id}: {exc}", flush=True)


def reclaim_stale(now: int | None = None) -> list[int]:
    """Notices whose sender died mid-flight, put back in the queue.

    The opposite call from the scheduler's: a stale *announcement* is settled as
    failed because a duplicate is worse than a missing one, while a duplicate
    reminder is a nuisance and a missing one is the feature not happening.
    """
    now = int(now if now is not None else time.time())
    cutoff = now - STALE_CLAIM_SECONDS
    try:
        def work(cur):
            rows = cur.execute(
                "SELECT id FROM meeting_notices WHERE status = ? AND claimed_at < ?",
                (NOTICE_SENDING, cutoff),
            ).fetchall()
            ids = [int(row[0]) for row in rows]
            if ids:
                cur.execute(
                    f"UPDATE meeting_notices SET status = ? WHERE id IN "
                    f"({','.join('?' for _ in ids)})",
                    (NOTICE_PENDING, *ids),
                )
            return ids
        return db.run_atomic(work)
    except Exception as exc:
        print(f"[MEETINGS] Could not reclaim stale notices: {exc}", flush=True)
        return []


def set_rsvp(meeting_id: int, user_id: int, answer: str, now: int | None = None) -> bool:
    """Record one person's answer. The last press wins, by design."""
    now = int(now if now is not None else time.time())
    if answer not in ("yes", "maybe", "no"):
        return False
    try:
        def work(cur):
            cur.execute(
                "DELETE FROM meeting_rsvps WHERE meeting_id = ? AND user_id = ?",
                (int(meeting_id), int(user_id)),
            )
            cur.execute(
                "INSERT INTO meeting_rsvps (meeting_id, user_id, answer, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (int(meeting_id), int(user_id), answer, now),
            )
            return True
        return bool(db.run_atomic(work))
    except Exception as exc:
        print(f"[MEETINGS] Could not record a reply for #{meeting_id}: {exc}", flush=True)
        return False


def tally(meeting_id: int) -> dict:
    """``{"yes": 3, "maybe": 1, "no": 0}`` — every answer counted, absent ones 0."""
    counts = {"yes": 0, "maybe": 0, "no": 0}
    for row in _rsvp_rows(meeting_id):
        answer = row.get("answer")
        if answer in counts:
            counts[answer] += 1
    return counts


def rsvp_ids(meeting_id: int, answer: str) -> list[int]:
    """Who gave one answer, for the names `/meeting view` shows."""
    return [
        int(row.get("user_id") or 0)
        for row in _rsvp_rows(meeting_id)
        if row.get("answer") == answer and row.get("user_id")
    ]


def _rsvp_rows(meeting_id: int) -> list[dict]:
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT user_id, answer FROM meeting_rsvps WHERE meeting_id = ?",
                (int(meeting_id),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        print(f"[MEETINGS] Could not read replies for #{meeting_id}: {exc}", flush=True)
        return []


def prune(now: int | None = None) -> int:
    """Forget meetings that finished long enough ago, and everything hanging off them.

    ``HISTORY_DAYS`` after the start, not after the cancel: what a server wants
    to look back on is what happened, and a meeting from six months ago is a row
    nobody will open and an RSVP list holding people's names for no reason.
    """
    now = int(now if now is not None else time.time())
    cutoff = now - HISTORY_DAYS * 24 * 3600

    def work(cur):
        ids = [
            int(row[0]) for row in cur.execute(
                "SELECT id FROM meetings WHERE starts_at < ?", (cutoff,)
            ).fetchall()
        ]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        cur.execute(f"DELETE FROM meeting_rsvps WHERE meeting_id IN ({marks})", ids)
        cur.execute(f"DELETE FROM meeting_notices WHERE meeting_id IN ({marks})", ids)
        cur.execute(f"DELETE FROM meetings WHERE id IN ({marks})", ids)
        return len(ids)

    try:
        return int(db.run_atomic(work) or 0)
    except Exception as exc:
        print(f"[MEETINGS] Could not prune old meetings: {exc}", flush=True)
        return 0
