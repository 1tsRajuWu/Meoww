"""Feedback asked for when a ticket closes.

The rating has to survive two awkward facts: the ticket channel is deleted a
minute later, and the opener is often not the person who closed the ticket. So
the ticket's details are written to ``ticket_feedback`` at close time, the
prompt is posted in the channel *and* DMed, and the first rating to arrive wins —
a second one (the same person answering both prompts) must not log twice.

These tests drive the real button callbacks and the real database.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import tickets
from tests.support import TempDatabaseMixin
from utils import db

GUILD_ID = 424242
AUTHOR_ID = 9001
STAFF_ID = 8001
LOG_CHANNEL_ID = 7001
TICKET_CHANNEL_ID = 6001


def layout_text(node) -> str:
    """Every readable string inside a Components V2 layout, flattened."""
    parts = []
    text = getattr(node, "content", None)
    if isinstance(text, str):
        parts.append(text)
    for child in getattr(node, "children", None) or []:
        parts.append(layout_text(child))
    return "\n".join(parts)


class _Message:
    def __init__(self):
        self.threads = []

    async def create_thread(self, *, name, auto_archive_duration=None, **kwargs):
        self.threads.append(name)
        return SimpleNamespace(name=name)


class _Channel:
    def __init__(self, cid: int, name: str):
        self.id = cid
        self.name = name
        self.mention = f"<#{cid}>"
        self.topic = None
        self.sent = []
        self.messages = []
        self.deleted = False
        self.delete_error = None

    async def send(self, content=None, **kwargs):
        # ``content`` is positional-friendly, the way a real channel is.
        self.sent.append({"content": content, **kwargs})
        message = _Message()
        self.messages.append(message)
        return message

    async def edit(self, **kwargs):
        if kwargs.get("topic") is not None:
            self.topic = kwargs["topic"]

    async def delete(self, **kwargs):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted = True


class _User:
    def __init__(self, uid: int, name: str = "raj", roles=(), manage_channels=False):
        self.id = uid
        self.name = name
        self.display_name = name
        self.mention = f"<@{uid}>"
        self.display_avatar = SimpleNamespace(url="https://example.invalid/a.png")
        self.roles = list(roles)
        self.guild_permissions = SimpleNamespace(manage_channels=manage_channels)
        self.dms = []
        self.dm_error = None

    async def send(self, **kwargs):
        if self.dm_error is not None:
            raise self.dm_error
        self.dms.append(kwargs)
        return _Message()


class _Guild:
    def __init__(self, channels=()):
        self.id = GUILD_ID
        self.name = "Test Server"
        self.icon = None
        self.banner = None
        self._channels = {c.id: c for c in channels}
        self._members = {}

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_member(self, uid):
        return self._members.get(uid)


class _Client:
    def __init__(self, guild=None):
        self._guild = guild
        self.users = {}

    def get_guild(self, gid):
        return self._guild if self._guild is not None and self._guild.id == gid else None

    def get_channel(self, cid):
        return self._guild.get_channel(cid) if self._guild is not None else None

    def get_user(self, uid):
        return self.users.get(uid)

    async def fetch_user(self, uid):
        return self.users.get(uid)


class _Response:
    def __init__(self):
        self.messages = []
        self.done = False
        self.modal = None

    def is_done(self):
        return self.done

    async def send_message(self, **kwargs):
        self.done = True
        self.messages.append(kwargs)
        return _Message()

    async def edit_message(self, **kwargs):
        self.done = True
        self.edits = getattr(self, "edits", [])
        self.edits.append(kwargs)
        return _Message()

    async def send_modal(self, modal):
        self.done = True
        self.modal = modal


class _Interaction:
    def __init__(self, client, *, guild=None, channel=None, user=None):
        self.client = client
        self.guild = guild
        self.channel = channel
        self.user = user or _User(STAFF_ID, manage_channels=True)
        self.response = _Response()
        self.followups = []

    async def send(self, **kwargs):
        """``send_panel`` only skips this for a real ``discord.Interaction``."""
        return await self.response.send_message(**kwargs)


class TicketFeedbackTests(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.use_temp_database("tickets.db")

    def tearDown(self):
        tickets._FEEDBACK_EVENTS.clear()
        tickets._FEEDBACK_TASKS.clear()
        self.release_temp_database()

    async def asyncSetUp(self):
        await db.setup()

    # ── topic parsing ────────────────────────────────────────────────────
    def test_topic_records_author_claimer_and_category(self):
        channel = SimpleNamespace(topic="Author: 111 | Category: report | Claimed: 222")
        self.assertEqual(
            tickets.ticket_topic_info(channel),
            {"author_id": 111, "claimed_by": 222, "category": "report"},
        )

    def test_topic_survives_junk_and_missing_parts(self):
        self.assertEqual(
            tickets.ticket_topic_info(SimpleNamespace(topic="Author: not-a-number | Category: ")),
            {"author_id": None, "claimed_by": None, "category": "general"},
        )
        self.assertEqual(
            tickets.ticket_topic_info(SimpleNamespace(topic=None)),
            {"author_id": None, "claimed_by": None, "category": "general"},
        )
        # An unclaimed ticket simply has no Claimed part.
        self.assertEqual(
            tickets.ticket_topic_info(SimpleNamespace(topic="Author: 5 | Category: general")),
            {"author_id": 5, "claimed_by": None, "category": "general"},
        )

    # ── the prompt itself ────────────────────────────────────────────────
    def test_star_bar_and_clamp(self):
        self.assertEqual(tickets.star_bar(5), "★★★★★")
        self.assertEqual(tickets.star_bar(1), "★☆☆☆☆")
        self.assertEqual(tickets.clamp_score(9), 5)
        self.assertEqual(tickets.clamp_score(0), 1)
        self.assertEqual(tickets.clamp_score("nonsense"), 1)
        self.assertEqual(tickets.clamp_score(None), 1)

    def test_prompt_has_five_unique_buttons_inside_discords_limit(self):
        panel = tickets.feedback_prompt(TICKET_CHANNEL_ID, timeout=None)
        buttons = [b for row in panel.container.children for b in getattr(row, "children", [])]
        self.assertEqual(len(buttons), 5)
        ids = [b.custom_id for b in buttons]
        self.assertEqual(len(set(ids)), 5, "custom_ids must be unique within a view")
        self.assertTrue(all(len(cid) <= 100 for cid in ids), "Discord caps custom_id at 100")
        # Every button must carry the ticket, so a DM copy resolves the same row.
        self.assertTrue(all(str(TICKET_CHANNEL_ID) in cid for cid in ids))
        self.assertTrue(all(b.callback is not None for b in buttons))

    async def test_tapping_a_star_opens_the_comment_modal(self):
        panel = tickets.feedback_prompt(TICKET_CHANNEL_ID, timeout=None)
        buttons = [b for row in panel.container.children for b in getattr(row, "children", [])]
        button = next(b for b in buttons if b.custom_id.endswith(":4"))
        await db.open_ticket_feedback(GUILD_ID, TICKET_CHANNEL_ID, AUTHOR_ID, STAFF_ID, "general")
        interaction = _Interaction(_Client(), user=_User(AUTHOR_ID))

        await button.callback(interaction)

        modal = interaction.response.modal
        self.assertEqual(modal.channel_id, TICKET_CHANNEL_ID)
        self.assertEqual(modal.score, 4, "each button remembers its own star")
        self.assertIn("4/5", modal.title)

    async def test_only_the_person_who_opened_the_ticket_may_rate(self):
        """The prompt sits where the handler is standing; it is not theirs to answer.

        Whoever claimed a ticket is usually the one who closes it, so an
        unguarded prompt invited staff to mark their own work — which is what the
        handler was being shown, and did.
        """
        panel = tickets.feedback_prompt(TICKET_CHANNEL_ID, timeout=None)
        buttons = [b for row in panel.container.children for b in getattr(row, "children", [])]
        button = next(b for b in buttons if b.custom_id.endswith(":5"))
        await db.open_ticket_feedback(GUILD_ID, TICKET_CHANNEL_ID, AUTHOR_ID, STAFF_ID, "general")

        handler = _Interaction(_Client(), user=_User(STAFF_ID, manage_channels=True))
        await button.callback(handler)
        self.assertIsNone(handler.response.modal, "the handler is not asked to rate")
        self.assertIn("opened", handler.response.messages[0]["content"])

        opener = _Interaction(_Client(), user=_User(AUTHOR_ID))
        await button.callback(opener)
        self.assertIsNotNone(opener.response.modal, "the opener still is")

    async def test_a_rating_submitted_by_someone_else_is_not_recorded(self):
        """The same rule where the rating is written, not just where it is asked."""
        _, log_channel, _, client = await self._close_ticket()
        handler = _Interaction(client, guild=client.get_guild(GUILD_ID))

        await tickets.submit_ticket_feedback(
            handler, channel_id=TICKET_CHANNEL_ID, score=5, comment="marking my own work"
        )

        row = await db.get_ticket_feedback(TICKET_CHANNEL_ID)
        self.assertIsNone(row["rating"], "nothing is written for a non-opener")
        self.assertEqual(log_channel.sent, [], "and nothing is logged")
        self.assertIn("opened", handler.response.messages[0]["content"])

    # ── claim bookkeeping ────────────────────────────────────────────────
    async def test_claiming_records_the_claimer_in_the_topic(self):
        ticket_channel = _Channel(TICKET_CHANNEL_ID, "ticket-support-raj")
        ticket_channel.topic = f"Author: {AUTHOR_ID} | Category: general"
        guild = _Guild([ticket_channel])
        staff = _User(STAFF_ID, manage_channels=True)
        interaction = _Interaction(_Client(guild), guild=guild, channel=ticket_channel, user=staff)

        await tickets.TicketControlsView().claim_ticket.callback(interaction)

        info = tickets.ticket_topic_info(ticket_channel)
        self.assertEqual(info["claimed_by"], STAFF_ID)
        self.assertEqual(info["author_id"], AUTHOR_ID)
        self.assertTrue(interaction.response.messages, "the claim must still answer")

    async def test_claiming_twice_does_not_duplicate_the_topic(self):
        ticket_channel = _Channel(TICKET_CHANNEL_ID, "ticket-support-raj")
        ticket_channel.topic = f"Author: {AUTHOR_ID} | Category: general | Claimed: {STAFF_ID}"
        guild = _Guild([ticket_channel])
        interaction = _Interaction(
            _Client(guild), guild=guild, channel=ticket_channel,
            user=_User(STAFF_ID, manage_channels=True),
        )

        await tickets.TicketControlsView().claim_ticket.callback(interaction)

        self.assertEqual(ticket_channel.topic.count("Claimed:"), 1)

    # ── the close → rate → log loop ──────────────────────────────────────
    async def _close_ticket(self, *, opener_id=AUTHOR_ID, closer=None, dms_open=True):
        ticket_channel = _Channel(TICKET_CHANNEL_ID, "ticket-support-raj")
        ticket_channel.topic = f"Author: {opener_id} | Category: report | Claimed: {STAFF_ID}"
        log_channel = _Channel(LOG_CHANNEL_ID, "logs")
        guild = _Guild([ticket_channel, log_channel])
        opener = _User(opener_id, "opener")
        opener.dm_error = None if dms_open else __import__("discord").Forbidden(
            SimpleNamespace(status=403, reason="Cannot send messages to this user"), "no dms"
        )
        guild._members[opener_id] = opener
        client = _Client(guild)
        client.users[opener_id] = opener
        closer = closer or _User(STAFF_ID, manage_channels=True)
        interaction = _Interaction(client, guild=guild, channel=ticket_channel, user=closer)

        await db.set_setting(GUILD_ID, "log_channel_id", LOG_CHANNEL_ID)
        await tickets.TicketControlsView().close_ticket.callback(interaction)
        return ticket_channel, log_channel, opener, client

    async def test_closing_asks_in_the_channel_and_by_dm(self):
        ticket_channel, _, opener, client = await self._close_ticket()

        # The prompt lands in the ticket (staff can see what was asked)...
        self.assertIn("How did we do?", layout_text(ticket_channel.sent[-1]["view"]))
        # ...and in the opener's DMs, which outlive the channel.
        self.assertEqual(len(opener.dms), 1)
        self.assertIn("How did we do?", layout_text(opener.dms[0]["view"]))
        self.assertIn(TICKET_CHANNEL_ID, tickets._FEEDBACK_EVENTS)

    async def test_the_opener_closing_their_own_ticket_gets_no_dm(self):
        ticket_channel, _, opener, _ = await self._close_ticket(
            closer=_User(AUTHOR_ID, manage_channels=True)
        )
        self.assertIn("How did we do?", layout_text(ticket_channel.sent[-1]["view"]))
        self.assertEqual(opener.dms, [], "no need to DM someone staring at the prompt")

    async def test_a_rating_logs_both_people_and_opens_a_thread(self):
        ticket_channel, log_channel, _, client = await self._close_ticket()

        await tickets.submit_ticket_feedback(
            _Interaction(client, guild=client.get_guild(GUILD_ID), user=_User(AUTHOR_ID)),
            channel_id=TICKET_CHANNEL_ID,
            score=5,
            comment="Fast and friendly",
        )
        await asyncio.gather(*list(tickets._FEEDBACK_TASKS))

        self.assertEqual(len(log_channel.sent), 1)
        logged = log_channel.sent[0]
        text = layout_text(logged["view"]) + "\n" + layout_text(logged.get("embeds") or "")
        self.assertIn(f"<@{AUTHOR_ID}>", text)
        self.assertIn(f"<@{STAFF_ID}>", text)
        self.assertIn("Fast and friendly", text)
        self.assertIn("5/5", text)
        # Both people are pingable, and nobody else.
        allowed = logged["allowed_mentions"]
        self.assertEqual({u.id for u in allowed.users}, {AUTHOR_ID, STAFF_ID})
        self.assertFalse(allowed.roles)
        self.assertFalse(allowed.everyone)
        # A thread so staff can follow up on it.
        self.assertEqual(log_channel.messages[0].threads, ["Feedback ★★★★★"])
        # And the ticket channel is gone the moment the rating lands.
        self.assertTrue(ticket_channel.deleted)
        self.assertNotIn(TICKET_CHANNEL_ID, tickets._FEEDBACK_EVENTS)

    async def test_a_second_rating_is_refused_and_does_not_double_log(self):
        _, log_channel, _, client = await self._close_ticket()
        rating_interaction = _Interaction(
            client, guild=client.get_guild(GUILD_ID), user=_User(AUTHOR_ID)
        )

        await tickets.submit_ticket_feedback(
            rating_interaction, channel_id=TICKET_CHANNEL_ID, score=2, comment="slow"
        )
        second = _Interaction(client, guild=client.get_guild(GUILD_ID), user=_User(AUTHOR_ID))
        await tickets.submit_ticket_feedback(
            second, channel_id=TICKET_CHANNEL_ID, score=5, comment="changed my mind"
        )

        self.assertEqual(len(log_channel.sent), 1, "one ticket means one log post")
        self.assertIn("already rated", second.response.messages[0]["content"])

    async def test_rating_from_the_dm_after_the_channel_is_gone_still_logs(self):
        ticket_channel, log_channel, _, client = await self._close_ticket()
        # The grace period elapses without a rating: the channel goes away.
        with mock.patch.object(tickets, "FEEDBACK_GRACE_SECONDS", 0.01):
            await tickets._close_when_rated(client, TICKET_CHANNEL_ID, asyncio.Event())

        self.assertTrue(ticket_channel.deleted)
        log_channel.sent.clear()

        await tickets.submit_ticket_feedback(
            _Interaction(client, user=_User(AUTHOR_ID)),
            channel_id=TICKET_CHANNEL_ID,
            score=3,
            comment="ok",
        )

        self.assertEqual(len(log_channel.sent), 1)
        self.assertTrue(
            log_channel.sent[0].get("allowed_mentions"),
            "the log post still tags the two people involved",
        )

    async def test_the_comment_modal_is_the_path_that_records_the_rating(self):
        _, log_channel, _, client = await self._close_ticket()
        modal = tickets.TicketFeedbackModal(channel_id=TICKET_CHANNEL_ID, score=4)
        modal.comment._value = "nicely done"

        await modal.on_submit(
            _Interaction(client, guild=client.get_guild(GUILD_ID), user=_User(AUTHOR_ID))
        )

        row = await db.get_ticket_feedback(TICKET_CHANNEL_ID)
        self.assertEqual(row["rating"], 4)
        self.assertEqual(row["comment"], "nicely done")
        self.assertEqual(len(log_channel.sent), 1)

    async def test_a_rating_survives_a_channel_that_is_already_gone(self):
        ticket_channel, _, _, client = await self._close_ticket()
        del client.get_guild(GUILD_ID)._channels[TICKET_CHANNEL_ID]

        await tickets.submit_ticket_feedback(
            _Interaction(client, guild=client.get_guild(GUILD_ID), user=_User(AUTHOR_ID)),
            channel_id=TICKET_CHANNEL_ID,
            score=4,
            comment="",
        )

        self.assertFalse(ticket_channel.deleted, "there was nothing left to delete")

    async def test_unknown_ticket_is_reported_not_crashed(self):
        client = _Client(_Guild())
        interaction = _Interaction(client, guild=client.get_guild(GUILD_ID))
        await tickets.submit_ticket_feedback(
            interaction, channel_id=999999, score=5, comment=""
        )
        self.assertIn("lost track", interaction.response.messages[0]["content"])

    async def test_feedback_is_kept_when_there_is_no_log_channel(self):
        ticket_channel = _Channel(TICKET_CHANNEL_ID, "ticket-support-raj")
        ticket_channel.topic = f"Author: {AUTHOR_ID} | Category: bug | Claimed: {STAFF_ID}"
        guild = _Guild([ticket_channel])
        client = _Client(guild)
        interaction = _Interaction(client, guild=guild, user=_User(AUTHOR_ID))

        await db.open_ticket_feedback(GUILD_ID, TICKET_CHANNEL_ID, AUTHOR_ID, STAFF_ID, "bug")
        await tickets.submit_ticket_feedback(
            interaction, channel_id=TICKET_CHANNEL_ID, score=1, comment="bad"
        )

        row = await db.get_ticket_feedback(TICKET_CHANNEL_ID)
        self.assertEqual(row["rating"], 1, "the rating is stored even when it cannot be posted")
        self.assertIn("Thanks", interaction.response.messages[0]["content"])

    # ── closing without an opener ────────────────────────────────────────
    async def test_a_channel_with_no_author_closes_the_old_way(self):
        channel = _Channel(TICKET_CHANNEL_ID, "ticket-not-really")
        guild = _Guild([channel])
        interaction = _Interaction(_Client(guild), guild=guild, channel=channel)

        with mock.patch.object(tickets.discord.utils, "sleep_until", mock.AsyncMock()):
            await tickets.TicketControlsView().close_ticket.callback(interaction)

        self.assertTrue(channel.deleted)
        self.assertNotIn(TICKET_CHANNEL_ID, tickets._FEEDBACK_EVENTS)

    async def test_a_refused_delete_is_reported(self):
        channel = _Channel(TICKET_CHANNEL_ID, "ticket-support-raj")
        channel.delete_error = __import__("discord").Forbidden(
            SimpleNamespace(status=403, reason="Missing Permissions"), "nope"
        )
        channel.sent.clear()
        await tickets._close_ticket_channel(channel)
        self.assertTrue(
            any("could not delete" in str(payload.get("content", "")) for payload in channel.sent)
        )

    # ── database guards ──────────────────────────────────────────────────
    async def test_reopening_refreshes_the_details_but_never_a_finished_rating(self):
        await db.open_ticket_feedback(GUILD_ID, TICKET_CHANNEL_ID, AUTHOR_ID, None, "general")
        # A second close before anyone rated (the first delete failed) picks up
        # whoever claimed the ticket in the meantime.
        await db.open_ticket_feedback(GUILD_ID, TICKET_CHANNEL_ID, AUTHOR_ID, STAFF_ID, "report")
        row = await db.get_ticket_feedback(TICKET_CHANNEL_ID)
        self.assertEqual(row["claimed_by"], STAFF_ID)
        self.assertEqual(row["category"], "report")
        self.assertIsNone(row["rating"])

        # Once rated, the row is closed for edits: one ticket, one rating.
        self.assertTrue(await db.record_ticket_feedback(TICKET_CHANNEL_ID, 5, "great"))
        self.assertFalse(await db.record_ticket_feedback(TICKET_CHANNEL_ID, 1, "again"))
        await db.open_ticket_feedback(GUILD_ID, TICKET_CHANNEL_ID, AUTHOR_ID, None, "general")
        row = await db.get_ticket_feedback(TICKET_CHANNEL_ID)
        self.assertEqual((row["rating"], row["comment"]), (5, "great"))
        self.assertEqual(row["claimed_by"], STAFF_ID)


if __name__ == "__main__":
    unittest.main()
