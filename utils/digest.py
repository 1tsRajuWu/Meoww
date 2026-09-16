"""Messages that are *built* when they post, instead of typed when they were queued.

A scheduled message normally carries the text it was written with, which is
exactly right for an announcement: what you approved is what goes out. It is
useless for the other half of what a server wants posted on a timer — "the weekly
report", "how busy was this week" — because that content does not exist until the
moment it is due, and it comes from data the bot has been collecting all along
(``activity_messages`` / ``activity_voice``, the tables behind ``/activity``).

So a schedule may name a **generator** instead of carrying text. The generator
runs at fire time, on the delivery loop, and returns the same payload shape the
composer produces — which means every guarantee the queue already has (claim,
retry, skip-if-too-late, per-channel result) applies to generated content
unchanged.

Two rules keep generators honest:

* **Nothing to say is not a failure.** A quiet week returns ``None`` with a
  reason, and the caller records it as a *missed* run — so a weekly digest does
  not post a card of zeros, does not count against the schedule's failure streak,
  and the schedule stays alive for next week.
* **The window is fixed and stated.** The digest covers the last seven days,
  inclusive of today, in UTC — the same rule ``cogs/tracker.py`` uses for
  ``/activity week``, so the number the bot posts and the number an administrator
  gets from the command agree.
"""
import datetime

from utils import db

#: How many entries each "top" section of the digest lists.
TOP_N = 3

#: Longest window any generator may describe, in days. Not a limit on the read —
#: a limit on what the card claims, so "last 7 days" cannot quietly become a year.
WINDOW_DAYS = 7


def human_count(n: int) -> str:
    """``1234`` → ``1,234``. Shared with the tracker so the numbers read alike."""
    return f"{int(n):,}"


def human_voice(seconds: int) -> str:
    """Seconds in the tracker's own shape: ``45s`` / ``12m 30s`` / ``3.5 h``.

    Deliberately identical to what ``/activity`` prints: the digest and the
    command describe the same seconds, and two spellings of one duration is how a
    reader concludes the numbers disagree.
    """
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    hours = seconds / 3600
    if hours >= 1:
        return f"{hours:.2f} h".rstrip("0").rstrip(".")
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}m {rest}s" if rest else f"{minutes}m"


def _window(now: int) -> dict:
    """The reporting window and the one before it, as inclusive UTC dates.

    Seven days *including* today (``today - 6 … today``), which is what the
    tracker's ``week`` range means. The previous window is the seven days before
    that, so the comparison is like-for-like rather than "this week so far"
    against "all of last week".
    """
    today = datetime.datetime.fromtimestamp(int(now), datetime.timezone.utc).date()
    start = today - datetime.timedelta(days=WINDOW_DAYS - 1)
    prev_end = start - datetime.timedelta(days=1)
    prev_start = prev_end - datetime.timedelta(days=WINDOW_DAYS - 1)
    return {
        "start": start.isoformat(),
        "end": today.isoformat(),
        "prev_start": prev_start.isoformat(),
        "prev_end": prev_end.isoformat(),
    }


def _scalar(conn, sql: str, params) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def _rows(conn, sql: str, params) -> list:
    return list(conn.execute(sql, params).fetchall())


def server_activity(guild_id: int, now: int) -> dict:
    """Everything the digest card needs, in one read of the tracker's tables.

    Synchronous on purpose: it runs on the delivery loop, and the project's
    established pattern for loop-side SQL is a short synchronous read
    (``utils/shop``, ``utils/loans``) rather than a second async path into the
    same file.
    """
    window = _window(now)
    conn = db.connect()
    try:
        def totals(start: str, end: str):
            return {
                "messages": _scalar(
                    conn,
                    "SELECT COALESCE(SUM(count), 0) FROM activity_messages "
                    "WHERE guild_id = ? AND date BETWEEN ? AND ?",
                    (guild_id, start, end),
                ),
                "voice_seconds": _scalar(
                    conn,
                    "SELECT COALESCE(SUM(seconds), 0) FROM activity_voice "
                    "WHERE guild_id = ? AND date BETWEEN ? AND ?",
                    (guild_id, start, end),
                ),
            }

        current = totals(window["start"], window["end"])
        previous = totals(window["prev_start"], window["prev_end"])

        members = _scalar(
            conn,
            "SELECT COUNT(*) FROM ("
            "  SELECT user_id FROM activity_messages WHERE guild_id = ? AND date BETWEEN ? AND ?"
            "  UNION"
            "  SELECT user_id FROM activity_voice WHERE guild_id = ? AND date BETWEEN ? AND ?"
            ")",
            (
                guild_id, window["start"], window["end"],
                guild_id, window["start"], window["end"],
            ),
        )
        top_message_channels = _rows(
            conn,
            "SELECT channel_id, SUM(count) AS total FROM activity_messages "
            "WHERE guild_id = ? AND date BETWEEN ? AND ? "
            "GROUP BY channel_id HAVING total > 0 ORDER BY total DESC LIMIT ?",
            (guild_id, window["start"], window["end"], TOP_N),
        )
        top_voice_channels = _rows(
            conn,
            "SELECT channel_id, SUM(seconds) AS total FROM activity_voice "
            "WHERE guild_id = ? AND date BETWEEN ? AND ? "
            "GROUP BY channel_id HAVING total > 0 ORDER BY total DESC LIMIT ?",
            (guild_id, window["start"], window["end"], TOP_N),
        )
        top_members = _rows(
            conn,
            "SELECT user_id, SUM(count) AS total FROM activity_messages "
            "WHERE guild_id = ? AND date BETWEEN ? AND ? "
            "GROUP BY user_id HAVING total > 0 ORDER BY total DESC LIMIT ?",
            (guild_id, window["start"], window["end"], TOP_N),
        )
        member_voice = {}
        if top_members:
            ids = [int(row[0]) for row in top_members]
            placeholders = ",".join("?" for _ in ids)
            for uid, seconds in _rows(
                conn,
                "SELECT user_id, SUM(seconds) FROM activity_voice "
                f"WHERE guild_id = ? AND date BETWEEN ? AND ? AND user_id IN ({placeholders}) "
                "GROUP BY user_id",
                (guild_id, window["start"], window["end"], *ids),
            ):
                member_voice[int(uid)] = int(seconds or 0)
    finally:
        conn.close()
    return {
        "window": window,
        "messages": current["messages"],
        "voice_seconds": current["voice_seconds"],
        "prev_messages": previous["messages"],
        "prev_voice_seconds": previous["voice_seconds"],
        "members": members,
        "message_channels": [(int(cid), int(total)) for cid, total in top_message_channels],
        "voice_channels": [(int(cid), int(total)) for cid, total in top_voice_channels],
        "members_by_messages": [(int(uid), int(total)) for uid, total in top_members],
        "member_voice": member_voice,
    }


def _change(current: int, previous: int) -> str:
    """A like-for-like comparison, or an honest refusal to invent one."""
    if previous <= 0:
        return "no comparable week before this one"
    delta = (current - previous) / previous * 100
    if abs(delta) < 1:
        return "about the same as the week before"
    return f"{'+' if delta > 0 else ''}{delta:.0f}% vs the week before"


def _channel_name(guild, channel_id: int, kind: str) -> str:
    channel = guild.get_channel(channel_id) if guild is not None else None
    name = getattr(channel, "name", None)
    if not name:
        # A channel deleted since is still worth counting — the activity happened —
        # so it is named by id rather than dropped from the totals.
        return f"a channel since deleted ({channel_id})"
    return f"🔊 {name}" if kind == "voice" else f"#{name}"


def _member_name(guild, user_id: int):
    member = guild.get_member(user_id) if guild is not None else None
    if member is None:
        return None
    return member.display_name or member.name


def weekly_digest(guild, now: int):
    """The server's last week, as an embed payload. ``(payload, reason)``.

    Reaches for the live guild to turn stored ids into names, so a channel that
    has been renamed appears as it is called *now* — the stored row only ever held
    the activity, never the label.
    """
    data = server_activity(int(guild.id), now)
    if not data["messages"] and not data["voice_seconds"]:
        return None, (
            f"nothing to report — no messages or voice activity in the "
            f"{WINDOW_DAYS} days to {data['window']['end']}"
        )

    start, end = data["window"]["start"], data["window"]["end"]
    lines = [
        f"**{human_count(data['messages'])} messages** from "
        f"{human_count(data['members'])} members · "
        f"**{human_voice(data['voice_seconds'])}** in voice",
        f"-# {start} → {end} (UTC) · "
        f"{_change(data['messages'], data['prev_messages'])} messages · "
        f"{_change(data['voice_seconds'], data['prev_voice_seconds'])} voice",
    ]

    if data["message_channels"]:
        lines.append("\n**Busiest text channels**")
        for index, (cid, total) in enumerate(data["message_channels"], start=1):
            lines.append(
                f"{index}. {_channel_name(guild, cid, 'text')} — "
                f"{human_count(total)} messages"
            )

    if data["voice_channels"]:
        lines.append("\n**Busiest voice channels**")
        for index, (cid, total) in enumerate(data["voice_channels"], start=1):
            lines.append(
                f"{index}. {_channel_name(guild, cid, 'voice')} — {human_voice(total)}"
            )

    talkers = []
    for uid, total in data["members_by_messages"]:
        name = _member_name(guild, uid)
        if name is None:
            # A member who has left cannot be featured under their nickname, and a
            # bare id reads as a bug — so they are counted in the totals above and
            # left out of the name list.
            continue
        voice = human_voice(data["member_voice"].get(uid, 0))
        talkers.append(f"{len(talkers) + 1}. {name} — {human_count(total)} messages · {voice}")
    if talkers:
        lines.append("\n**Top talkers**")
        lines.extend(talkers)

    description = "\n".join(lines)[:4000]
    return {
        # No content: an embed on its own, and no mentions at all — a weekly
        # summary must never ping anyone.
        "content": None,
        "embed": {
            "title": f"📊 {getattr(guild, 'name', 'This server')} — last {WINDOW_DAYS} days",
            "description": description,
        },
        "users": [],
        "roles": [],
        "everyone": False,
        "ignored_mentions": [],
    }, ""


#: What a schedule may ask for by name. The label is what the dashboard offers, so
#: the page can only choose a generator this module can actually build.
GENERATORS: dict[str, dict] = {
    "weekly_digest": {
        "label": "Weekly server digest",
        "blurb": "Messages, voice time and the busiest channels — built when it posts.",
        "build": weekly_digest,
    },
}


def label(name: str) -> str:
    entry = GENERATORS.get(name)
    return entry["label"] if entry else ""


def build(name: str, guild, now: int):
    """``(payload, reason)`` — ``payload`` is ``None`` when there is nothing to say."""
    entry = GENERATORS.get(name)
    if entry is None:
        return None, "that content generator no longer exists"
    try:
        return entry["build"](guild, now)
    except Exception as e:  # a broken generator must not take the queue down
        return None, f"could not build {entry['label'].lower()}: {e}"
