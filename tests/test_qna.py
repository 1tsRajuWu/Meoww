"""The anonymous question box: the promise, and the two readers of one row.

The feature is one sentence — *the server sees the question, one person sees who
asked it, the asker gets the answer in a DM* — so these tests are about the ways
that sentence can quietly stop being true:

* **the public message growing a name.** ``relay_payload`` is checked field by
  field, and then driven end to end through the real command with an asker whose
  name and id are unmistakable, failing if either appears anywhere in what the
  webhook is handed;
* **the private message losing one.** The owner's card and inbox are the *only*
  readers of the name, and both are checked to refuse a member who is not the
  owner — a DM card is only ever sent to the owner, so the refusal that matters
  is on the buttons and commands, which anyone can click;
* **an answer going to the wrong place.** The question is claimed before the DM
  is sent (two clicks must not DM twice) and put back when the DM fails (an
  answer that never arrived must not close the question);
* **a question going nowhere at all.** With no webhook configured, ``/qna ask``
  says which setting is missing instead of doing nothing, and writes nothing
  down — a question box with nowhere to post to is the exact state that reads as
  a broken bot.

Everything here runs against a throwaway database and the real callbacks; the
webhook is the one thing patched, because there is no network in this suite.
"""
import asyncio
import json
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from cogs import qna as qna_cog
from tests.support import TempDatabaseMixin
from utils import admin_limits, bot_channels, db, qna, style

GUILD_ID = 4242
GUILD_NAME = "Test Server"
ASKER_ID = 777001
ASKER_NAME = "RajTheAsker"
OWNER_ID = 777002
OWNER_NAME = "TheOperator"
WEBHOOK = "https://discord.com/api/webhooks/123456789/tok3n"


def layout_text(node) -> str:
    """Every readable string inside a Components V2 layout, flattened."""
    parts = []
    text = getattr(node, "content", None)
    if isinstance(text, str):
        parts.append(text)
    for child in getattr(node, "children", None) or []:
        parts.append(layout_text(child))
    return "\n".join(parts)


def card_text(view) -> str:
    """What a card says, however deeply it nests it."""
    return layout_text(view)


# --------------------------------------------------------------------------- #
# Stand-ins
# --------------------------------------------------------------------------- #
#: A role the deployment reads as a **Dev** one. The id is the module's own, so
#: this stands in for the real role without a copy of the number in a test — the
#: one place a copied id would be wrong is the place it matters.
DEV_ROLE = SimpleNamespace(id=bot_channels.DEV_ROLE_ID, name="Dev", is_default=lambda: False)


class _User:
    def __init__(self, uid: int, name: str):
        self.id = uid
        self.name = name
        self.display_name = name
        self.global_name = name
        self.mention = f"<@{uid}>"
        self.roles = []
        self.dms = []
        self.dm_error = None

    async def send(self, **kwargs):
        if self.dm_error is not None:
            raise self.dm_error
        self.dms.append(kwargs)
        return SimpleNamespace(id=900_000 + len(self.dms))


class _Response:
    def __init__(self):
        self.messages = []
        self.deferred = 0
        self.modals = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    async def defer(self, **kwargs):
        self.deferred += 1

    async def send_modal(self, modal):
        self.modals.append(modal)

    def is_done(self) -> bool:
        return bool(self.messages) or bool(self.deferred)


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _Interaction:
    """Only what the cog's callbacks actually read.

    ``roles`` is both halves of the Dev gate at once — the roles the *member*
    holds and the roles the *guild* has — because a gate that read one and not the
    other would pass a test that handed it the same list twice.
    """

    def __init__(self, *, user, guild=True, message_id=0, bot=None, module_on=True, roles=(), channels=()):
        held = list(roles)
        listed = list(channels)
        self.user = user
        user.roles = held
        self.guild = (
            SimpleNamespace(
                id=GUILD_ID,
                name=GUILD_NAME,
                roles=held,
                get_role=lambda rid: next((r for r in held if r.id == rid), None),
                # ``channels`` exists for the one question that is about *which*
                # server this is: a server is the bot's own either because
                # ``BOT_SERVER_GUILD_ID`` names it or because it holds the legacy
                # update channel, and only the second of those needs a lookup.
                get_channel=lambda cid: next(
                    (c for c in listed if getattr(c, "id", None) == cid), None
                ),
            )
            if guild
            else None
        )
        self.guild_id = GUILD_ID if guild else None
        self.message = SimpleNamespace(id=message_id)
        self.response = _Response()
        self.followup = _Followup()
        self.client = bot or _Bot(module_on=module_on)

    async def send(self, **kwargs):
        # ``style.send_panel`` posts to a *channel* when the target is not a real
        # ``discord.Interaction``, so a card sent through it lands here.
        self.response.messages.append(kwargs)

    @property
    def answered(self) -> list[dict]:
        return self.response.messages + self.followup.messages


class _Bot:
    """A stand-in client: user lookups, the module gate and nothing else."""

    def __init__(self, *, users=(), module_on=True, lookup_error=False):
        self.users = {user.id: user for user in users}
        self.views = []
        self.module_on = module_on
        self.lookup_error = lookup_error
        self.user = _User(999, "Meow")

    def get_user(self, uid):
        return self.users.get(uid)

    async def fetch_user(self, uid):
        if self.lookup_error:
            raise RuntimeError("Discord said no")
        return self.users.get(uid)

    def add_view(self, view):
        self.views.append(view)

    def is_module_enabled(self, _guild_id, _module_id):
        return self.module_on


def _row(**overrides) -> dict:
    """A stored question, as ``utils.qna`` hands one back."""
    record = {
        "id": 0x4F2A,
        "guild_id": GUILD_ID,
        "guild_name": GUILD_NAME,
        "user_id": ASKER_ID,
        "user_name": ASKER_NAME,
        "question": "why is the sky blue?",
        "asked_at": 1767225600.0,
        "relayed": True,
        "relay_detail": "",
        "owner_message_id": 0,
        "status": qna.OPEN,
        "answered_at": 0.0,
        "answer": "",
    }
    record.update(overrides)
    return record


# --------------------------------------------------------------------------- #
# The public reading
# --------------------------------------------------------------------------- #
class ThePublicMessage(unittest.TestCase):
    """What the channel is shown, and every field of it."""

    def test_the_relay_is_a_closed_field_list(self):
        """A new field is a failing test, not a silent policy change.

        The same rule the out-of-band alerts are held to: the disclosure *is* the
        specification, so adding something to what leaves this process has to be a
        decision somebody made on purpose.
        """
        payload = qna.relay_payload(_row(asked_at=0))
        self.assertEqual(sorted(payload), ["allowed_mentions", "embeds", "username"])
        self.assertEqual(
            sorted(payload["embeds"][0]),
            ["color", "description", "footer", "timestamp", "title"],
        )

    def test_nothing_about_the_asker_reaches_the_webhook(self):
        blob = json.dumps(qna.relay_payload(_row()))
        self.assertNotIn(ASKER_NAME, blob)
        self.assertNotIn(str(ASKER_ID), blob)
        self.assertNotIn(str(GUILD_ID), blob)

    def test_the_author_is_absent_rather_than_disguised(self):
        self.assertEqual(qna.relay_payload(_row())["username"], qna.PUBLIC_NAME)
        self.assertNotEqual(qna.PUBLIC_NAME, ASKER_NAME)

    def test_a_question_cannot_ping_anybody(self):
        """The question is a stranger's text; ``@everyone`` in it is inert."""
        payload = qna.relay_payload(_row(question="@everyone read this"))
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("content", payload)

    def test_the_relay_names_the_question_and_how_to_ask_one(self):
        embed = qna.relay_payload(_row())["embeds"][0]
        self.assertEqual(embed["description"], "why is the sky blue?")
        self.assertIn(qna.handle(_row()), embed["footer"]["text"])
        self.assertIn("/qna", embed["footer"]["text"])

    def test_an_empty_question_does_not_render_blank(self):
        embed = qna.relay_payload(_row(question="   "))["embeds"][0]
        self.assertTrue(embed["description"].strip())

    def test_the_handle_is_the_id_and_says_nothing_about_anybody(self):
        self.assertEqual(qna.handle({"id": 0x4F2A}), "anon 4F2A")
        self.assertNotEqual(qna.handle({"id": 1}), qna.handle({"id": 2}))

    def test_the_colour_is_the_products_own(self):
        """One accent, and the same one — not a hue invented for this feature."""
        self.assertEqual(qna.ACCENT, style.ACCENT)


class TheOwnerReading(unittest.TestCase):
    """The other half: the same row, read by the person who has to be told."""

    def test_the_owner_is_told_who_asked_and_where(self):
        rendered = dict(qna.owner_fields(_row()))
        self.assertIn(ASKER_NAME, rendered["From"])
        self.assertIn(str(ASKER_ID), rendered["From"])
        self.assertEqual(rendered["Server"], GUILD_NAME)
        self.assertIn(qna.handle(_row()), rendered["Public as"])

    def test_the_owner_is_told_when_in_their_own_clock(self):
        """"3:33 am Indian time" is a fact this feature reports back.

        A fixed +05:30 and not ``zoneinfo``: the zone database is absent on a slim
        host, where a missing one raises — and a bot command must not fail over a
        timestamp. India has had one offset and no daylight saving since 1945, so
        this is IST rather than an approximation of it.
        """
        self.assertEqual(qna.IST.utcoffset(None), timedelta(hours=5, minutes=30))
        self.assertEqual(qna.IST.tzname(None), "IST")
        self.assertEqual(qna.ist_stamp(1767225600), "01 Jan 2026, 05:30 IST")

    def test_an_unreadable_timestamp_does_not_raise(self):
        self.assertTrue(qna.ist_stamp(None).endswith("IST"))
        self.assertTrue(qna.ist_stamp("not a time").endswith("IST"))

    def test_the_operators_row_carries_both_halves_of_the_promise(self):
        """The name, and the handle the channel saw, in one dict — on purpose.

        A console can only show an operator *whether* a question is worth a DM if
        it can put the asker beside the question, so this reading has to carry
        the name. What keeps that from being a leak is that it is not the reading
        the channel gets: the same row through :func:`qna.relay_payload` — the
        function that builds what leaves this process — is checked below for
        every field this one adds.
        """
        row = qna.owner_row(_row())
        self.assertEqual(row["asker"], ASKER_NAME)
        self.assertEqual(row["asker_id"], str(ASKER_ID))
        self.assertEqual(row["handle"], qna.handle(_row()))
        self.assertEqual(row["server"], GUILD_NAME)
        self.assertEqual(row["question"], "why is the sky blue?")
        # The moment as well as the sentence about it: the console sorts by the
        # first and prints the second, and re-parsing "01 Jan 2026, 05:30 IST"
        # back into a number is exactly the kind of accident this avoids.
        self.assertEqual(row["asked_at"], _row()["asked_at"])
        self.assertIn("IST", row["asked"])
        blob = json.dumps(qna.relay_payload(_row()))
        for private in (row["asker"], row["asker_id"], row["server"]):
            self.assertNotIn(private, blob)

    def test_a_question_the_channel_never_saw_says_so(self):
        """A row that is merely unanswered and one that never went out differ.

        ``relayed`` is the only field that separates them, and the console is the
        one surface where the difference is visible — so the delivery's own
        reason travels with it rather than being reduced to a boolean.
        """
        missed = qna.owner_row(_row(relayed=False, relay_detail="HTTPError 404"))
        self.assertFalse(missed["relayed"])
        self.assertEqual(missed["relay_detail"], "HTTPError 404")
        self.assertTrue(qna.owner_row(_row())["relayed"])

    def test_a_row_a_backup_forgot_a_field_for_is_still_readable(self):
        """The console is what an operator opens when something looks wrong.

        A reading that raised on one odd row would be a page that shows nothing
        at all — including for the fifty questions beside the odd one.
        """
        row = qna.owner_row({})
        self.assertEqual(row["id"], 0)
        self.assertEqual(row["asker"], "unknown")
        self.assertEqual(row["question"], "*(the question arrived empty)*")
        self.assertTrue(row["asked"].endswith("IST"))


class TheAnswer(unittest.TestCase):
    def test_the_answer_quotes_the_question_it_answers(self):
        body = qna.answer_body(_row(), "because of Rayleigh scattering")
        self.assertIn("Rayleigh scattering", body)
        self.assertIn("why is the sky blue?", body)

    def test_an_empty_or_oversized_answer_is_handled(self):
        self.assertIn("In answer to", qna.answer_body(_row(), ""))
        body = qna.answer_body(_row(), "x" * (qna.MAX_ANSWER + 500))
        self.assertLess(len(body), qna.MAX_ANSWER + 200)


# --------------------------------------------------------------------------- #
# Posting
# --------------------------------------------------------------------------- #
class _Opener:
    """A urlopen answer: a context manager, the way the real one is used."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, _count=1):
        return b""


class _Answer:
    """A urlopen stand-in: records the request, answers or refuses."""

    def __init__(self, error=None):
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return _Opener()


class PostingToTheWebhook(unittest.TestCase):
    def test_no_webhook_is_a_refusal_that_names_the_setting(self):
        ok, detail = qna.post("", {"username": "Anonymous"})
        self.assertFalse(ok)
        self.assertIn(qna.SETTING, detail)

    def test_a_question_is_posted_and_reported_as_posted(self):
        opener = _Answer()
        with mock.patch("urllib.request.urlopen", opener):
            ok, detail = qna.post(WEBHOOK, qna.relay_payload(_row()))
        self.assertTrue(ok)
        self.assertEqual(detail, "")
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(opener.requests[0].full_url, WEBHOOK)
        self.assertEqual(opener.requests[0].method, "POST")

    def test_a_refusing_webhook_is_reported_rather_than_raised(self):
        """The asker is waiting for an answer either way."""
        opener = _Answer(error=RuntimeError("403"))
        with mock.patch("urllib.request.urlopen", opener):
            ok, detail = qna.post(WEBHOOK, qna.relay_payload(_row()))
        self.assertFalse(ok)
        self.assertIn("RuntimeError", detail)


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #
class TheRecord(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("qna_record.db")
        await db.setup()

    async def test_a_question_round_trips_with_its_asker(self):
        record = await qna.record(
            guild_id=GUILD_ID,
            guild_name=GUILD_NAME,
            user_id=ASKER_ID,
            user_name=ASKER_NAME,
            question="hello",
        )
        self.assertTrue(record["id"])
        self.assertEqual(record["status"], qna.OPEN)
        self.assertFalse(record["relayed"])
        stored = await qna.get(record["id"])
        self.assertEqual(stored["user_name"], ASKER_NAME)
        self.assertEqual(stored["user_id"], ASKER_ID)
        self.assertEqual(stored["guild_name"], GUILD_NAME)

    async def test_the_inbox_is_newest_first_and_reads_one_server(self):
        for number in (1, 2, 3):
            await qna.record(
                guild_id=GUILD_ID,
                guild_name=GUILD_NAME,
                user_id=ASKER_ID,
                user_name=ASKER_NAME,
                question=f"q{number}",
                asked_at=1000 + number,
            )
        await qna.record(
            guild_id=999,
            guild_name="Other",
            user_id=ASKER_ID,
            user_name=ASKER_NAME,
            question="elsewhere",
            asked_at=9999,
        )
        mine = await qna.open_questions(guild_id=GUILD_ID)
        self.assertEqual([row["question"] for row in mine], ["q3", "q2", "q1"])
        everyone = await qna.open_questions()
        self.assertEqual(everyone[0]["question"], "elsewhere")

    async def test_a_question_is_claimed_exactly_once(self):
        record = await qna.record(
            guild_id=GUILD_ID,
            guild_name=GUILD_NAME,
            user_id=ASKER_ID,
            user_name=ASKER_NAME,
            question="one",
        )
        self.assertTrue(await qna.mark_answered(record["id"], "first"))
        self.assertFalse(await qna.mark_answered(record["id"], "second"))
        stored = await qna.get(record["id"])
        self.assertEqual(stored["answer"], "first")
        self.assertEqual(stored["status"], qna.ANSWERED)

    async def test_an_undelivered_answer_goes_back_to_the_inbox(self):
        record = await qna.record(
            guild_id=GUILD_ID,
            guild_name=GUILD_NAME,
            user_id=ASKER_ID,
            user_name=ASKER_NAME,
            question="one",
        )
        await qna.mark_answered(record["id"], "lost")
        await qna.reopen(record["id"])
        stored = await qna.get(record["id"])
        self.assertEqual(stored["status"], qna.OPEN)
        self.assertEqual(stored["answer"], "")
        self.assertEqual(len(await qna.open_questions()), 1)

    async def test_an_owner_dm_finds_its_own_question_only(self):
        first = await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="first",
        )
        second = await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="second",
        )
        await qna.attach_owner_message(first["id"], 111)
        await qna.attach_owner_message(second["id"], 222)
        found = await qna.for_owner_message(222)
        self.assertEqual(found["question"], "second")
        self.assertIsNone(await qna.for_owner_message(333))
        self.assertIsNone(await qna.for_owner_message(0))

    async def test_counts_report_the_two_states(self):
        open_row = await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="open",
        )
        answered = await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="answered",
        )
        await qna.mark_answered(answered["id"], "done")
        self.assertEqual(await qna.counts(), {"open": 1, "answered": 1})
        self.assertIsNotNone(await qna.get(open_row["id"]))


class TheOperatorScreen(TempDatabaseMixin, unittest.TestCase):
    """The console's reading: the same rows, off a thread with no event loop.

    `/api/console/questions` has to answer while the bot is restarting, which is
    the one moment there is no loop to await on — so the reading is synchronous
    and this is where that is proven, including the state the page must not get
    wrong: a box that could not be read is *unknown*, never empty.
    """

    def setUp(self):
        self.use_temp_database("qna_screen.db")
        asyncio.run(db.setup())

    def tearDown(self):
        self.release_temp_database()

    def ask(self, question="why is the sky blue?", *, relayed=True, when=1000.0):
        return asyncio.run(
            qna.record(
                guild_id=GUILD_ID,
                guild_name=GUILD_NAME,
                user_id=ASKER_ID,
                user_name=ASKER_NAME,
                question=question,
                asked_at=when,
            )
        )

    def test_the_reading_names_the_asker_and_the_handle_the_channel_saw(self):
        self.ask()
        reading = qna.owner_inbox_sync()
        self.assertTrue(reading["available"])
        row = reading["rows"][0]
        self.assertEqual(row["asker"], ASKER_NAME)
        self.assertEqual(row["asker_id"], str(ASKER_ID))
        self.assertEqual(row["handle"], qna.handle(row))
        self.assertEqual(row["server"], GUILD_NAME)

    def test_the_counts_are_the_tables_own_and_not_the_lists_length(self):
        """A page that said "3 open" above a capped list of one would be wrong.

        The list is always capped — the console asks for twenty — so the numbers
        have to come from the table rather than from what fits on screen.
        """
        for index in range(3):
            self.ask(f"q{index}", when=1000.0 + index)
        answered = self.ask("answered")
        asyncio.run(qna.mark_answered(answered["id"], "done"))
        reading = qna.owner_inbox_sync(limit=1)
        self.assertEqual(len(reading["rows"]), 1)
        self.assertEqual(reading["shown"], 1)
        self.assertEqual(reading["open"], 3)
        self.assertEqual(reading["answered"], 1)
        # Newest first, so the one row a capped list shows is the newest.
        self.assertEqual(reading["rows"][0]["question"], "q2")

    def test_the_box_that_is_not_configured_still_reads(self):
        """The setting being absent is why nothing is *arriving*, not a reason
        to hide what already arrived — and the page says which setting it is."""
        self.ask()
        with mock.patch.dict(os.environ, {"QNA_WEBHOOK_URL": ""}):
            reading = qna.owner_inbox_sync()
        self.assertFalse(reading["configured"])
        self.assertEqual(reading["setting"], "QNA_WEBHOOK_URL")
        self.assertEqual(len(reading["rows"]), 1)

    def test_an_unreadable_box_is_unknown_rather_than_empty(self):
        """The distinction the whole page is built on, checked at its source.

        "Nothing is waiting" about a list nobody could open is the one sentence
        an operator would act on by doing nothing at all.
        """
        with mock.patch.object(qna.db, "connect", side_effect=RuntimeError("no such table")):
            reading = qna.owner_inbox_sync()
        self.assertFalse(reading["available"])
        self.assertEqual(reading["rows"], [])
        self.assertIn("unknown rather than empty", reading["because"])

    def test_the_reading_creates_the_table_a_restored_backup_forgot(self):
        """A database restored from before this feature existed is the exact
        case: the page must be an empty list, not an error and not a 500."""
        conn = db.connect()
        conn.execute(f"DROP TABLE IF EXISTS {qna.TABLE}")
        conn.commit()
        conn.close()
        reading = qna.owner_inbox_sync()
        self.assertTrue(reading["available"])
        self.assertEqual(reading["rows"], [])
        self.assertEqual(reading["open"], 0)


# --------------------------------------------------------------------------- #
# The command, end to end
# --------------------------------------------------------------------------- #
class _QnaCase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """The real callbacks, a throwaway database, and one patched webhook.

    ``GUILD_ID`` is declared as *the bot's own server* for the whole class, since
    that is where the operator's side of the box lives and every surface it reaches
    is a surface that refuses anywhere else. The refusal itself is the subject of
    :class:`TheOperatorsSideIsOneServer`, which patches it away.
    """

    async def asyncSetUp(self):
        self.use_temp_database("qna_cog.db")
        await db.setup()
        self.asker = _User(ASKER_ID, ASKER_NAME)
        self.owner = _User(OWNER_ID, OWNER_NAME)
        self.bot = _Bot(users=(self.asker, self.owner))
        self.cog = qna_cog.Qna(self.bot)
        self.environ = mock.patch.dict(
            os.environ,
            {
                "OWNER_USER_ID": str(OWNER_ID),
                "QNA_WEBHOOK_URL": WEBHOOK,
                "BOT_SERVER_GUILD_ID": str(GUILD_ID),
            },
        )
        self.environ.start()
        self.addCleanup(self.environ.stop)
        owner_patch = mock.patch.object(admin_limits, "OWNER_USER_ID", OWNER_ID)
        owner_patch.start()
        self.addCleanup(owner_patch.stop)

    def relay(self, ok=True, detail=""):
        """Patch the one thing that needs a network, and keep what it was given."""
        sent = {}

        def fake_post(url, payload, timeout=None):
            sent["url"] = url
            sent["payload"] = payload
            return ok, detail

        patcher = mock.patch.object(qna, "post", fake_post)
        patcher.start()
        self.addCleanup(patcher.stop)
        return sent


class Asking(_QnaCase):
    async def test_the_question_is_relayed_and_only_the_owner_learns_who_asked(self):
        """The headline claim, driven through the real callback."""
        sent = self.relay()
        interaction = _Interaction(user=self.asker, bot=self.bot)

        await qna_cog.submit_question(interaction, "why is the sky blue?")

        # 1. what the channel is shown says nothing about the asker
        blob = json.dumps(sent["payload"])
        self.assertNotIn(ASKER_NAME, blob)
        self.assertNotIn(str(ASKER_ID), blob)
        self.assertEqual(sent["payload"]["username"], qna.PUBLIC_NAME)
        self.assertEqual(sent["url"], WEBHOOK)
        # 2. the asker is told, privately
        self.assertTrue(
            any(message.get("ephemeral") for message in interaction.answered),
            interaction.answered,
        )
        self.assertIn("anonymously", interaction.answered[-1]["content"])
        # 3. the owner is told *with the name*, on a card that carries the button
        self.assertEqual(len(self.owner.dms), 1)
        card = self.owner.dms[0]["view"]
        self.assertIn(ASKER_NAME, card_text(card))
        self.assertIn("why is the sky blue?", card_text(card))
        custom_ids = [
            getattr(child, "custom_id", None) for child in card.walk_children()
        ]
        self.assertEqual(custom_ids.count(qna_cog.REPLY_BUTTON_ID), 1)
        # 4. and the row behind it remembers the name, for /qna inbox to read
        [record] = await qna.open_questions()
        self.assertEqual(record["user_name"], ASKER_NAME)
        self.assertEqual(record["user_id"], ASKER_ID)
        self.assertTrue(record["relayed"])
        # The card's own id, so the button on it can find this question again.
        self.assertEqual(record["owner_message_id"], 900_001)

    async def test_an_unconfigured_box_refuses_and_writes_nothing_down(self):
        self.relay()
        with mock.patch.dict(os.environ, {"QNA_WEBHOOK_URL": ""}):
            interaction = _Interaction(user=self.asker, bot=self.bot)
            await qna_cog.submit_question(interaction, "anyone there?")
        self.assertIn(qna.SETTING, interaction.answered[-1]["content"])
        self.assertEqual(await qna.open_questions(), [])
        self.assertEqual(self.owner.dms, [])

    async def test_a_webhook_that_refuses_is_reported_to_the_asker(self):
        self.relay(ok=False, detail="the webhook did not accept it (HTTPError)")
        interaction = _Interaction(user=self.asker, bot=self.bot)
        await qna_cog.submit_question(interaction, "did this work?")
        self.assertIn("refused", interaction.answered[-1]["content"])
        [record] = await qna.open_questions()
        self.assertFalse(record["relayed"])
        self.assertIn("HTTPError", record["relay_detail"])

    async def test_a_question_asked_in_a_dm_is_told_where_questions_go(self):
        interaction = _Interaction(user=self.asker, guild=False, bot=self.bot)
        await qna_cog.submit_question(interaction, "hello?")
        self.assertIn("in a server", interaction.answered[-1]["content"])

    async def test_the_box_reaches_the_owner_even_with_no_owner_configured(self):
        """The DM is a courtesy; the record is the feature."""
        self.relay()
        with mock.patch.dict(os.environ, {"OWNER_USER_ID": ""}):
            interaction = _Interaction(user=self.asker, bot=self.bot)
            await qna_cog.submit_question(interaction, "no owner set")
        self.assertEqual(self.owner.dms, [])
        self.assertEqual(len(await qna.open_questions()), 1)


class Answering(_QnaCase):
    async def _ask(self, question="why is the sky blue?") -> dict:
        return await qna.record(
            guild_id=GUILD_ID,
            guild_name=GUILD_NAME,
            user_id=ASKER_ID,
            user_name=ASKER_NAME,
            question=question,
        )

    async def test_the_answer_is_dmed_and_the_question_closed(self):
        record = await self._ask()
        interaction = _Interaction(user=self.owner, bot=self.bot)
        await qna_cog.deliver_reply(interaction, record["id"], "Rayleigh scattering")
        self.assertEqual(len(self.asker.dms), 1)
        self.assertIn("Rayleigh scattering", card_text(self.asker.dms[0]["view"]))
        self.assertIn("why is the sky blue?", card_text(self.asker.dms[0]["view"]))
        stored = await qna.get(record["id"])
        self.assertEqual(stored["status"], qna.ANSWERED)
        self.assertIn("Sent to", interaction.answered[-1]["content"])
        self.assertIn(ASKER_NAME, interaction.answered[-1]["content"])

    async def test_a_second_answer_cannot_dm_the_asker_twice(self):
        record = await self._ask()
        first = _Interaction(user=self.owner, bot=self.bot)
        second = _Interaction(user=self.owner, bot=self.bot)
        await qna_cog.deliver_reply(first, record["id"], "the first answer")
        await qna_cog.deliver_reply(second, record["id"], "the second answer")
        self.assertEqual(len(self.asker.dms), 1)
        self.assertIn("already has an answer", second.answered[-1]["content"])

    async def test_a_refused_dm_leaves_the_question_waiting(self):
        record = await self._ask()
        self.asker.dm_error = RuntimeError("50007 Cannot send messages to this user")
        interaction = _Interaction(user=self.owner, bot=self.bot)
        await qna_cog.deliver_reply(interaction, record["id"], "hello?")
        self.assertIn("back in", interaction.answered[-1]["content"])
        stored = await qna.get(record["id"])
        self.assertEqual(stored["status"], qna.OPEN)
        self.assertEqual(stored["answer"], "")
        self.assertEqual(len(await qna.open_questions()), 1)

    async def test_an_empty_answer_is_not_sent(self):
        record = await self._ask()
        interaction = _Interaction(user=self.owner, bot=self.bot)
        await qna_cog.deliver_reply(interaction, record["id"], "   ")
        self.assertEqual(self.asker.dms, [])
        self.assertNotEqual((await qna.get(record["id"]))["status"], qna.ANSWERED)

    async def test_a_stranger_cannot_answer_and_is_not_shown_the_name(self):
        record = await self._ask()
        stranger = _User(555, "NotTheOwner")
        interaction = _Interaction(user=stranger, bot=_Bot(users=(stranger,)))
        await qna_cog.deliver_reply(interaction, record["id"], "an impostor's answer")
        self.assertEqual(self.asker.dms, [])
        self.assertIn("owner", interaction.answered[-1]["content"])
        self.assertEqual((await qna.get(record["id"]))["status"], qna.OPEN)

    async def test_an_unknown_question_is_refused_by_id(self):
        interaction = _Interaction(user=self.owner, bot=self.bot)
        await qna_cog.deliver_reply(interaction, 987654, "hello?")
        self.assertIn("987654", interaction.answered[-1]["content"])

    async def test_the_screen_and_the_button_share_one_delivery(self):
        """The console calls ``deliver_answer`` itself, on the bot's own loop.

        The order inside it — claim the question, *then* DM — is the whole
        protection against an asker being DMed twice, and the console is a second
        caller of it rather than a second implementation of it. Driven here the
        way the route drives it (no interaction, just the client), because that
        is the difference between the two surfaces and nothing else is.
        """
        record = await self._ask()
        first = await qna_cog.deliver_answer(self.bot, record["id"], "from the web")
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["outcome"], "sent")
        self.assertEqual(first["handle"], qna.handle(record))
        self.assertEqual(first["asker"], ASKER_NAME)
        self.assertEqual(len(self.asker.dms), 1)
        # …and the button on the card, clicked afterwards, DMs nobody a second
        # time: the question is already claimed.
        second = await qna_cog.deliver_answer(self.bot, record["id"], "from the card")
        self.assertEqual(second["outcome"], "already")
        self.assertEqual(len(self.asker.dms), 1)
        self.assertEqual((await qna.get(record["id"]))["answer"], "from the web")

    async def test_a_refused_dm_puts_the_question_back_for_every_surface(self):
        record = await self._ask()
        self.asker.dm_error = RuntimeError("50007 Cannot send messages to this user")
        result = await qna_cog.deliver_answer(self.bot, record["id"], "hello?")
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "refused")
        # The reason names the *kind* of refusal, not the exception's text: a DM
        # that failed because the asker closed their DMs and one that failed
        # because Discord was down read the same to an operator, and the class
        # name is the part that distinguishes "they will never get this".
        self.assertIn("refused the DM", result["reason"])
        self.assertIn("RuntimeError", result["reason"])
        stored = await qna.get(record["id"])
        self.assertEqual(stored["status"], qna.OPEN)
        self.assertEqual(stored["answer"], "")

    async def test_an_empty_answer_never_reaches_the_lookup(self):
        """Checked before the question is read, so nothing is claimed for it."""
        record = await self._ask()
        result = await qna_cog.deliver_answer(self.bot, record["id"], "   ")
        self.assertEqual(result["outcome"], "empty")
        self.assertEqual(self.asker.dms, [])
        self.assertEqual((await qna.get(record["id"]))["status"], qna.OPEN)

    def test_one_outcome_is_worded_the_same_way_wherever_it_happened(self):
        """The sentence is the product's, not the surface's.

        The single difference is *where a question goes back to*: a command on
        Discord, the list on a screen. Everything else — including the fact that
        nobody was DMed twice — has to read the same, because an operator who
        answered from the console and then from their phone should not be told
        two different things about the same question.
        """
        sent = {"outcome": "sent", "handle": "anon 4F2A", "asker": ASKER_NAME}
        self.assertEqual(
            qna_cog.answer_sentence(sent),
            qna_cog.answer_sentence(sent, inbox="this list"),
        )
        self.assertIn(ASKER_NAME, qna_cog.answer_sentence(sent))
        # …and the emphasis is the surface's, because the console shows this
        # sentence in HTML where nothing renders markdown: the two differ by the
        # asterisks and by nothing else, which is the claim being pinned.
        card = qna_cog.answer_sentence(sent)
        web = qna_cog.answer_sentence(sent, bold=False)
        self.assertIn(f"**{ASKER_NAME}**", card)
        self.assertNotIn("**", web)
        self.assertEqual(card.replace("**", ""), web)
        already = {"outcome": "already", "handle": "anon 4F2A"}
        self.assertEqual(
            qna_cog.answer_sentence(already),
            qna_cog.answer_sentence(already, inbox="this list"),
        )
        refused = {"outcome": "refused", "reason": "Discord said no"}
        self.assertIn("back in /qna inbox", qna_cog.answer_sentence(refused))
        self.assertIn("back in this list", qna_cog.answer_sentence(refused, inbox="this list"))
        # The reason is carried through rather than replaced by a generic apology.
        self.assertIn("Discord said no", qna_cog.answer_sentence(refused))
        unknown = {"outcome": "unknown", "id": 987654}
        self.assertIn("987654", qna_cog.answer_sentence(unknown))


class TheOwnersSurfaces(_QnaCase):
    async def test_the_button_opens_the_form_for_its_own_question(self):
        record = await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="from the card",
        )
        await qna.attach_owner_message(record["id"], 555000)
        interaction = _Interaction(user=self.owner, message_id=555000, bot=self.bot)
        await qna_cog.open_reply_modal(interaction)
        self.assertEqual(len(interaction.response.modals), 1)
        modal = interaction.response.modals[0]
        self.assertEqual(modal.question_id, record["id"])
        self.assertEqual(modal.custom_id, f"{qna_cog.ANSWER_MODAL_PREFIX}{record['id']}")

    async def test_the_registered_view_answers_a_card_it_did_not_send(self):
        """What the boot-time registration is *for*, driven the way Dispatch does it.

        A card clicked after a redeploy resolves to this view's own button —
        ``await item.callback(interaction)`` is exactly what discord.py runs — so
        the entry point is exercised rather than merely registered.
        """
        record = await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="asked before a redeploy",
        )
        await qna.attach_owner_message(record["id"], 987654)
        view = qna_cog.QnaReplyView()
        button, = view.children
        interaction = _Interaction(user=self.owner, message_id=987654, bot=self.bot)
        await button.callback(interaction)
        self.assertEqual(interaction.response.modals[0].question_id, record["id"])

    async def test_a_stranger_cannot_open_the_form(self):
        record = await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="private",
        )
        await qna.attach_owner_message(record["id"], 555000)
        stranger = _User(556, "Curious")
        interaction = _Interaction(user=stranger, message_id=555000, bot=_Bot(users=(stranger,)))
        await qna_cog.open_reply_modal(interaction)
        self.assertEqual(interaction.response.modals, [])

    async def test_the_inbox_names_the_asker_to_the_owner(self):
        """The one other surface that renders the name, and it renders it privately.

        ``send_panel`` is recorded rather than followed, because *ephemeral* is
        the claim here and the flag only reaches Discord on the real interaction
        path — a stub that swallowed it would let this card start being posted
        into the channel without failing anything.
        """
        await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="in the inbox",
        )
        calls = []

        async def record_panel(target, view, **kwargs):
            calls.append({"target": target, "view": view, **kwargs})

        interaction = _Interaction(user=self.owner, bot=self.bot)
        with mock.patch.object(qna_cog.style, "send_panel", record_panel):
            await self.cog.qna_inbox.callback(self.cog, interaction)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].get("ephemeral"))
        self.assertIs(calls[0]["target"], interaction)
        body = card_text(calls[0]["view"])
        self.assertIn(ASKER_NAME, body)
        self.assertIn("in the inbox", body)

    async def test_the_inbox_refuses_a_stranger(self):
        stranger = _User(557, "Nosy")
        interaction = _Interaction(user=stranger, bot=_Bot(users=(stranger,)))
        await self.cog.qna_inbox.callback(self.cog, interaction)
        self.assertNotIn("view", interaction.answered[-1])

    async def test_an_empty_inbox_says_so(self):
        interaction = _Interaction(user=self.owner, bot=self.bot)
        await self.cog.qna_inbox.callback(self.cog, interaction)
        self.assertIn("No questions", interaction.answered[-1]["content"])

    async def test_replying_without_an_id_answers_the_newest(self):
        await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="older", asked_at=1000,
        )
        newest = await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="newest", asked_at=2000,
        )
        interaction = _Interaction(user=self.owner, bot=self.bot)
        await self.cog.qna_reply.callback(self.cog, interaction, None)
        self.assertEqual(interaction.response.modals[0].question_id, newest["id"])

    async def test_asking_opens_the_form_and_never_posts_it(self):
        # In a server that is *not* the bot's own, which is the only place the
        # command opens a form at all — see ``TheOperatorsSideIsOneServer``.
        with mock.patch.dict(os.environ, {"BOT_SERVER_GUILD_ID": "999999"}):
            interaction = _Interaction(user=self.asker, bot=self.bot)
            await self.cog.qna_ask.callback(self.cog, interaction)
        self.assertEqual(len(interaction.response.modals), 1)
        modal = interaction.response.modals[0]
        self.assertIsNotNone(modal.question)
        self.assertEqual(modal.question.max_length, qna.MAX_QUESTION)

    async def test_a_question_box_can_be_turned_off_for_one_server(self):
        """The gate is the *server's* switch, read off the cog's own client."""
        off_cog = qna_cog.Qna(_Bot(module_on=False))
        off = _Interaction(user=self.asker, bot=_Bot(module_on=False))
        self.assertFalse(await off_cog.interaction_check(off))
        self.assertIn("turned off", off.answered[-1]["content"])
        on = _Interaction(user=self.asker, bot=self.bot)
        self.assertTrue(await self.cog.interaction_check(on))

    async def test_the_dm_form_is_registered_so_the_button_outlives_a_deploy(self):
        """The button on an old card is dispatched by ``custom_id`` alone.

        A view registered once, with no state in it, is what makes a card clicked
        the next morning work — the question is found from the message the card
        sits on rather than carried in the button.
        """
        self.assertEqual(len(self.bot.views), 1)
        view = self.bot.views[0]
        self.assertTrue(view.is_persistent())
        self.assertEqual(
            [item.custom_id for item in view.children], [qna_cog.REPLY_BUTTON_ID]
        )


class TheOperatorsSideIsOneServer(_QnaCase):
    """Who may answer, *where*, and the one command that moves the other way.

    The box is anonymous because two different people see two different halves: the
    server reads the question, the operator reads who asked. Both of these refusals
    are that sentence taken seriously rather than a permission setting — asked in
    the operator's own server there is nobody the name is hidden *from*, and
    answered anywhere else the answer is written by somebody the deployment cannot
    vouch for while the asker believes it came from the bot's owner.
    """

    async def test_asking_is_refused_in_the_bots_own_server(self):
        interaction = _Interaction(user=self.asker, bot=self.bot)
        await self.cog.qna_ask.callback(self.cog, interaction)

        self.assertEqual(interaction.response.modals, [], "the form opened here")
        self.assertIn("bot's own server", interaction.answered[-1]["content"])

    async def test_asking_still_works_in_any_other_server(self):
        # The half that must not become collateral damage: the command is for the
        # servers the bot is a visitor in, which is all of them but one.
        with mock.patch.dict(os.environ, {"BOT_SERVER_GUILD_ID": "999999"}):
            interaction = _Interaction(user=self.asker, bot=self.bot)
            await self.cog.qna_ask.callback(self.cog, interaction)
        self.assertEqual(len(interaction.response.modals), 1)

    async def test_the_inbox_is_refused_in_any_other_server(self):
        with mock.patch.dict(os.environ, {"BOT_SERVER_GUILD_ID": "999999"}):
            interaction = _Interaction(user=self.owner, bot=self.bot)
            await self.cog.qna_inbox.callback(self.cog, interaction)

        self.assertIn("only runs in the bot's own server", interaction.answered[-1]["content"])
        # Refused for the *place*, not for the person: the owner is who this
        # message is written for, and it has to say what would work instead.
        self.assertIn("/qna ask", interaction.answered[-1]["content"])

    async def test_the_reply_is_refused_in_any_other_server(self):
        with mock.patch.dict(os.environ, {"BOT_SERVER_GUILD_ID": "999999"}):
            interaction = _Interaction(user=self.owner, bot=self.bot)
            await self.cog.qna_reply.callback(self.cog, interaction, None)
        self.assertEqual(interaction.response.modals, [])

    async def test_the_older_of_the_bots_two_servers_answers_too(self):
        """The first version of this gate named one server and refused in the other.

        ``BOT_SERVER_GUILD_ID`` names where the bot's own output lands today; the
        legacy update channel lives in the server its notes were hardcoded to
        *first*, which is where the Dev role the ping has always tagged was made.
        Both are the operator's, and a command that works in one and is refused in
        the other is a command that looks broken wherever they happen to be.
        """
        legacy = SimpleNamespace(
            id=bot_channels.LEGACY_UPDATE_CHANNEL_ID, name="meow-bot-update"
        )
        interaction = _Interaction(user=self.owner, bot=self.bot, channels=(legacy,))
        with mock.patch.dict(os.environ, {"BOT_SERVER_GUILD_ID": "999999"}):
            await self.cog.qna_inbox.callback(self.cog, interaction)

        self.assertIn("No questions", interaction.answered[-1]["content"])

    async def test_asking_is_refused_there_as_well(self):
        """The other half of the same sentence: if two servers are the bot's own,
        then the question box is for the servers that are *not*."""
        legacy = SimpleNamespace(
            id=bot_channels.LEGACY_UPDATE_CHANNEL_ID, name="meow-bot-update"
        )
        interaction = _Interaction(user=self.asker, bot=self.bot, channels=(legacy,))
        with mock.patch.dict(os.environ, {"BOT_SERVER_GUILD_ID": "999999"}):
            await self.cog.qna_ask.callback(self.cog, interaction)

        self.assertEqual(interaction.response.modals, [], "the form opened here")

    async def test_a_member_without_the_dev_role_cannot_read_the_inbox(self):
        member = _User(559, "Member")
        interaction = _Interaction(user=member, bot=_Bot(users=(member,)))
        await self.cog.qna_inbox.callback(self.cog, interaction)

        self.assertNotIn("view", interaction.answered[-1])
        self.assertIn("Dev", interaction.answered[-1]["content"])

    async def test_a_member_with_the_dev_role_may_read_the_inbox(self):
        dev = _User(560, "Operator")
        interaction = _Interaction(user=dev, bot=_Bot(users=(dev,)), roles=(DEV_ROLE,))
        await self.cog.qna_inbox.callback(self.cog, interaction)

        self.assertIn("No questions", interaction.answered[-1]["content"])

    async def test_a_dev_answers_a_question_end_to_end(self):
        """Not the owner, and the asker still hears back.

        The whole promise is that the answer arrives; a Dev let into the inbox but
        refused at the last step would look exactly like this working.
        """
        record = await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="who answers this?",
        )
        dev = _User(560, "Operator")
        interaction = _Interaction(user=dev, bot=_Bot(users=(dev, self.asker)), roles=(DEV_ROLE,))
        await qna_cog.deliver_reply(interaction, record["id"], "a Dev does")

        self.assertEqual(len(self.asker.dms), 1)
        self.assertIn("a Dev does", card_text(self.asker.dms[0]["view"]))
        self.assertEqual((await qna.get(record["id"]))["status"], qna.ANSWERED)

    async def test_a_member_without_the_dev_role_cannot_answer_through_the_form(self):
        record = await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="who answers this?",
        )
        member = _User(561, "Member")
        interaction = _Interaction(user=member, bot=_Bot(users=(member,)))
        await qna_cog.deliver_reply(interaction, record["id"], "not me")
        self.assertEqual(self.asker.dms, [])
        self.assertEqual((await qna.get(record["id"]))["status"], qna.OPEN)

    async def test_the_form_on_the_owners_card_is_the_owners_alone(self):
        """A DM has no roles to read, so the owner is the only answer there.

        A Dev reaching the same modal through ``/qna reply`` and reaching it through
        the owner's private card are *not* the same act: the card was sent to one
        person, and handing it to a role would hand that person's DM to them.
        """
        owner_card = _Interaction(user=self.owner, guild=False, bot=self.bot)
        self.assertTrue(await qna_cog._is_owner(owner_card))
        dev = _User(560, "Operator")
        dev_card = _Interaction(user=dev, guild=False, bot=self.bot, roles=(DEV_ROLE,))
        record = await qna.record(
            guild_id=GUILD_ID, guild_name=GUILD_NAME, user_id=ASKER_ID,
            user_name=ASKER_NAME, question="from the card",
        )
        await qna.attach_owner_message(record["id"], 424242)
        dev_card.message = SimpleNamespace(id=424242)
        await qna_cog.open_reply_modal(dev_card)
        self.assertEqual(dev_card.response.modals, [])


if __name__ == "__main__":
    unittest.main()
