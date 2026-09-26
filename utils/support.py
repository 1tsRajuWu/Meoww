"""The support bridge: one Discord channel, held from the web.

The operator asked for one thing: **a card that posts into a channel and shows
the replies coming back**, so the whole conversation can be held from a browser
instead of from Discord. That is what this module is — and the interesting part
is not the sending, it is *what a conversation is when half of it happens
somewhere the bot does not control*.

Three decisions, each of them a way the feature breaks if it is made casually:

* **The transcript is the database's, not Discord's history.** Every message the
  bridge posts and every message it takes in is a row in
  ``support_messages``, so the card draws the same conversation after a restart,
  during an outage, and while the gateway is reconnecting. Discord is the room
  and this is the record; a card that read ``channel.history`` on every render
  would be a page that cannot open while the thing it is about is down — which
  is exactly when an operator opens it. A *pull* reads history on demand and
  writes down anything the record does not have (see :func:`pull`).
* **A message is written down once.** Discord's id is stored on the row and the
  table has a unique index over it, so the same message arriving twice — once
  from the listener and once from a pull, or a pull run twice in a minute —
  cannot become two lines in the conversation. Ids of ``0`` are exempt, because
  the two ways a row can lack one (a send that failed, an old row from before
  the id was kept) are not the same message and must not collide.
* **Only the two sides are in it.** The listener takes in messages written by
  people in that channel and nothing else: not the bot, not another bot, not a
  webhook. The bridge's own posts are written by the bot, so this is also what
  stops the conversation from answering itself — a message the web posted is
  recorded when it is sent, and is never recorded again by the listener.
* **A conversation has a home, and it is a thread.** The channel is the room
  people walk into; a support conversation is one person's problem, so the bot
  opens a **thread** on the message that started it and everything after that
  happens in there. That is what makes a back-and-forth possible rather than a
  wall: the member replies under their own message, the operator replies from the
  web into the same thread, and neither has to be in the channel at the right
  moment. The thread's id is kept on the row (``thread_id``), so a surface can
  answer *into* the conversation instead of linking the operator out to Discord.

**What the reader must know about the two limits Discord imposes.** Reading
message *content* in a guild channel needs the Message Content intent; with it
dropped, a reply arrives with an empty ``content`` and the row says so rather
than pretending the message was blank. And whether the bot can post, and whether
it may read history at all, are channel permissions — so :func:`status` reports
them separately from "the channel is configured", because a bridge pointed at a
channel the bot cannot speak in looks exactly like one nobody has written to.

The address is a deployment setting (``SUPPORT_CHANNEL_ID``) rather than a
constant: which channel an operator talks support in is a fact about *their*
deployment, and the compiled default is this bot's own support channel. ``0`` is
the off switch, the same word it is everywhere else in this project.

Nothing here talks to Discord beyond the objects it is handed, so the whole
decision is exercised with stand-ins in ``tests/test_support.py`` — the listener
and the two routes are the parts that hold a real channel.
"""
from __future__ import annotations

import time

from . import config_audit, db, version

#: The channel the bridge posts into and reads replies from. Read through a
#: name-holding constant rather than spelled at the point of reading: the config
#: audit's source walk looks for the *name* of a declared setting being read.
CHANNEL_ENV = "SUPPORT_CHANNEL_ID"

#: This bot's own support channel, and the one the feature was asked for: a
#: deployment that has configured nothing talks support where this bot already
#: reads it.
_DEFAULT_CHANNEL_ID = 1553178180448292924

#: Where the conversation lives. Created at boot *and* on first use, because a
#: database restored from a backup taken before this existed would otherwise
#: make the whole bridge fail rather than start empty.
TABLE = "support_messages"

#: The two sides of one conversation, as the rows record them. ``web`` is a
#: message an operator sent from the dashboard or the console; ``discord`` is a
#: message somebody wrote in the channel.
WEB = "web"
DISCORD = "discord"

#: Every column, in the order the queries below select them.
COLUMNS = (
    "id", "message_id", "side", "author_id", "author_name", "body",
    "attachments", "jump", "where_name", "thread_id", "ts",
)

#: Columns added after the table shipped, and the declaration each one needs.
#: Read by both the async boot path and the synchronous page reader, because a
#: restored backup can arrive at either of them first and a ``SELECT`` naming a
#: column the table does not have is an error on the page that shows the
#: conversation — the one page nobody can check by looking at Discord instead.
THREAD_COLUMNS = {"thread_id": "INTEGER NOT NULL DEFAULT 0"}

CREATE_TABLE = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    # Discord's own id of the message this row is about. 0 means "not known",
    # which is what a send that failed leaves and what an old row carries.
    "message_id INTEGER NOT NULL DEFAULT 0,"
    f"side TEXT NOT NULL DEFAULT '{DISCORD}',"
    "author_id INTEGER NOT NULL DEFAULT 0,"
    "author_name TEXT NOT NULL DEFAULT '',"
    "body TEXT NOT NULL DEFAULT '',"
    "attachments INTEGER NOT NULL DEFAULT 0,"
    "jump TEXT NOT NULL DEFAULT '',"
    # The thread a reply arrived in, when it arrived in one. Empty for the
    # channel itself, which is where most of it happens.
    "where_name TEXT NOT NULL DEFAULT '',"
    # The thread this conversation lives in: the thread opened on this message,
    # or the thread this reply was written in. ``0`` is the channel itself.
    "thread_id INTEGER NOT NULL DEFAULT 0,"
    # When the message was *written*, in seconds since the epoch — read from
    # Discord's own timestamp so a pulled message lands where it belongs in the
    # conversation rather than where the pull happened to run.
    "ts REAL NOT NULL DEFAULT 0"
    ")"
)

#: What the bot says inside a thread it has just opened. One line, because its job
#: is to make the thread non-empty and to tell a member where they are: an empty
#: thread under your own message reads as if the bot had made a mistake.
CONTINUE_HERE = (
    "-# Continue here — this thread is the conversation, and what you write in it "
    "reaches the operator's console."
)

#: Discord's own ceiling for a thread's name, and the marker this project puts in
#: front of one so a thread is recognisable as the bot's in a list of them.
THREAD_MARK = "🧵"
MAX_THREAD_NAME = 100

#: One message, one row — the reason the listener and a pull can both run over
#: the same message without the conversation growing a duplicate. Partial,
#: because the rows that do not know their message id are not the same message.
CREATE_INDEX = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {TABLE}_message "
    f"ON {TABLE}(message_id) WHERE message_id <> 0"
)

#: Long enough for a real support message; short enough that the attribution
#: below still fits under Discord's 2000-character limit with room to spare.
MAX_BODY = 1800

#: How much of a name is printed beside a message.
MAX_NAME = 80

#: How many messages a pull takes in, and how many the transcript shows by
#: default. The card is a conversation, not an archive: the record keeps
#: everything, the page shows the tail of it.
PULL_LIMIT = 50
SHOWN = 30


def thread_name(body: str, author: str) -> str:
    """What to call the thread opened on somebody's message.

    The person first, because a list of threads is scanned by *who* it is about,
    then as much of what they said as fits. Everything a name could do to the
    line around it is already ruled out by :func:`one_line`, and the result is
    capped at Discord's own limit so the name can never be the reason the thread
    is refused.
    """
    who = one_line(author, 40) or "somebody"
    said = one_line(body, MAX_THREAD_NAME - 50)
    named = f"{THREAD_MARK} {who} — {said}" if said else f"{THREAD_MARK} {who}"
    return named[:MAX_THREAD_NAME]


def thread_of(message) -> int:
    """The thread a message was written in, or ``0`` for the channel itself.

    Read from the channel the message arrived in: a thread is a channel whose id
    is not the support channel and whose ``parent_id`` is, which is the same pair
    :func:`in_channel` accepts. This is what makes a reply *already in* a thread
    record where it belongs without the listener having to remember anything.
    """
    room = _room(message)
    if room is None:
        return 0
    here = as_int(getattr(room, "id", 0))
    if not here or here == channel_id():
        return 0
    return here if in_channel(room, channel_id()) else 0


def thread_target(rows) -> int:
    """The thread one row of the conversation should be answered in.

    ``0`` means "there is no thread yet, make one on this message" — which is
    only ever true of the message that started the conversation, because a reply
    written inside a thread records the thread it arrived in.
    """
    for row in rows:
        if row is None:
            continue
        found = as_int(row.get("thread_id"))
        if found:
            return found
        if str(row.get("where_name") or ""):
            # A reply that arrived in a thread whose id predates this column:
            # the name is all the record has, and a named room with no id is not
            # a target. Answered as "no thread" rather than guessed at.
            return 0
    return 0


def _configured(setting: str, default: int) -> int:
    """One channel id from the environment, falling back on anything unusable.

    A malformed value falls back rather than raising, because this is read on a
    message path: a typo in a variable must not be the reason a listener errors
    on every message in the channel it was pointed at.
    """
    raw = config_audit.get(setting)
    if raw in (None, ""):
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def channel_id() -> int:
    """The channel the bridge holds. ``0`` switches it off."""
    return _configured(CHANNEL_ENV, _DEFAULT_CHANNEL_ID)


def on() -> bool:
    """Whether a channel is configured at all — the setting, not the permission."""
    return bool(channel_id())


def one_line(text, limit: int = MAX_NAME) -> str:
    """One line of text, collapsed and capped, safe to put in a message.

    Newlines become spaces and the markdown characters that could re-shape the
    line are dropped: this is used for a person's display name, which the
    operator does not control, and a name containing ``*`` must not be able to
    change how the message around it reads.
    """
    flat = " ".join(str(text or "").split())
    for mark in ("*", "_", "`", "~", "|", "\\"):
        flat = flat.replace(mark, "")
    return flat[:limit]


def compose(body: str, name: str) -> str:
    """What the bot posts for a message an operator sent from the web.

    The body first and the attribution in a subtext line, deliberately: the
    reply is what the room is reading, and the `-#` line is Discord's own
    smallest text, which is exactly what "who this is from" deserves. It also
    keeps a name from being able to reshape the body above it — everything a
    name could do is confined to the line it is printed on.
    """
    text = str(body or "").strip()[:MAX_BODY]
    who = one_line(name) or "the operator"
    return f"{text}\n\n-# {who} · from the web"


def _room(message):
    """The channel a message arrived in, or ``None``."""
    return getattr(message, "channel", None)


def as_int(value) -> int:
    """One id as a number, or ``0`` for anything that is not one.

    This runs on a *message path* — a listener here decides on every message the
    bot sees — so a value this project did not write has to come back as "not
    this channel" rather than raising inside an event handler. The first version
    of :func:`in_channel` cast the channel's id directly, and a stand-in with a
    non-numeric id took the listener down with a ``ValueError`` — which is the
    failure a listener can least afford, because it is the one nobody sees.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def in_channel(channel, channel_id_) -> bool:
    """Whether one channel *is* the support channel, or a thread on it.

    A thread is included on purpose: support staff who open a thread on the
    operator's message are answering it, and a bridge that dropped those replies
    would lose exactly the answers that were written most carefully.
    """
    if channel is None:
        return False
    wanted = as_int(channel_id_)
    if not wanted:
        return False
    if as_int(getattr(channel, "id", 0)) == wanted:
        return True
    return as_int(getattr(channel, "parent_id", None)) == wanted


def is_reply(message, *, channel: int | None = None) -> bool:
    """Whether this message belongs in the conversation.

    Deliberately strict, and every condition below is one that, dropped, makes
    the transcript wrong rather than merely noisy:

    * the bridge is configured at all — a channel id of ``0`` means this feature
      is off, not that everything in every channel is interesting;
    * the message is in the support channel (or a thread on it);
    * it was written by a person: not the bot, not another bot, not a webhook.
      The bot's own posts are the web side of this conversation — they are
      written down when they are sent, and letting the listener take them in
      again would double every message the operator sends.
    """
    if channel is None:
        if not on():
            return False
        channel = channel_id()
    if not channel:
        return False
    if not in_channel(_room(message), channel):
        return False
    if getattr(message, "webhook_id", None):
        return False
    author = getattr(message, "author", None)
    if author is None or getattr(author, "bot", False):
        return False
    return True


def _when(message) -> float:
    """When a message was written, as seconds since the epoch.

    Discord's own timestamp rather than the clock here: a pulled message is
    minutes or days old, and a row stamped with the moment the pull ran would
    sort to the end of the conversation it belongs in the middle of.
    """
    created = getattr(message, "created_at", None)
    if created is not None:
        try:
            return float(created.timestamp())
        except (AttributeError, TypeError, ValueError, OSError, OverflowError):
            pass
    return time.time()


def _where(message) -> str:
    """The thread a reply arrived in, or ``""`` for the channel itself.

    A reply in a thread is still part of the conversation — that is why
    :func:`is_reply` accepts it — and the card says which thread it came from,
    because "support answered in a thread on your message" and "support answered
    in the channel" are different places to go and read.
    """
    room = _room(message)
    if room is None or as_int(getattr(room, "id", 0)) == channel_id():
        return ""
    return str(getattr(room, "name", "") or "")


def facts(message) -> dict:
    """What one Discord message is, as the row that records it.

    Returned as data so the listener and a pull write the same thing — and so
    the *empty* message is honest: a message whose content this deployment
    cannot read (no Message Content intent) and one that carries only an
    attachment both arrive as empty text, and the count of attachments and the
    wording on the card are what tell the operator which of those it was.
    """
    author = getattr(message, "author", None)
    return {
        "message_id": as_int(getattr(message, "id", 0)),
        "author_id": as_int(getattr(author, "id", 0)),
        "author_name": one_line(
            getattr(author, "display_name", None) or getattr(author, "name", "")
        ) or "somebody",
        "body": str(getattr(message, "content", "") or "").strip()[:MAX_BODY],
        "attachments": len(getattr(message, "attachments", None) or []),
        "jump": str(getattr(message, "jump_url", "") or ""),
        "where_name": _where(message),
        "thread_id": thread_of(message),
        "ts": _when(message),
    }


def resolve(channel_id_: int | None = None, guild=None, find=None):
    """The support channel, looked up deployment-wide first.

    ``find`` is the client's own ``get_channel``, which answers for every server
    the bot is in. It is tried first because the support channel lives in the
    operator's own server, which is very often **not** the server a console page
    happens to be reading — resolving against that one made a bridge that was
    working perfectly read as *nothing the bot can see*. ``guild`` is the
    fallback for a caller that has only one server to look in.
    """
    wanted = as_int(channel_id_ if channel_id_ is not None else channel_id())
    if not wanted:
        return None
    if callable(find):
        try:
            found = find(wanted)
        except Exception:  # noqa: BLE001 — a lookup that cannot answer is not a verdict
            found = None
        if found is not None:
            return found
    lookup = getattr(guild, "get_channel", None) if guild is not None else None
    return lookup(wanted) if callable(lookup) else None


def address(channel_id_: int | None = None, guild=None, find=None) -> dict:
    """The support channel as a surface can print it.

    ``resolved`` is the difference between "this id is a channel this bot can
    see" and "this id is set and nothing answers to it" — the second is what a
    typo, or a channel that went with a server the bot has left, looks like. It
    is a flag rather than a sentence somebody has to parse out of a name.
    """
    wanted = as_int(channel_id_ if channel_id_ is not None else channel_id())
    row = {
        "id": wanted,
        "name": f"`{wanted}`",
        "mention": f"`{wanted}`",
        "resolved": False,
    }
    if not wanted:
        return row
    channel = resolve(wanted, guild, find)
    if channel is None:
        return row
    row["resolved"] = True
    row["name"] = str(getattr(channel, "name", "") or row["name"])
    row["mention"] = str(getattr(channel, "mention", "") or row["mention"])
    return row


def _permission(me, channel, name: str):
    """One channel permission, or ``None`` when Discord did not say."""
    if me is None or channel is None:
        return None
    try:
        perms = channel.permissions_for(me)
    except Exception:  # noqa: BLE001 — a channel that cannot answer is not a finding
        return None
    value = getattr(perms, name, None)
    return None if value is None else bool(value)


def status(guild=None, find=None) -> dict:
    """The bridge as an operator reads it: is it on, where, and what can it do.

    ``postable`` and ``readable`` are the two permissions that decide whether the
    bridge works, and they are reported apart from the address deliberately: a
    bridge pointed at a channel the bot may not speak in, or may not read history
    in, is configured correctly and does nothing — the failure that looks like a
    quiet channel.

    ``why`` is the sentence for an *off* bridge: "off" on its own is the least
    useful thing a console can say about something somebody believes is on.
    """
    configured = channel_id()
    why = ""
    if not configured:
        why = (
            f"{CHANNEL_ENV} is 0, which is this bridge's own off switch — blank is "
            "not, it falls back on the compiled support channel."
        )
    row = address(configured, guild, find)
    channel = resolve(configured, guild, find)
    # The bot's own member *in the support channel's server*: a channel's guild is
    # where its permission reading lives, and the console's server is not it.
    me = None
    if channel is not None:
        channel_guild = getattr(channel, "guild", None)
        me = getattr(channel_guild, "me", None) if channel_guild is not None else None
    if me is None and guild is not None:
        me = getattr(guild, "me", None)
    return {
        "on": on(),
        "why": why,
        "channel": row,
        "postable": _permission(me, channel, "send_messages"),
        "readable": _permission(me, channel, "read_message_history"),
        # Whether a conversation can be *held* here rather than announced: the
        # bot opens a thread on each new message, and without this permission it
        # cannot, which is a different failure from "the bridge is off" and has
        # its own fix.
        "threadable": _permission(me, channel, "create_public_threads"),
        # Whether *anything* could resolve the id: without this the row above is
        # a setting, and "an id nothing answers to" and "an id nobody looked up"
        # are different sentences on the card.
        "resolvable": bool(
            callable(find) or callable(getattr(guild, "get_channel", None))
        ),
        "setting": CHANNEL_ENV,
        # The two numbers a form and a button read: how long one message may be,
        # and how far back a pull reaches. Handed over rather than typed into a
        # template, so a page cannot offer a length the module would refuse.
        "max_body": MAX_BODY,
        "shown": SHOWN,
        "pull_limit": PULL_LIMIT,
    }


# --------------------------------------------------------------------------- #
# Writing the conversation down
# --------------------------------------------------------------------------- #
def _row(row) -> dict | None:
    """One database row as a dict with the types the rest of this module expects."""
    if row is None:
        return None
    record = dict(zip(COLUMNS, row))
    for key in ("id", "message_id", "author_id", "attachments", "thread_id"):
        try:
            record[key] = int(record.get(key) or 0)
        except (TypeError, ValueError):
            record[key] = 0
    try:
        record["ts"] = float(record.get("ts") or 0.0)
    except (TypeError, ValueError):
        record["ts"] = 0.0
    record["side"] = str(record.get("side") or DISCORD)
    return record


async def ensure_table(conn) -> None:
    """Create the table and its unique index, and add any column that is missing.

    The column pass is what makes this safe on a database restored from a backup
    taken before a column existed: ``CREATE TABLE IF NOT EXISTS`` never changes a
    table that is already there, so without it the conversation would read as
    "unknown" on the surfaces that SELECT the new name — and ``ALTER TABLE ...
    ADD COLUMN`` is additive, so running it on every call costs one ``PRAGMA``.
    """
    await conn.execute(CREATE_TABLE)
    await conn.execute(CREATE_INDEX)
    await db.ensure_columns(conn, TABLE, THREAD_COLUMNS)


def _ensure_columns_sync(conn) -> None:
    """The same column pass for the synchronous page reader.

    A page must not be the surface that fails on an un-migrated database: this
    runs before its own ``SELECT``, so the transcript is either the conversation
    or an honest "could not be read" rather than a column error.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()}
    for name, declaration in THREAD_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {declaration}")
            print(f"[SUPPORT] Added column {name} to {TABLE}.", flush=True)


async def _insert(*, side: str, found: dict) -> dict | None:
    """One row, or ``None`` when that message is already in the conversation.

    ``INSERT OR IGNORE`` against the unique index is the whole mechanism: the
    listener and a pull can both be handed the same message, and the second one
    is a no-op rather than a duplicate line. The id is handed back so a caller
    can say what actually happened rather than what it hoped happened.
    """
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        cursor = await conn.execute(
            f"INSERT OR IGNORE INTO {TABLE} "
            "(message_id, side, author_id, author_name, body, attachments, jump,"
            " where_name, thread_id, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                # Everything here is a value this module built, and the ids go
                # through ``as_int`` anyway: this write runs inside a listener on
                # a message path, and a single unparsable field must not be the
                # reason a reply is lost.
                as_int(found.get("message_id")),
                str(side),
                as_int(found.get("author_id")),
                str(found.get("author_name") or "")[:MAX_NAME],
                str(found.get("body") or "")[:MAX_BODY],
                as_int(found.get("attachments")),
                str(found.get("jump") or "")[:300],
                str(found.get("where_name") or "")[:100],
                as_int(found.get("thread_id")),
                float(found.get("ts") or time.time()),
            ),
        )
        changed = cursor.rowcount
        new_id = cursor.lastrowid
        await conn.commit()
    if not changed:
        return None
    return await get(new_id)


async def get(row_id: int) -> dict | None:
    """One row of the conversation by its own id, or ``None``."""
    try:
        wanted = int(row_id)
    except (TypeError, ValueError):
        return None
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        async with conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE id = ?", (wanted,)
        ) as cursor:
            row = await cursor.fetchone()
    return _row(row)


async def record_inbound(message) -> dict | None:
    """Write down one message somebody wrote in the channel. ``None`` if known."""
    return await _insert(side=DISCORD, found=facts(message))


async def attach_thread(row_id: int, thread_id: int) -> bool:
    """Point one row at the thread its conversation now lives in.

    Only ever the message that started it: the listener opens a thread the moment
    somebody writes in the channel, and the row written a line earlier is where
    every surface later looks to find the conversation. Returns whether a row was
    changed, so the listener can say what happened rather than assume it.
    """
    wanted, thread = as_int(row_id), as_int(thread_id)
    if not wanted or not thread:
        return False
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        cursor = await conn.execute(
            f"UPDATE {TABLE} SET thread_id = ? WHERE id = ?", (thread, wanted)
        )
        changed = cursor.rowcount
        await conn.commit()
    return bool(changed)


async def record_outbound(
    *, body: str, author_id: int = 0, author_name: str = "", message=None, thread=None
) -> dict:
    """Write down a message the bridge posted, with Discord's id when it has one.

    Only ever *after* the message was accepted by Discord: a row here is the
    bridge's claim that the room has this line, and a claim written before the
    send is a conversation with a message in it that nobody in the room can see.
    """
    # ``thread`` is the room the reply was posted into, when it was posted into
    # one: the reply is part of that conversation, and the card has to be able to
    # say which thread it went to (and to answer into it again).
    in_thread = as_int(getattr(thread, "id", 0))
    found = {
        "message_id": as_int(getattr(message, "id", 0)),
        "author_id": int(author_id or 0),
        "author_name": one_line(author_name) or "the operator",
        "body": str(body or "").strip()[:MAX_BODY],
        "attachments": 0,
        "jump": str(getattr(message, "jump_url", "") or ""),
        "where_name": one_line(getattr(thread, "name", "")) if in_thread else "",
        "thread_id": in_thread,
        "ts": _when(message) if message is not None else time.time(),
    }
    return await _insert(side=WEB, found=found) or {}


async def recent(limit: int = SHOWN) -> list[dict]:
    """The tail of the conversation, oldest first — the order it is read in."""
    try:
        wanted = max(1, min(int(limit or SHOWN), 200))
    except (TypeError, ValueError):
        wanted = SHOWN
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        async with conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} ORDER BY ts DESC, id DESC LIMIT ?",
            (wanted,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [row for row in (_row(item) for item in rows) if row is not None][::-1]


async def count() -> int:
    """How many messages the record holds, in total."""
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await ensure_table(conn)
        async with conn.execute(f"SELECT COUNT(*) FROM {TABLE}") as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def pull(channel, *, limit: int = PULL_LIMIT) -> dict:
    """Take the channel's recent history into the record, on demand.

    The listener only sees messages that arrive while the bot is running, so a
    bridge switched on after the conversation started would open on an empty
    card. This is the door for that: it reads Discord's own history and writes
    down whatever the record does not already have, oldest-landing-where-it-
    belongs because every row keeps Discord's timestamp.

    Never raises and always says what it did — a channel whose history cannot be
    read (no permission, an outage) is a sentence on the card, not a page that
    fails, and the count is what it actually took in rather than how many
    messages it looked at.
    """
    seen = taken = 0
    try:
        history = channel.history(limit=max(1, min(as_int(limit) or PULL_LIMIT, 200)))
    except Exception as exc:  # noqa: BLE001 — a refusal is an answer
        return {
            "ok": False,
            "seen": 0,
            "taken": 0,
            "detail": f"Discord did not hand the history over ({type(exc).__name__}).",
        }
    try:
        async for message in history:
            seen += 1
            if not is_reply(message, channel=as_int(getattr(channel, "id", 0))):
                continue
            if await record_inbound(message) is not None:
                taken += 1
    except Exception as exc:  # noqa: BLE001 — a partial pull is still a pull
        return {
            "ok": False,
            "seen": seen,
            "taken": taken,
            "detail": f"The history stopped part-way through ({type(exc).__name__}).",
        }
    return {"ok": True, "seen": seen, "taken": taken, "detail": ""}


# --------------------------------------------------------------------------- #
# Reading it back
# --------------------------------------------------------------------------- #
def reading(row) -> dict:
    """One row as a surface draws it.

    The stamp is made here rather than in a template for the reason
    ``utils/qna`` makes its own: four environments render these pages and only
    one of them has this app's filters, so a page that formatted an epoch itself
    would be a page that reads differently wherever it is previewed.
    """
    record = row or {}
    try:
        when = float(record.get("ts") or 0.0)
    except (TypeError, ValueError):
        when = 0.0
    body = str(record.get("body") or "").strip()
    attachments = as_int(record.get("attachments"))
    if not body:
        body = (
            "*(nothing but an attachment — or a message this deployment cannot read, "
            "which is what a dropped Message Content intent looks like)*"
            if attachments
            else "*(an empty message)*"
        )
    return {
        "id": int(record.get("id") or 0),
        "message_id": int(record.get("message_id") or 0),
        "side": str(record.get("side") or DISCORD),
        "from_web": str(record.get("side") or DISCORD) == WEB,
        "author": str(record.get("author_name") or "somebody"),
        "body": body,
        "attachments": attachments,
        "jump": str(record.get("jump") or ""),
        "where_name": str(record.get("where_name") or ""),
        # The conversation this line belongs to, and the handle a form replies
        # to: the row's own id, which the server resolves into "the thread this
        # is in, or a new one on the message that started it". Handing a form a
        # thread id it might not have yet would be a form that posts nowhere.
        "thread_id": int(record.get("thread_id") or 0),
        "reply_to": int(record.get("id") or 0),
        "at": when,
        "when": version.stamp(when) if when else "",
    }


def transcript_sync(limit: int = SHOWN) -> dict:
    """The conversation as a Flask thread reads it — synchronously, from disk.

    Synchronous for the reason ``utils/qna.owner_inbox_sync`` is: a page that
    could only be drawn while the bot's loop was running would show nothing
    during exactly the restarts an operator opens it to check on, and the
    conversation is on disk either way.

    ``read`` is not decoration. An unreadable database and an empty conversation
    both produce no rows, and the card must not describe the first as the second.
    """
    try:
        wanted = max(1, min(int(limit or SHOWN), 200))
    except (TypeError, ValueError):
        wanted = SHOWN
    rows: list[dict] = []
    total = 0
    try:
        conn = db.connect()
    except Exception as exc:  # noqa: BLE001 — an unreadable database is answered
        print(f"[SUPPORT] Could not open the conversation for reading: {exc}", flush=True)
        return {
            "read": False,
            "why": "The database could not be opened, so this transcript is unknown rather than empty.",
            "rows": [],
            "total": 0,
            "shown": 0,
        }
    try:
        conn.execute(CREATE_TABLE)
        conn.execute(CREATE_INDEX)
        _ensure_columns_sync(conn)
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} ORDER BY ts DESC, id DESC LIMIT ?",
            (wanted,),
        )
        rows = [
            reading(item) for item in (_row(row) for row in cur.fetchall()) if item is not None
        ]
        rows.reverse()
        cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
        total = int(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        print(f"[SUPPORT] Could not read the conversation: {exc}", flush=True)
        return {
            "read": False,
            "why": "The conversation table could not be read, so this transcript is unknown rather than empty.",
            "rows": [],
            "total": 0,
            "shown": 0,
        }
    finally:
        conn.close()
    return {"read": True, "why": "", "rows": rows, "total": total, "shown": len(rows)}


def card(*, limit: int = SHOWN, guild=None, find=None) -> dict:
    """Everything one card draws: the address, the permissions, and the tail.

    Assembled here rather than in each view because three surfaces render it
    (the dashboard, the console, and the preview harness), and a card built three
    times is three cards that start to differ — the failure this project keeps
    writing laws about. The view supplies the Discord lookups, which is the only
    part of this a pure module cannot do itself.
    """
    reading_ = transcript_sync(limit)
    return {
        "status": status(guild, find),
        "transcript": reading_,
        "url": "/api/support",
    }
