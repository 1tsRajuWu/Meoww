"""The support bridge: what belongs in the conversation, and what it refuses.

The feature is one sentence — *a message written on the web is posted into one
Discord channel, and what people write back there comes home to the page* — and
each test below is one of the ways that sentence can quietly stop being true:

* **the bridge answering itself.** The listener takes in messages written by
  people and nothing else; the bot's own posts are the web side of the same
  conversation and are written down once, when they are sent. A rule that let a
  bot's message through would double every message the operator sends, and one
  that let a webhook through would let anybody holding the webhook post as the
  conversation;
* **the same message arriving twice.** The listener and an on-demand pull can
  both run over one message, so the unique index over Discord's id is the thing
  that keeps two lines from being one message — and the ids of ``0`` (a send that
  failed) are exempt on purpose, because they are not the same message;
* **a message reported as sent that nobody received.** The row is written after
  Discord accepted the message, never before, and a refusal is answered as one;
* **a blank line that reads as somebody having said something.** A message whose
  content this deployment cannot read (no Message Content intent) and one that is
  only an attachment both arrive empty, and the reading says which it was.

The address itself is a deployment setting with a compiled default, so the tests
set it rather than depending on whichever channel this checkout names — a rule
this project keeps for every address that is a fact about one deployment.

Everything here runs against a throwaway database, and the Discord objects are
stand-ins: the listener and the two routes are the only parts that hold a real
channel.
"""
import asyncio
import os
import unittest
import warnings
from types import SimpleNamespace
from unittest import mock

import keep_alive

from cogs import support as support_cog
from tests.support import TempDatabaseMixin
from tests.test_composer import _Channel, GUILD_ID
from tests.test_dashboard import _DashboardFixture, CSRF
from utils import db, support, version

ROOM = 1553178180448292924
OTHER_ROOM = 515151515151515151
GUILD = 777
THREAD = 989898989898989898


class _Person:
    """A message's author: an id, a name, and whether it is a bot at all."""

    def __init__(self, uid, name="Ash", bot=False):
        self.id = uid
        self.name = name
        self.display_name = name
        self.bot = bot
        self.mention = f"<@{uid}>"


class _OpenedThread:
    """A thread the bot opened: an id, a name, and what it was made to say.

    Deliberately not a channel: the two things a surface reads off a thread are
    its identity (so a reply can be answered *into* it) and the fact that it was
    told something, which is what ``sent`` is for.
    """

    def __init__(self, tid=THREAD, name="🧵"):
        self.id = tid
        self.name = name
        self.parent_id = ROOM
        self.mention = f"<#{tid}>"
        self.sent = []

    async def send(self, content, **_kwargs):
        self.sent.append(content)
        return SimpleNamespace(id=7000 + len(self.sent), jump_url="", created_at=None)


class _StartedIt:
    """The message a conversation started on — something a thread can be opened on.

    ``create_thread`` is Discord's own method and the only thing the bridge asks
    of this object, which is why the stand-in is one method and a record of what
    it was called with. ``refusal`` makes it raise, the way a channel where the
    bot lacks Create Public Threads does.
    """

    def __init__(self, *, thread=None, refusal=None):
        self.thread = thread or _OpenedThread()
        self.refusal = refusal
        self.opened = []

    async def create_thread(self, name=None, auto_archive_duration=None):
        if self.refusal is not None:
            raise self.refusal
        self.opened.append((name, auto_archive_duration))
        if name:
            self.thread.name = name
        return self.thread


class _Message:
    """One Discord message, carrying only what the bridge reads off one."""

    def __init__(
        self,
        author,
        channel,
        content="hello",
        *,
        message_id=1001,
        attachments=(),
        webhook_id=None,
        created_at=None,
        started=None,
    ):
        self.author = author
        self.channel = channel
        self.content = content
        self.id = message_id
        self.attachments = list(attachments)
        self.webhook_id = webhook_id
        self.jump_url = f"https://discord.com/channels/1/{getattr(channel, 'id', 0)}/{message_id}"
        self.created_at = created_at or version.ist_now(1_700_000_000)
        # What a thread would be opened *on*: the listener calls this on the
        # message that started the conversation, so a stand-in without one is a
        # stand-in the thread path cannot be driven with.
        self.started = started

    async def create_thread(self, name=None, auto_archive_duration=None):
        if self.started is None:
            raise RuntimeError("this stand-in was given no thread to open")
        return await self.started.create_thread(name=name, auto_archive_duration=auto_archive_duration)

    @classmethod
    def person(cls, channel, content="hello", **kwargs):
        return cls(_Person(501), channel, content, **kwargs)

    @classmethod
    def from_bot(cls, channel, content="posted", **kwargs):
        return cls(_Person(999, "Meow", bot=True), channel, content, **kwargs)


class _Room:
    """The support channel, or a thread on it, as ``in_channel`` reads one."""

    def __init__(self, cid, name="support", parent_id=None):
        self.id = cid
        self.name = name
        self.parent_id = parent_id
        self.mention = f"<#{cid}>"


class _HistoryChannel(_Channel):
    """A text channel that also answers the two permissions the bridge reads.

    ``history`` is the pull's own door: an async iterator over whatever the test
    put in it, oldest first or newest — the bridge sorts by Discord's timestamp,
    so the order a channel hands them over in is not something it trusts.
    """

    def __init__(self, cid, name="support", *, history=(), can_post=True, can_read=True):
        super().__init__(cid, name, can_send=can_post)
        self._history = list(history)
        self._can_read = can_read
        self.guild = SimpleNamespace(me=SimpleNamespace(id=999))
        # The message a reply from the web is answered *on*, when the conversation
        # has no thread yet: ``get_partial_message`` is Discord's own lookup, and
        # a channel that answers ``None`` is one whose history the bot can no
        # longer reach — a distinct failure with its own sentence.
        self.parent = None

    def get_partial_message(self, _message_id):
        return self.parent

    def permissions_for(self, member):
        return SimpleNamespace(
            view_channel=True,
            send_messages=self._can_send,
            read_message_history=self._can_read,
        )

    async def history(self, limit=50):  # noqa: A003 — Discord's own name for it
        for message in self._history:
            yield message


def _here(coro, timeout=20.0):
    """Run one route's coroutine in this thread, as the harness does.

    Production's ``keep_alive._on_bot_loop`` schedules it on the gateway's loop
    and refuses to touch Discord when there is none — the right answer for a bot
    that is still booting, and pinned below. The fixture's stand-in gateway has
    no loop at all, which is the case ``keep_alive._run_on_bot_loop`` documents a
    fallback for ("the stand-in harness's documented case"), so the routes under
    test get their work done here instead of being left untested behind a
    refusal that has its own tests.
    """
    result = asyncio.run(coro)
    return result if isinstance(result, tuple) else (result, 200)


def _with_room(channel_id):
    """Run one body with ``SUPPORT_CHANNEL_ID`` set, whatever the host has set."""

    class _Shell:
        def __enter__(self):
            self._patch = mock.patch.dict(os.environ, {"SUPPORT_CHANNEL_ID": str(channel_id)})
            return self._patch.start()

        def __exit__(self, *_exc):
            self._patch.stop()
            return False

    return _Shell()


class TheAddressIsASetting(unittest.TestCase):
    def test_blank_falls_back_on_the_compiled_support_channel(self):
        with mock.patch.dict(os.environ, {"SUPPORT_CHANNEL_ID": ""}):
            self.assertEqual(support.channel_id(), 1553178180448292924)
        self.assertTrue(support.on())

    def test_the_variable_wins(self):
        with _with_room(ROOM):
            self.assertEqual(support.channel_id(), ROOM)

    def test_a_typo_falls_back_rather_than_raising(self):
        """This is read on a message path: a bad variable must not error a listener."""
        with mock.patch.dict(os.environ, {"SUPPORT_CHANNEL_ID": "not-a-number"}):
            self.assertEqual(support.channel_id(), 1553178180448292924)

    def test_zero_is_the_off_switch_and_says_so(self):
        with _with_room(0):
            self.assertFalse(support.on())
            self.assertFalse(support.is_reply(_Message.person(_Room(ROOM))))
            status = support.status(find=lambda _cid: _Room(ROOM))
        self.assertFalse(status["on"])
        self.assertIn("SUPPORT_CHANNEL_ID", status["why"])
        self.assertIn("0", status["why"])


class TheMessageTheBridgePosts(unittest.TestCase):
    def test_the_body_comes_first_and_the_name_is_a_subtext_line(self):
        posted = support.compose("can you check the export?", "Raj")
        self.assertTrue(posted.startswith("can you check the export?"))
        self.assertIn("-# Raj", posted)
        self.assertIn("from the web", posted)

    def test_a_body_that_is_only_whitespace_is_empty(self):
        self.assertIn("hello", support.compose("  hello  ", "Raj"))

    def test_a_name_cannot_reshape_the_line_it_is_printed_on(self):
        posted = support.compose("hi", "**not bold** `code`\nsecond line")
        self.assertNotIn("*", posted.split("-# ", 1)[1])
        self.assertNotIn("\nsecond line", posted)

    def test_the_whole_message_fits_inside_discord_s_limit(self):
        """The cap is what keeps the attribution from being the part that is cut."""
        posted = support.compose("x" * 10000, "a" * 400)
        self.assertLess(len(posted), 2000)
        self.assertIn("-# ", posted)

    def test_a_missing_name_still_says_who_wrote_it(self):
        self.assertIn("the operator", support.compose("hi", ""))


class WhatBelongsToTheConversation(unittest.TestCase):
    def setUp(self):
        self._room = _with_room(ROOM)
        self._room.__enter__()

    def tearDown(self):
        self._room.__exit__(None, None, None)

    def test_a_person_in_the_channel_is_part_of_it(self):
        self.assertTrue(support.is_reply(_Message.person(_Room(ROOM))))

    def test_the_bot_itself_is_not(self):
        """Its own posts are the web side, written down when they are sent."""
        self.assertFalse(support.is_reply(_Message.from_bot(_Room(ROOM))))

    def test_another_bot_is_not_either(self):
        self.assertFalse(support.is_reply(_Message(_Person(7, "helper", bot=True), _Room(ROOM))))

    def test_a_webhook_is_not(self):
        """A webhook is a way to post into the channel without being in it."""
        message = _Message.person(_Room(ROOM), webhook_id=12345)
        self.assertFalse(support.is_reply(message))

    def test_another_channel_is_not(self):
        self.assertFalse(support.is_reply(_Message.person(_Room(OTHER_ROOM))))

    def test_a_thread_on_the_channel_is(self):
        """An answer written in a thread on the message is still an answer."""
        thread = _Room(THREAD, "about the export", parent_id=ROOM)
        self.assertTrue(support.is_reply(_Message.person(thread)))

    def test_a_thread_somewhere_else_is_not(self):
        thread = _Room(THREAD, "unrelated", parent_id=OTHER_ROOM)
        self.assertFalse(support.is_reply(_Message.person(thread)))

    def test_the_weird_room_shape_is_refused_rather_than_raising(self):
        for room in (None, SimpleNamespace(), SimpleNamespace(id="not-a-number")):
            with self.subTest(room=room):
                self.assertFalse(support.is_reply(_Message.person(room)))

    def test_a_thread_reply_remembers_which_thread(self):
        thread = _Room(THREAD, "about the export", parent_id=ROOM)
        self.assertEqual(support.facts(_Message.person(thread))["where_name"], "about the export")
        self.assertEqual(support.facts(_Message.person(_Room(ROOM)))["where_name"], "")

    def test_when_it_arrived_is_discords_own_clock(self):
        """A pulled message has to land where it belongs, not at the pull's hour."""
        message = _Message.person(_Room(ROOM), created_at=version.ist_now(1_600_000_000))
        self.assertEqual(support.facts(message)["ts"], 1_600_000_000)

    def test_an_empty_message_is_recorded_as_empty_with_its_attachments(self):
        """Not as a lie and not as a failure: the reading says which it was."""
        message = _Message.person(_Room(ROOM), "", attachments=[object(), object()])
        found = support.facts(message)
        self.assertEqual(found["body"], "")
        self.assertEqual(found["attachments"], 2)
        shown = support.reading({**found, "id": 1, "side": support.DISCORD})
        self.assertIn("attachment", shown["body"])
        self.assertEqual(shown["attachments"], 2)

    def test_a_plain_empty_message_says_it_was_empty(self):
        shown = support.reading({
            "id": 1, "side": support.DISCORD, "body": "", "attachments": 0, "ts": 0,
        })
        self.assertIn("empty", shown["body"])

    def test_a_recorded_row_is_stamped_on_the_operators_clock(self):
        row = {
            "id": 1, "side": support.WEB, "author_name": "Raj", "body": "hi",
            "attachments": 0, "ts": 1_700_000_000, "message_id": 5, "jump": "",
            "where_name": "", "author_id": 1,
        }
        shown = support.reading(row)
        self.assertEqual(shown["when"], version.stamp(1_700_000_000))
        self.assertTrue(shown["from_web"])


class TheRecord(TempDatabaseMixin, unittest.TestCase):
    """What is written down, and what is written down exactly once."""

    def setUp(self):
        self.use_temp_database("support.db")
        asyncio.run(db.setup())
        self.room = _Room(ROOM)
        self._room = _with_room(ROOM)
        self._room.__enter__()

    def tearDown(self):
        self._room.__exit__(None, None, None)
        self.release_temp_database()

    def rows(self):
        return asyncio.run(support.recent(50))

    def test_a_reply_is_written_down_with_its_author_and_text(self):
        message = _Message.person(self.room, "the export fails")
        row = asyncio.run(support.record_inbound(message))
        self.assertEqual(row["side"], support.DISCORD)
        self.assertEqual(row["author_name"], "Ash")
        self.assertEqual(row["body"], "the export fails")
        self.assertEqual(row["message_id"], 1001)
        self.assertEqual(len(self.rows()), 1)

    def test_the_same_message_twice_is_one_line(self):
        """The listener and a pull can both be handed it."""
        message = _Message.person(self.room, "twice?")
        first = asyncio.run(support.record_inbound(message))
        second = asyncio.run(support.record_inbound(message))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.rows()), 1)

    def test_a_message_with_no_id_is_never_treated_as_a_duplicate(self):
        """``0`` means unknown, and two unknowns are not the same message."""
        blank = _Message.person(self.room, "first", message_id=0)
        other = _Message.person(self.room, "second", message_id=0)
        asyncio.run(support.record_inbound(blank))
        asyncio.run(support.record_inbound(other))
        self.assertEqual(len(self.rows()), 2)

    def test_a_post_from_the_web_is_written_down_with_discords_id(self):
        message = SimpleNamespace(id=4242, jump_url="https://discord.com/channels/1/2/4242")
        row = asyncio.run(
            support.record_outbound(
                body="looking at it", author_id=7, author_name="Raj", message=message
            )
        )
        self.assertEqual(row["side"], support.WEB)
        self.assertEqual(row["message_id"], 4242)
        self.assertEqual(row["jump"], message.jump_url)

    def test_the_transcript_is_oldest_first_and_carries_its_own_total(self):
        asyncio.run(support.record_inbound(_Message.person(self.room, "first", message_id=1,
                                                            created_at=version.ist_now(1_700_000_000))))
        asyncio.run(support.record_inbound(_Message.person(self.room, "second", message_id=2,
                                                            created_at=version.ist_now(1_700_000_500))))
        asyncio.run(support.record_inbound(_Message.person(self.room, "middle", message_id=3,
                                                            created_at=version.ist_now(1_700_000_200))))
        reading = support.transcript_sync(2)
        self.assertTrue(reading["read"])
        self.assertEqual([row["body"] for row in reading["rows"]], ["middle", "second"])
        self.assertEqual(reading["total"], 3)
        self.assertEqual(reading["shown"], 2)

    def test_a_pull_takes_in_the_history_and_skips_what_the_bot_said(self):
        kept = _Message.person(self.room, "older question", message_id=11,
                               created_at=version.ist_now(1_700_000_000))
        theirs = _Message.from_bot(self.room, "posted by the bridge", message_id=12,
                                   created_at=version.ist_now(1_700_000_100))
        channel = _HistoryChannel(ROOM, history=[kept, theirs])
        report = asyncio.run(support.pull(channel))
        self.assertTrue(report["ok"])
        self.assertEqual((report["seen"], report["taken"]), (2, 1))
        self.assertEqual([row["body"] for row in self.rows()], ["older question"])

    def test_a_pull_run_twice_adds_nothing_twice(self):
        channel = _HistoryChannel(ROOM, history=[_Message.person(self.room, "once", message_id=21)])
        asyncio.run(support.pull(channel))
        again = asyncio.run(support.pull(channel))
        self.assertEqual(again["taken"], 0)
        self.assertEqual(len(self.rows()), 1)

    def test_a_pull_that_cannot_read_the_history_says_so_rather_than_raising(self):
        class _Refusing(_HistoryChannel):
            def history(self, limit=50):  # noqa: A003 — Discord's own name
                raise RuntimeError("Forbidden")

        report = asyncio.run(support.pull(_Refusing(ROOM)))
        self.assertFalse(report["ok"])
        self.assertEqual(report["taken"], 0)
        self.assertIn("RuntimeError", report["detail"])
        self.assertEqual(self.rows(), [])

    def test_an_unreadable_database_reading_says_unknown_rather_than_empty(self):
        """A page that cannot tell those apart reports an empty conversation."""
        with mock.patch.object(db, "connect", side_effect=RuntimeError("no database")):
            reading = support.transcript_sync()
        self.assertFalse(reading["read"])
        self.assertEqual(reading["rows"], [])
        self.assertTrue(reading["why"])
        self.assertEqual(reading["total"], 0)


class TheStatus(unittest.TestCase):
    def test_a_channel_the_bot_can_see_reports_where_it_is_and_what_it_may_do(self):
        channel = _HistoryChannel(ROOM)
        status = support.status(find=lambda _cid: channel)
        self.assertTrue(status["on"])
        self.assertTrue(status["channel"]["resolved"])
        self.assertEqual(status["channel"]["name"], "support")
        self.assertTrue(status["postable"])
        self.assertTrue(status["readable"])
        self.assertTrue(status["resolvable"])

    def test_a_bridge_the_bot_cannot_speak_in_still_reads_as_configured(self):
        """That distinction is the whole point: a quiet channel looks the same."""
        channel = _HistoryChannel(ROOM, can_post=False)
        status = support.status(find=lambda _cid: channel)
        self.assertTrue(status["on"])
        self.assertFalse(status["postable"])

    def test_a_channel_nothing_answers_to_is_not_resolved(self):
        status = support.status(find=lambda _cid: None)
        self.assertTrue(status["on"])
        self.assertFalse(status["channel"]["resolved"])
        self.assertIsNone(status["postable"])

    def test_nothing_looked_it_up_and_that_is_said(self):
        status = support.status()
        self.assertFalse(status["resolvable"])
        self.assertFalse(status["channel"]["resolved"])


class TheConversationHasAHome(TempDatabaseMixin, unittest.TestCase):
    """A support conversation is one person's problem, and it lives in a thread.

    The room is where somebody walks in; the thread is where the back-and-forth
    happens. Everything here is about the id that makes that true — which is the
    only thing a surface needs to answer *into* a conversation rather than
    shouting into a channel and linking the reader out to Discord.
    """

    def setUp(self):
        self.use_temp_database("support-threads.db")
        asyncio.run(db.setup())
        self._room = _with_room(ROOM)
        self._room.__enter__()

    def tearDown(self):
        self._room.__exit__(None, None, None)
        self.release_temp_database()

    def test_the_thread_is_named_for_the_person_and_what_they_said(self):
        named = support.thread_name("the export fails on a big server", "Ash")
        self.assertTrue(named.startswith(support.THREAD_MARK))
        self.assertIn("Ash", named)
        self.assertIn("the export fails", named)

    def test_a_name_that_would_reshape_the_thread_is_flattened(self):
        """The name comes from a person, and a thread line is still a line."""
        named = support.thread_name("**bold** `code`\nsecond line", "A*sh`")
        self.assertNotIn("*", named)
        self.assertNotIn("`", named)
        self.assertNotIn("\n", named)

    def test_the_name_can_never_be_why_discord_refuses_the_thread(self):
        named = support.thread_name("x" * 4000, "y" * 4000)
        self.assertLessEqual(len(named), support.MAX_THREAD_NAME)

    def test_the_message_that_started_it_has_no_thread_yet(self):
        """Which is what "open one on this message" is read from."""
        found = support.facts(_Message.person(_Room(ROOM), "the export fails"))
        self.assertEqual(support.thread_of(_Message.person(_Room(ROOM))), 0)
        self.assertEqual(support.thread_target([{**found, "id": 1}]), 0)

    def test_a_reply_written_inside_a_thread_remembers_it(self):
        thread = _Room(THREAD, "about the export", parent_id=ROOM)
        found = support.facts(_Message.person(thread, "any update?"))
        self.assertEqual(found["thread_id"], THREAD)
        self.assertEqual(found["where_name"], "about the export")

    def test_a_row_carries_the_thread_it_is_answered_in(self):
        self.assertEqual(support.thread_target([{"id": 1, "thread_id": THREAD}]), THREAD)

    def test_a_named_room_with_no_id_is_not_guessed_at(self):
        """A row from before the column existed names a thread and cannot point
        at one. Answering on a guess would answer in the wrong conversation."""
        old = {"id": 1, "thread_id": 0, "where_name": "about the export"}
        self.assertEqual(support.thread_target([old]), 0)

    def test_attaching_the_thread_writes_it_on_the_row(self):
        row = asyncio.run(support.record_inbound(_Message.person(_Room(ROOM), "start")))
        self.assertTrue(asyncio.run(support.attach_thread(row["id"], THREAD)))
        self.assertEqual(asyncio.run(support.get(row["id"]))["thread_id"], THREAD)

    def test_attaching_a_thread_to_a_row_that_is_gone_changes_nothing(self):
        self.assertFalse(asyncio.run(support.attach_thread(999, THREAD)))
        self.assertFalse(asyncio.run(support.attach_thread(0, THREAD)))
        self.assertFalse(asyncio.run(support.attach_thread(row_id=1, thread_id=0)))

    def test_the_reading_hands_a_surface_the_handle_it_replies_to(self):
        row = asyncio.run(support.record_inbound(_Message.person(_Room(ROOM), "start")))
        shown = support.transcript_sync(5)["rows"][0]
        self.assertEqual(shown["reply_to"], row["id"])
        self.assertEqual(shown["thread_id"], 0)


class TheListener(TempDatabaseMixin, unittest.TestCase):
    """The cog is one call into the module, so this pins the wiring."""

    def setUp(self):
        self.use_temp_database("support-cog.db")
        asyncio.run(db.setup())
        self.cog = support_cog.Support(bot=SimpleNamespace())
        self._room = _with_room(ROOM)
        self._room.__enter__()

    def tearDown(self):
        self._room.__exit__(None, None, None)
        self.release_temp_database()

    def test_a_reply_lands_in_the_record(self):
        message = _Message.person(_Room(ROOM), "any update?")
        asyncio.run(self.cog.on_message(message))
        rows = asyncio.run(support.recent(10))
        self.assertEqual([row["body"] for row in rows], ["any update?"])

    def test_a_message_anywhere_else_is_left_alone(self):
        asyncio.run(self.cog.on_message(_Message.person(_Room(OTHER_ROOM), "not here")))
        self.assertEqual(asyncio.run(support.recent(10)), [])

    def test_the_bot_opens_a_thread_and_says_where_it_continues(self):
        """What makes the bridge a conversation rather than a wall of replies."""
        started = _StartedIt()
        message = _Message.person(
            _Room(ROOM), "the export fails on a big server", message_id=41, started=started
        )
        asyncio.run(self.cog.on_message(message))
        self.assertEqual(len(started.opened), 1)
        name, archive = started.opened[0]
        self.assertIn("Ash", name)
        self.assertEqual(archive, 1440)
        # It says where it is, in the thread, so an empty thread under somebody's
        # own message does not read as the bot having made a mistake.
        self.assertEqual(started.thread.sent, [support.CONTINUE_HERE])
        rows = asyncio.run(support.recent(10))
        self.assertEqual(rows[0]["thread_id"], started.thread.id)

    def test_a_thread_it_could_not_open_is_survived_and_the_reply_is_written_down(self):
        """A bridge that could not make a thread is still a bridge: taking the
        listener down would lose the reply as well as the thread."""
        started = _StartedIt(refusal=RuntimeError("Missing Permissions"))
        message = _Message.person(_Room(ROOM), "still recorded", message_id=42, started=started)
        asyncio.run(self.cog.on_message(message))
        rows = asyncio.run(support.recent(10))
        self.assertEqual([row["body"] for row in rows], ["still recorded"])
        self.assertEqual(rows[0]["thread_id"], 0)

    def test_a_reply_already_in_a_thread_does_not_get_another_one(self):
        started = _StartedIt()
        thread = _Room(THREAD, "about the export", parent_id=ROOM)
        asyncio.run(
            self.cog.on_message(_Message.person(thread, "any update?", message_id=43,
                                               started=started))
        )
        self.assertEqual(started.opened, [])
        rows = asyncio.run(support.recent(10))
        self.assertEqual(rows[0]["thread_id"], THREAD)


class TheDoor(_DashboardFixture):
    """The two routes, driven the way the page and a script drive them."""

    def setUp(self):
        super().setUp()
        self.room = _HistoryChannel(int(support.channel_id()))
        self.other_room = _Room(OTHER_ROOM)
        self.thread = _OpenedThread()
        self.bot.get_channel = lambda cid: (
            self.room if int(cid) == int(self.room.id) else None
        )
        self._runner = mock.patch.object(keep_alive, "_on_bot_loop", _here)
        self._runner.start()

    def tearDown(self):
        self._runner.stop()
        super().tearDown()

    def post(self, body=None, **extra):
        payload = {"body": body} if body is not None else {}
        payload.update(extra)
        return self.api("POST", "/api/support", payload)

    def test_the_operator_reads_the_conversation_and_nobody_else_does(self):
        self.sign_in(scope="global")
        payload = self.api("GET", "/api/support").get_json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["status"]["channel"]["resolved"])
        self.assertEqual(payload["transcript"]["rows"], [])

        self.sign_in()
        denied = self.api("GET", "/api/support")
        self.assertEqual(denied.status_code, 403)
        self.assertIn("Operator", denied.get_json()["error"])

    def test_a_message_typed_here_is_posted_and_written_down(self):
        self.sign_in(scope="global")
        response = self.post("the export fails on a big server")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(len(self.room.sent), 1)
        content = self.room.sent[0]["content"]
        self.assertIn("the export fails on a big server", content)
        self.assertIn("from the web", content)
        rows = asyncio.run(support.recent(10))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["side"], support.WEB)

    def test_a_send_the_bot_cannot_make_is_a_refusal_with_nothing_recorded(self):
        self.sign_in(scope="global")
        self.room.send_error = RuntimeError("Forbidden")
        response = self.post("hello")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["success"])
        self.assertEqual(asyncio.run(support.recent(10)), [])

    def test_an_empty_message_is_refused_before_anything_is_posted(self):
        self.sign_in(scope="global")
        for payload in ("", "   ", None):
            with self.subTest(body=payload):
                response = self.post(payload)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.room.sent, [])

    def test_a_bot_with_no_running_loop_is_refused_and_posts_nothing(self):
        """The real runner, not the stand-in: this is what a booting bot answers."""
        self.sign_in(scope="global")
        self._runner.stop()
        try:
            with warnings.catch_warnings():
                # ``_on_bot_loop`` decides *after* its caller has built the
                # coroutine, so a bot with no loop leaves one un-awaited — the
                # same warning every dashboard write makes while a bot is still
                # booting. It is the runner's shape, not this route's, and the
                # assertion that matters is that nothing was posted.
                warnings.simplefilter("ignore", RuntimeWarning)
                response = self.post("anybody there?")
        finally:
            self._runner.start()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["success"])
        self.assertIn("not connected", response.get_json()["error"])
        self.assertEqual(self.room.sent, [])
        self.assertEqual(asyncio.run(support.recent(10)), [])

    def test_a_channel_nothing_can_reach_is_refused_with_a_sentence(self):
        self.sign_in(scope="global")
        self.bot.get_channel = lambda _cid: None
        response = self.post("anybody there?")
        self.assertEqual(response.status_code, 409)
        self.assertIn("could not be reached", response.get_json()["error"])
        self.assertEqual(asyncio.run(support.recent(10)), [])

    def test_a_form_post_lands_back_on_the_page_it_came_from(self):
        """The dashboard's own door: a person pressed a button, not a script."""
        self.sign_in(scope="global")
        response = self.client.post(
            "/api/support",
            data={
                "body": "hello from the form",
                "csrf_token": CSRF,
                "dash_return": "/dashboard?guild_id=777001",
                "dash_notice": "Sent.",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard?guild_id=777001")
        with self.client.session_transaction() as session:
            self.assertTrue(session.get("dash_notice"))
        self.assertEqual(len(self.room.sent), 1)

    def test_a_form_post_from_the_console_answers_with_the_console_s_own_notice(self):
        self.sign_in(scope="global")
        response = self.client.post(
            "/api/support",
            data={
                "body": "hello from the console",
                "csrf_token": CSRF,
                "dash_return": "/owner/support",
                "dash_notice": "Sent.",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/owner/support")
        with self.client.session_transaction() as session:
            self.assertTrue(session.get("owner_notice"))

    def test_a_refusal_on_a_form_post_is_answered_with_its_own_sentence(self):
        self.sign_in(scope="global")
        response = self.client.post(
            "/api/support",
            data={"body": "", "csrf_token": CSRF, "dash_return": "/dashboard"},
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertTrue(session.get("dash_error"))

    def test_pulling_the_history_takes_in_what_the_page_does_not_have(self):
        self.sign_in(scope="global")
        self.room._history = [
            _Message.person(self.other_room, "not this room", message_id=31),
            _Message.person(_Room(ROOM), "an older question", message_id=32),
        ]
        first = self.post(action="pull")
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertIn("1", first.get_json()["said"])
        rows = asyncio.run(support.recent(10))
        self.assertEqual([row["body"] for row in rows], ["an older question"])

        again = self.post(action="pull")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(asyncio.run(support.count()), 1)

    def test_the_page_draws_the_card_for_the_operator_and_not_for_an_administrator(self):
        self.sign_in(scope="global")
        page = self.client.get(f"/dashboard?guild_id={GUILD_ID}").get_data(as_text=True)
        self.assertIn("Support — #support", page)
        self.assertIn("Start a new message in the room", page)
        # And the card says **how** a conversation is held here rather than only
        # that it can be: the top form starts one and the thread is the
        # back-and-forth, which is the half that was missing. A page that only
        # offered a composer was a page that could speak and not answer.
        self.assertIn("bot opens a thread under it", page)
        self.assertIn("<strong>Reply</strong>", page)

        self.sign_in()  # a guild-scoped link: one server, and not this conversation
        page = self.client.get(f"/dashboard?guild_id={GUILD_ID}").get_data(as_text=True)
        self.assertNotIn("Start a new message in the room", page)

    def test_the_reader_is_behind_the_agreement(self):
        """It speaks as the bot in a channel, like the composer does."""
        self.assertTrue(keep_alive._agreement_stands_here("/api/support"))

    def test_an_unknown_action_is_refused(self):
        self.sign_in(scope="global")
        response = self.post(action="delete-everything")
        self.assertEqual(response.status_code, 400)

    # ── answering into the conversation ───────────────────────────────────
    def a_row(self, body="the export fails", *, message_id=31, thread_id=0):
        """One message in the room, written down the way the listener writes it."""
        row = asyncio.run(
            support.record_inbound(_Message.person(self.room, body, message_id=message_id))
        )
        if thread_id:
            asyncio.run(support.attach_thread(row["id"], thread_id))
        return asyncio.run(support.get(row["id"]))

    def test_a_row_on_the_card_offers_the_reply_that_goes_into_its_thread(self):
        """The card used to offer *Open it in Discord* and nothing else: a page
        that can speak and cannot answer, which is the complaint it was written
        for."""
        row = self.a_row("the export fails", thread_id=THREAD)
        self.sign_in(scope="global")
        page = self.client.get(f"/dashboard?guild_id={GUILD_ID}").get_data(as_text=True)
        self.assertIn("<summary>Reply in the thread</summary>", page)
        self.assertIn(f'name="reply_to" value="{row["id"]}"', page)
        self.assertIn("Your reply to Ash", page)

    def test_a_row_with_no_thread_yet_offers_a_plain_reply(self):
        """Which is the reply that *opens* the conversation's thread."""
        self.a_row("the export fails")
        self.sign_in(scope="global")
        page = self.client.get(f"/dashboard?guild_id={GUILD_ID}").get_data(as_text=True)
        self.assertIn("<summary>Reply</summary>", page)

    def test_replying_on_a_line_answers_into_that_conversation_s_thread(self):
        """Answers the person rather than the room — the whole of the feature."""
        row = self.a_row(thread_id=THREAD)
        self.bot.get_channel = lambda cid: self.thread if int(cid) == THREAD else self.room
        self.sign_in(scope="global")
        response = self.post("looking at it now", reply_to=row["id"])
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.room.sent, [])
        self.assertIn("looking at it now", self.thread.sent[0])
        self.assertEqual(asyncio.run(support.recent(5))[-1]["thread_id"], THREAD)
        self.assertIn("into the thread", response.get_json()["said"])

    def test_replying_where_there_is_no_thread_opens_one_on_the_message(self):
        """A conversation nobody has threaded yet gets one *here*, on the message
        that started it — otherwise the answer talks over the top of it."""
        row = self.a_row(message_id=77)
        started = _StartedIt()
        self.room.parent = started
        self.sign_in(scope="global")
        response = self.post("answering this one", reply_to=row["id"])
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(len(started.opened), 1)
        self.assertIn("Ash", started.opened[0][0])
        self.assertEqual(self.room.sent, [])
        self.assertIn("answering this one", started.thread.sent[0])
        self.assertEqual(asyncio.run(support.get(row["id"]))["thread_id"], started.thread.id)
        self.assertEqual(asyncio.run(support.recent(5))[-1]["thread_id"], started.thread.id)

    def test_a_new_message_still_goes_to_the_room_and_starts_a_conversation(self):
        """No thread from this side: the bot opens one when the member writes back,
        so a channel where nobody answers yet does not fill with empty threads."""
        started = _StartedIt()
        self.room.parent = started
        self.sign_in(scope="global")
        response = self.post("is anybody there?")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.room.sent), 1)
        self.assertEqual(started.opened, [])
        self.assertEqual(asyncio.run(support.recent(5))[-1]["thread_id"], 0)

    def test_a_thread_that_is_gone_is_a_sentence_and_nothing_is_posted(self):
        row = self.a_row(thread_id=THREAD)
        self.sign_in(scope="global")
        response = self.post("anyone?", reply_to=row["id"])
        self.assertEqual(response.status_code, 409)
        self.assertIn("gone", response.get_json()["error"])
        self.assertEqual(self.room.sent, [])
        self.assertEqual(len(asyncio.run(support.recent(5))), 1)

    def test_a_message_discord_no_longer_has_is_a_sentence(self):
        row = self.a_row(message_id=88)  # no ``parent``: the lookup answers nothing
        self.sign_in(scope="global")
        response = self.post("hello?", reply_to=row["id"])
        self.assertEqual(response.status_code, 409)
        self.assertIn("not one this bot can still answer", response.get_json()["error"])
        self.assertEqual(self.room.sent, [])

    def test_a_thread_that_could_not_be_opened_says_which_permission(self):
        row = self.a_row(message_id=89)
        self.room.parent = _StartedIt(refusal=RuntimeError("Missing Permissions"))
        self.sign_in(scope="global")
        response = self.post("hello?", reply_to=row["id"])
        self.assertEqual(response.status_code, 409)
        self.assertIn("Create Public Threads", response.get_json()["error"])
        self.assertEqual(self.room.sent, [])

    def test_replying_to_a_row_that_is_not_in_the_conversation_is_refused(self):
        self.sign_in(scope="global")
        response = self.post("hello?", reply_to=4242)
        self.assertEqual(response.status_code, 409)
        self.assertIn("not in the conversation", response.get_json()["error"])
        self.assertEqual(self.room.sent, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
