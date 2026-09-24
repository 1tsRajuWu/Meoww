"""Anonymous questions: what is relayed, what is kept, and who may read which half.

A question box is a promise, and the promise is one sentence: **the server sees
the question, one person sees who asked it, and the asker gets the answer in a
DM.** Everything in this module is that sentence, kept where it can be tested
without a gateway — the relay body, the record, and the two readings of it.

Three decisions worth stating, because each of them is a way the promise breaks:

* **The relay is a webhook, not a message the bot sends.** ``/qna`` posts the
  question through a webhook URL from the environment
  (:data:`SETTING`), so what lands in the channel is a message whose author is
  the webhook — there is no reply, no mention, no profile to click, and nothing
  Discord can resolve back to the member. A webhook is also the honest answer to
  "where does this go": one channel, configured once, not the channel the person
  happened to type in.
* **The asker's name is kept, and read by exactly one reader.** The public body
  (:func:`relay_payload`) is built from a closed field list and cannot contain an
  id or a name, which is asserted rather than intended — ``tests/test_qna.py``
  builds a payload from a row whose asker is distinctive and fails if any of it
  appears. The private reading (:func:`owner_fields`) is the other half, and it
  is the *only* place the name is rendered.
* **The anonymous handle is derived, never stored.** ``anon 4F2A`` is the
  question's own id in hex, so it is unique, stable, and carries no information
  about the person — a stored random handle would be a second identifier to
  correlate, and could collide, and a handle that is the id cannot.

`/qna` is per-server like every other command, but the webhook is not: one
deployment, one box. A server that wants questions to land in *its* channel sets
its own ``QNA_WEBHOOK_URL`` — which is the reason this is an environment variable
and not a command, since a webhook URL is a credential (anybody holding it can
post as the box) and a member must never be able to set one.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone

from . import config_audit, db, style, version

#: The setting that switches the relay on. Read through the literal rather than
#: through this constant because the config audit scans the source for a quoted
#: name handed to the getter, and a name arriving in a variable is invisible to
#: it — a setting read only through a constant is reported as one *nothing*
#: reads, and its absence stops being mentioned at all.
SETTING = "QNA_WEBHOOK_URL"

#: Where the questions live. Created at boot *and* on first use, because a
#: database restored from a backup taken before this existed would otherwise
#: make the whole feature fail rather than simply start empty.
TABLE = "qna_questions"

#: What the relayed message is posted *as*. This is the anonymity: not a
#: disguised name, an absent one.
PUBLIC_NAME = "Anonymous"

#: The accent the public question card is drawn in — the product's own accent,
#: not a sixth colour invented for one feature. Read from the one author of the
#: palette rather than typed as a literal: this line was a second declaration of
#: the same blurple, so a rebrand would have moved every card except this one.
ACCENT = style.ACCENT

#: Long enough for a real question and an answer that replies to it; short enough
#: that neither can become a wall in a DM or a channel.
MAX_QUESTION = 1000
MAX_ANSWER = 1000

#: The two states a question can be in. There is no third: a question is waiting
#: or it has been answered, and "relayed" is a separate fact about the same row.
OPEN, ANSWERED = "open", "answered"

#: A webhook that has not answered in eight seconds is a webhook that is down,
#: and the asker is waiting for an answer either way.
TIMEOUT = 8.0

#: India Standard Time — the same clock the release notes are sent on and the
#: same stamp format, because a reader comparing the two should not have to
#: wonder whether 03:33 means the same thing in both. Owned by ``utils.version``,
#: which explains the fixed offset (`zoneinfo` is not installed on a slim host).
IST = version.IST

#: Every column, in the order the queries below select them.
COLUMNS = (
    "id", "guild_id", "guild_name", "user_id", "user_name", "question",
    "asked_at", "relayed", "relay_detail", "owner_message_id", "status",
    "answered_at", "answer",
)

CREATE_TABLE = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "guild_id INTEGER NOT NULL DEFAULT 0,"
    "guild_name TEXT NOT NULL DEFAULT '',"
    # The asker. Two columns, written once, read only by the owner's own
    # surfaces — see the module docstring: the public payload is built from a
    # closed field list and never touches either of these.
    "user_id INTEGER NOT NULL DEFAULT 0,"
    "user_name TEXT NOT NULL DEFAULT '',"
    "question TEXT NOT NULL DEFAULT '',"
    "asked_at REAL NOT NULL DEFAULT 0,"
    "relayed INTEGER NOT NULL DEFAULT 0,"
    "relay_detail TEXT NOT NULL DEFAULT '',"
    # The id of the owner's DM about this question, which is how the Answer
    # button on it finds the question again after a restart.
    "owner_message_id INTEGER NOT NULL DEFAULT 0,"
    "status TEXT NOT NULL DEFAULT 'open',"
    "answered_at REAL NOT NULL DEFAULT 0,"
    "answer TEXT NOT NULL DEFAULT ''"
    ")"
)

#: Newest first, which is the order an inbox is read in.
_ORDER = "ORDER BY asked_at DESC, id DESC"


async def ensure_table(conn) -> None:
    """Create the table if it is missing. Safe to call on every read and write."""
    await conn.execute(CREATE_TABLE)


def _row(row) -> dict | None:
    """One database row as a dict with the types the rest of this module expects."""
    if row is None:
        return None
    record = dict(zip(COLUMNS, row))
    for key in ("id", "guild_id", "user_id", "owner_message_id"):
        try:
            record[key] = int(record.get(key) or 0)
        except (TypeError, ValueError):
            record[key] = 0
    try:
        record["asked_at"] = float(record.get("asked_at") or 0.0)
    except (TypeError, ValueError):
        record["asked_at"] = 0.0
    record["relayed"] = bool(record.get("relayed"))
    return record


# --------------------------------------------------------------------------- #
# Reading and writing the record
# --------------------------------------------------------------------------- #
async def record(
    *,
    guild_id: int | None,
    guild_name: str,
    user_id: int,
    user_name: str,
    question: str,
    asked_at: float | None = None,
) -> dict:
    """Write a question down, and hand it back with its id.

    Written *before* the relay is attempted, so a webhook that refuses the
    question leaves a row saying so rather than losing it: the owner's inbox is
    then the honest answer to "did my question go anywhere".
    """
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        cursor = await conn.execute(
            f"INSERT INTO {TABLE} "
            "(guild_id, guild_name, user_id, user_name, question, asked_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                int(guild_id or 0),
                str(guild_name or "")[:100],
                int(user_id or 0),
                str(user_name or "")[:100],
                str(question or "")[:MAX_QUESTION],
                float(asked_at if asked_at is not None else time.time()),
            ),
        )
        new_id = cursor.lastrowid
        await conn.commit()
    return await get(new_id) or {}


async def get(question_id: int) -> dict | None:
    """One question by id, or ``None``."""
    try:
        wanted = int(question_id)
    except (TypeError, ValueError):
        return None
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        async with conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE id = ?", (wanted,)
        ) as cursor:
            row = await cursor.fetchone()
    return _row(row)


async def for_owner_message(message_id: int) -> dict | None:
    """The question an owner DM is about, or ``None``.

    This is the whole reason the owner's card can carry a durable button: the
    message id is written down when the card is sent, and the button reads it
    back off the interaction instead of carrying the question in its
    ``custom_id`` (a fixed ``custom_id`` is what makes one registered view answer
    every card, years of cards included).
    """
    try:
        wanted = int(message_id or 0)
    except (TypeError, ValueError):
        return None
    if not wanted:
        return None
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        async with conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} "
            "WHERE owner_message_id = ? " + _ORDER + " LIMIT 1",
            (wanted,),
        ) as cursor:
            row = await cursor.fetchone()
    return _row(row)


async def attach_owner_message(question_id: int, message_id: int) -> None:
    """Remember which DM tells the owner about this question."""
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        await conn.execute(
            f"UPDATE {TABLE} SET owner_message_id = ? WHERE id = ?",
            (int(message_id or 0), int(question_id)),
        )
        await conn.commit()


async def note_relay(question_id: int, ok: bool, detail: str = "") -> None:
    """Record whether the webhook took the question, and why not when it did not."""
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        await conn.execute(
            f"UPDATE {TABLE} SET relayed = ?, relay_detail = ? WHERE id = ?",
            (1 if ok else 0, str(detail or "")[:300], int(question_id)),
        )
        await conn.commit()


async def mark_answered(question_id: int, answer: str) -> bool:
    """Close a question, but only if it is still open.

    The conditional is the point: two clicks on an old Answer button, or a reply
    from the inbox and one from the DM, must not DM the asker twice. Returns
    whether this call was the one that closed it.
    """
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        cursor = await conn.execute(
            f"UPDATE {TABLE} SET status = ?, answer = ?, answered_at = ? "
            "WHERE id = ? AND status = ?",
            (
                ANSWERED,
                str(answer or "")[:MAX_ANSWER],
                time.time(),
                int(question_id),
                OPEN,
            ),
        )
        changed = cursor.rowcount
        await conn.commit()
    return bool(changed)


async def reopen(question_id: int) -> None:
    """Put a question back in the inbox when its answer could not be delivered.

    The other half of :func:`mark_answered`'s conditional claim: an answer that
    was never DMed has not been given, so the question is not closed, and the
    owner's next attempt is a fresh one rather than a lost one.

    ``relay_detail`` is left alone — it is about the post, not the answer — and
    the answer itself is cleared, because it is about to be written again.
    """
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        await conn.execute(
            f"UPDATE {TABLE} SET status = ?, answer = '', answered_at = 0 "
            "WHERE id = ? AND status = ?",
            (OPEN, int(question_id), ANSWERED),
        )
        await conn.commit()


async def open_questions(*, guild_id: int | None = None, limit: int = 10) -> list[dict]:
    """The questions still waiting for an answer, newest first.

    ``guild_id`` filters to one server; ``None`` (the owner asking from a DM, or
    from the dashboard) is every server, which is the operator's reading.
    """
    where, params = "", []
    if guild_id:
        where, params = "WHERE status = ? AND guild_id = ?", [OPEN, int(guild_id)]
    else:
        where, params = "WHERE status = ?", [OPEN]
    params.append(max(1, min(int(limit or 10), 25)))
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        async with conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} {where} {_ORDER} LIMIT ?",
            tuple(params),
        ) as cursor:
            rows = await cursor.fetchall()
    return [record for record in (_row(row) for row in rows) if record is not None]


async def counts() -> dict:
    """How many questions are waiting and how many have been answered."""
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        out = {"open": 0, "answered": 0}
        async with conn.execute(
            f"SELECT status, COUNT(*) FROM {TABLE} GROUP BY status"
        ) as cursor:
            async for status, number in cursor:
                out[str(status)] = int(number)
    return out


# --------------------------------------------------------------------------- #
# The two readings of one question
# --------------------------------------------------------------------------- #
def handle(record) -> str:
    """The public name of one question: ``anon 4F2A``.

    Derived from the id, so it is unique and stable and says nothing — a stored
    random handle would be a second identifier to correlate between the channel
    and the inbox, and could collide, which the id cannot.
    """
    try:
        number = int((record or {}).get("id") or 0)
    except (TypeError, ValueError):
        number = 0
    return f"anon {number:04X}"


def ist_stamp(when) -> str:
    """A moment as ``22 Sep 2026, 03:33 IST`` — the operator's own clock.

    A bad or absent value becomes *now* rather than an empty string: this is a
    line in a card about a real question, and a card with no time on it reads as
    the one thing the inbox must never say — that it does not know when this
    arrived.
    """
    if when in (None, ""):
        return version.stamp()
    try:
        return version.stamp(float(when))
    except (TypeError, ValueError, OSError, OverflowError):
        return version.stamp()


def question_text(record) -> str:
    """The question itself, trimmed and capped to what Discord will render.

    Public because both readings render it — the relayed message and the owner's
    card are the same question, and two copies of "trim it" is how they start to
    differ.
    """
    text = str((record or {}).get("question") or "").strip()
    return text[:MAX_QUESTION] or "*(the question arrived empty)*"


def relay_payload(record, *, username: str = PUBLIC_NAME) -> dict:
    """The exact body the relay webhook is given.

    A closed field list, and every one of them is about the *question*: the id
    (as a public handle), the text, and the moment. The asker's name and id are
    not here, ``username`` is a constant, and ``allowed_mentions`` is empty —
    which is not politeness but the point: the question is a stranger's text, and
    a question that can ping ``@everyone`` is not anonymous.
    """
    return {
        "username": username,
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "❓ Anonymous question",
                "description": question_text(record),
                "color": ACCENT,
                "footer": {
                    "text": f"{handle(record)} · ask your own with /qna",
                },
                "timestamp": datetime.fromtimestamp(
                    float((record or {}).get("asked_at") or time.time()), tz=timezone.utc
                ).isoformat(),
            }
        ],
    }


def owner_fields(record) -> list[tuple[str, str]]:
    """The private half: who asked, and when.

    Rendered **only** in the owner's own DM and inbox. The name and the id are
    both here rather than just the mention, because the operator's use for this
    is deciding whether an answer is worth more than one DM ("this is the third
    question from the same person") — and a mention renders as a clickable name
    only for somebody who shares a server with them, which is not guaranteed.
    """
    record = record or {}
    return [
        ("From", f"**{record.get('user_name') or 'unknown'}** · `{record.get('user_id')}`"),
        ("Asked", ist_stamp(record.get("asked_at"))),
        ("Server", str(record.get("guild_name") or "unknown")),
        ("Public as", handle(record)),
    ]


def owner_row(record) -> dict:
    """One question as an operator's *own* screen shows it: the private reading.

    This is :func:`owner_fields` for a surface that renders in HTML rather than
    on a Discord card, and it belongs in this module for the same reason the
    fields do: the promise is "the server sees the question, one person sees who
    asked it", so every function that reveals the asker lives here, next to the
    one that must not — rather than in a route where it is a line somebody could
    add to the wrong side later.

    Nothing here is rendered publicly, and the closed field list on
    :func:`relay_payload` is what makes that checkable: an id in this dict
    travels to an operator's browser and nowhere else.

    ``asked_at`` travels beside ``asked`` because the console sorts by the
    moment rather than by the sentence about it, and parsing ``22 Sep 2026,
    03:33 IST`` back into one would be an accident waiting to happen.
    """
    record = record or {}
    try:
        question_id = int(record.get("id") or 0)
    except (TypeError, ValueError):
        question_id = 0
    return {
        "id": question_id,
        "handle": handle(record),
        "question": question_text(record),
        # The two fields the public relay can never carry, and the whole reason
        # this reading is owner-only.
        "asker": str(record.get("user_name") or "unknown"),
        "asker_id": str(record.get("user_id") or 0),
        "server": str(record.get("guild_name") or "unknown"),
        "asked": ist_stamp(record.get("asked_at")),
        "asked_at": float(record.get("asked_at") or 0.0),
        # Whether the channel ever got it. A question nobody answered *and* the
        # channel never saw is a different thing from one that is merely waiting,
        # and the console is where that difference is visible.
        "relayed": bool(record.get("relayed")),
        "relay_detail": str(record.get("relay_detail") or ""),
    }


def owner_inbox_sync(*, limit: int = 20) -> dict:
    """The same reading from a Flask thread, which has no event loop.

    The console's reads are synchronous (``db.connect``) and must stay that way:
    a page that could only be drawn while the bot's loop was running would show
    nothing during exactly the deploys and restarts an operator opens it to
    check on, and the questions are on disk either way.

    Two doors to one table, so the *reading* is shared rather than rewritten:
    the rows come from :func:`owner_row`, which is the only place the asker's
    name is put into a dict, and the counts are the same two numbers the
    asyncio path reports.
    """
    rows: list[dict] = []
    open_count = answered = 0
    try:
        conn = db.connect()
    except Exception as exc:  # noqa: BLE001 — an unreadable database is answered
        print(f"[QNA] Could not open the question box for reading: {exc}", flush=True)
        return {
            "rows": [], "open": 0, "answered": 0, "shown": 0,
            "configured": configured(), "setting": SETTING,
            "available": False,
            "because": "The database could not be opened, so this list is unknown rather than empty.",
        }
    try:
        # The table is created at boot *and* here, for the same reason the
        # asyncio path does it: a database restored from a backup taken before
        # this feature existed would otherwise make the page an error instead of
        # an empty list. `CREATE TABLE IF NOT EXISTS` is a no-op when it is there.
        conn.execute(CREATE_TABLE)
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE status = ? {_ORDER} LIMIT ?",
            (OPEN, max(1, min(int(limit or 20), 100))),
        )
        rows = [owner_row(row) for row in (_row(item) for item in cur.fetchall()) if row]
        cur.execute(f"SELECT status, COUNT(*) FROM {TABLE} GROUP BY status")
        for status, number in cur.fetchall():
            if str(status) == OPEN:
                open_count = int(number)
            elif str(status) == ANSWERED:
                answered = int(number)
    except Exception as exc:  # noqa: BLE001
        print(f"[QNA] Could not read the question box: {exc}", flush=True)
        return {
            "rows": [], "open": 0, "answered": 0, "shown": 0,
            "configured": configured(), "setting": SETTING,
            "available": False,
            "because": "The table could not be read, so this list is unknown rather than empty.",
        }
    finally:
        conn.close()
    return {
        "rows": rows,
        "open": open_count,
        "answered": answered,
        "shown": len(rows),
        "configured": configured(),
        "setting": SETTING,
        "available": True,
        "because": "",
    }


def answer_body(record, answer: str) -> str:
    """What the asker is DMed: the answer, and the question it answers.

    Both, because a DM that arrives days later is a message out of nowhere
    otherwise. The question is quoted as the bot received it — there is no name
    on either side of this exchange to leak.
    """
    return (
        f"{str(answer or '').strip()[:MAX_ANSWER]}\n\n"
        f"-# In answer to: {question_text(record)}"
    )


# --------------------------------------------------------------------------- #
# Posting it
# --------------------------------------------------------------------------- #
def webhook_url() -> str:
    """The configured relay, or ``""`` when none is set."""
    return config_audit.get("QNA_WEBHOOK_URL")


def configured() -> bool:
    """Whether a question can be relayed at all."""
    return bool(webhook_url())


def post(url: str, payload: dict, *, timeout: float = TIMEOUT) -> tuple[bool, str]:
    """Hand one payload to the webhook, and say whether Discord took it.

    Blocking on purpose, and the caller runs it on a thread: unlike the
    out-of-band alerts — which must never be able to fail anything — here some-
    body is waiting to be told their question was posted, so this returns the
    answer instead of swallowing it. Nothing about the payload is logged: the
    question belongs to the channel it was posted in, not to the host's log.
    """
    if not url:
        return False, f"{SETTING} is not set, so there is nowhere to post this"
    try:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "MeowBot/1.0 (+https://meow-bot.pages.dev)",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
    except Exception as exc:  # noqa: BLE001 — a refusal is an answer, not a crash
        return False, f"the webhook did not accept it ({type(exc).__name__})"
    return True, ""
