"""The meetings feature, held by the rules the rest of the suite holds.

Three things this file exists to pin, each a way the feature would fail *quietly*
otherwise — and a quiet failure is the worst kind here, because a meeting that
was never announced and a meeting whose reminders never fired both leave an
empty channel and no error anywhere:

* **The time somebody typed is the time that fires.** Every accepted shape of
  ``when`` is asserted against a fixed clock, including the traps: a bare clock
  rolls to tomorrow rather than into the past, ``2026-10-03`` keeps its ``-03``
  instead of losing three hours to a sign it was only spelling a day with, and
  an offset moves the wall clock rather than the meeting.
* **The receipt tells the truth about the queue.** ``/meeting create`` shows
  the notice rows that were actually written, not the schedule that was asked
  for — a notice already in the past by booking time is never created, and a
  card promising it would be a promise nobody keeps.
* **A button pressed tomorrow still finds its meeting.** The reply buttons are
  matched by their fixed custom ids and the card's message id, never by any
  state the process that sent them is holding.
"""
import asyncio
import calendar
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import discord
from discord.ext import commands

import cogs.meetings as meetings_cog
from utils import db, features, meetings, permissions, style

# A guild id used nowhere else, so these rows cannot be mistaken for another
# test's fixtures (the incident ``tests/__init__.py`` exists to prevent).
GUILD_ID = 910_017
CHANNEL_ID = 910_018

#: 2026-09-29 12:00:00 UTC — a Tuesday, which the weekday tests depend on.
NOW = calendar.timegm((2026, 9, 29, 12, 0, 0))


def _utc(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


class ParsingWhen(unittest.TestCase):
    """Every accepted shape of ``when``, against a fixed clock."""

    def test_relative(self):
        ts, error = meetings.parse_when("in 2 hours", NOW)
        self.assertIsNone(error)
        self.assertEqual(ts, NOW + 7200)

    def test_named_day_with_a_clock(self):
        ts, error = meetings.parse_when("tomorrow 18:00", NOW)
        self.assertIsNone(error)
        self.assertEqual(_utc(ts), datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc))

    def test_today_stays_today(self):
        ts, error = meetings.parse_when("today 6pm", NOW)
        self.assertIsNone(error)
        self.assertEqual(_utc(ts), datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc))

    def test_iso_date(self):
        ts, error = meetings.parse_when("2026-10-03 18:00", NOW)
        self.assertIsNone(error)
        self.assertEqual(_utc(ts), datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc))

    def test_a_date_is_never_read_as_an_offset(self):
        """``2026-10-03`` ends in ``-03`` — and -03 is a day, not three hours.

        The offset strip runs on the *time* half only, and this is the input
        that proves it: a bare ISO date that lost three hours would meet the
        horizon checks happily and fire at the wrong moment forever.
        """
        ts, error = meetings.parse_when("2026-10-03 18:00", NOW)
        self.assertIsNone(error)
        self.assertEqual(_utc(ts).hour, 18)

    def test_offset_moves_the_wall_clock_not_the_meeting(self):
        ahead, error = meetings.parse_when("18:00+2", NOW)
        self.assertIsNone(error)
        self.assertEqual(_utc(ahead), datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc))
        behind, error = meetings.parse_when("6pm-5", NOW)
        self.assertIsNone(error)
        self.assertEqual(_utc(behind), datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc))

    def test_weekday_is_the_next_one(self):
        ts, error = meetings.parse_when("friday 19:00", NOW)
        self.assertIsNone(error)
        self.assertEqual(_utc(ts), datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(_utc(ts).weekday(), 4)

    def test_a_bare_clock_that_has_passed_rolls_to_tomorrow(self):
        ts, error = meetings.parse_when("00:10", NOW)
        self.assertIsNone(error)
        self.assertEqual(_utc(ts), datetime(2026, 9, 30, 0, 10, tzinfo=timezone.utc))

    def test_a_bare_clock_ahead_stays_today(self):
        ts, error = meetings.parse_when("18:00", NOW)
        self.assertIsNone(error)
        self.assertEqual(_utc(ts), datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc))

    def test_the_horizon_is_enforced_both_ways(self):
        _ts, error = meetings.parse_when("2026-09-29 12:00", NOW)
        self.assertIn("already passed", error or "")
        _ts, error = meetings.parse_when("in 9000 hours", NOW)
        self.assertIn("more than a year", error or "")

    def test_a_day_without_a_time_is_refused_with_an_example(self):
        for text in ("tomorrow", "friday", "2026-10-03"):
            _ts, error = meetings.parse_when(text, NOW)
            self.assertIsNotNone(error, text)
            self.assertIn("18:00", error)

    def test_junk_is_refused_with_the_hint(self):
        _ts, error = meetings.parse_when("banana", NOW)
        self.assertIn(meetings.WHEN_HINT, error or "")
        _ts, error = meetings.parse_when("in 2 moons", NOW)
        self.assertIn("moons", error or "")


class ParsingNotices(unittest.TestCase):
    """``1h,15m`` and every way of getting it wrong."""

    def test_the_default_shape(self):
        self.assertEqual(meetings.parse_notices("1h,15m"), ([3600, 900], None))

    def test_units_words_semicolons_and_spaces(self):
        self.assertEqual(meetings.parse_notices("1 day; 30 minutes"), ([86400, 1800], None))
        self.assertEqual(meetings.parse_notices("2 hours 15m"), ([7200, 900], None))

    def test_a_bare_number_is_minutes(self):
        self.assertEqual(meetings.parse_notices("99"), ([5940], None))

    def test_zero_m_means_when_it_starts(self):
        self.assertEqual(meetings.parse_notices("0m"), ([0], None))

    def test_none_means_none(self):
        for word in ("none", "off", "never"):
            self.assertEqual(meetings.parse_notices(word), ([], None))

    def test_duplicates_collapse_and_order_is_largest_first(self):
        self.assertEqual(meetings.parse_notices("15m,1h,15m"), ([3600, 900], None))

    def test_a_bad_unit_names_the_unit_and_offers_the_format(self):
        _offsets, error = meetings.parse_notices("soon")
        self.assertIn(meetings.NOTICES_HINT, error or "")
        _offsets, error = meetings.parse_notices("2 lightyears")
        self.assertIn("lightyears", error or "")

    def test_the_caps(self):
        _offsets, error = meetings.parse_notices("40d")
        self.assertIn("30 days", error or "")
        _offsets, error = meetings.parse_notices("1m,2m,3m,4m,5m,6m")
        self.assertIn(str(meetings.MAX_NOTICES), error or "")

    def test_labels_and_sentences(self):
        self.assertEqual(meetings.notice_label(0), "at the start")
        self.assertEqual(meetings.notice_label(3600), "1 hour")
        self.assertEqual(meetings.notice_label(5400), "1 hour 30 minutes")
        self.assertEqual(meetings.notice_label(14 * 86400), "2 weeks")
        self.assertEqual(
            meetings.notices_sentence([3600, 900]), "1 hour before, 15 minutes before"
        )
        self.assertEqual(meetings.notices_sentence([]), "None — the announcement is the only notice.")

    def test_due_times_drop_what_has_already_passed(self):
        starts = NOW + 1800
        self.assertEqual(meetings.due_times(starts, [3600, 900], NOW), [starts - 900])
        self.assertEqual(meetings.due_times(starts, [3600], NOW), [])


class Storage(unittest.TestCase):
    """The rows: written once, read back the same, and never half of either."""

    def tearDown(self):
        conn = db.connect()
        try:
            conn.execute("DELETE FROM meetings WHERE guild_id = ?", (GUILD_ID,))
            conn.commit()
        finally:
            conn.close()

    def _meeting(self, *, starts_in=5 * 3600, notices=(3600, 900), **kw):
        args = dict(
            title="Sprint planning",
            starts_at=NOW + starts_in,
            channel_id=CHANNEL_ID,
            created_by=7,
            created_by_name="raj",
            agenda="Q4 roadmap",
            voice_channel_id=99,
            location="https://meet.example/abc",
            audience_role_id=1234,
            notices=list(notices),
            now=NOW,
        )
        args.update(kw)
        return meetings.create(GUILD_ID, **args)

    def test_create_writes_the_meeting_and_its_notices(self):
        meeting_id = self._meeting()
        row = meetings.get(meeting_id)
        self.assertEqual(row["title"], "Sprint planning")
        self.assertEqual(row["status"], meetings.STATUS_SCHEDULED)
        self.assertEqual(row["notices"], [3600, 900])
        rows = meetings.pending_notice_rows(meeting_id)
        self.assertEqual(
            [(r["due_at"], r["offset_seconds"]) for r in rows],
            [(NOW + 5 * 3600 - 3600, 3600), (NOW + 5 * 3600 - 900, 900)],
        )

    def test_a_notice_already_due_at_booking_time_is_not_written(self):
        """The receipt shows the rows, so a row nobody will send must not exist.

        A meeting in ten minutes with a one-hour notice: the hour has passed,
        and scheduling it anyway would either fire immediately or sit there
        looking queued. Neither is what the creator was promised.
        """
        meeting_id = self._meeting(starts_in=600, notices=[3600, 300])
        rows = meetings.pending_notice_rows(meeting_id)
        self.assertEqual([r["offset_seconds"] for r in rows], [300])

    def test_upcoming_is_scoped_to_one_server(self):
        meeting_id = self._meeting()
        self.assertEqual([r["id"] for r in meetings.upcoming(GUILD_ID)], [meeting_id])
        self.assertEqual(meetings.upcoming(GUILD_ID + 1), [])

    def test_replies_take_the_last_press(self):
        meeting_id = self._meeting()
        self.assertTrue(meetings.set_rsvp(meeting_id, 111, "yes", NOW))
        self.assertTrue(meetings.set_rsvp(meeting_id, 111, "no", NOW + 1))
        self.assertEqual(meetings.tally(meeting_id), {"yes": 0, "maybe": 0, "no": 1})
        self.assertEqual(meetings.rsvp_ids(meeting_id, "no"), [111])
        self.assertEqual(meetings.rsvp_ids(meeting_id, "yes"), [])

    def test_a_refused_answer_is_not_recorded(self):
        meeting_id = self._meeting()
        self.assertFalse(meetings.set_rsvp(meeting_id, 111, "perhaps", NOW))
        self.assertEqual(meetings.tally(meeting_id), {"yes": 0, "maybe": 0, "no": 0})

    def test_a_notice_is_claimed_exactly_once(self):
        meeting_id = self._meeting()
        notice_id = meetings.pending_notice_rows(meeting_id)[0]["id"]
        self.assertTrue(meetings.claim_notice(notice_id, NOW))
        self.assertFalse(meetings.claim_notice(notice_id, NOW))
        meetings.mark_notice(notice_id, meetings.NOTICE_SENT, "", NOW)
        self.assertEqual(meetings.notice_rows(meeting_id)[0]["status"], meetings.NOTICE_SENT)

    def test_a_claim_interrupted_by_a_restart_goes_back_in_the_queue(self):
        """The opposite of the scheduler's rule, on purpose.

        A duplicate *announcement* is worse than a missing one, so
        ``utils/scheduled`` settles a stale claim as failed. A duplicate
        *reminder* is a nuisance; a missing one is the feature not happening.
        """
        meeting_id = self._meeting()
        notice_id = meetings.pending_notice_rows(meeting_id)[0]["id"]
        self.assertTrue(meetings.claim_notice(notice_id, NOW))
        self.assertEqual(meetings.notice_rows(meeting_id)[0]["status"], meetings.NOTICE_SENDING)
        # Inside the window it stays claimed — two sweeps must not both send it.
        self.assertEqual(meetings.reclaim_stale(NOW + meetings.STALE_CLAIM_SECONDS - 1), [])
        self.assertEqual(
            meetings.notice_rows(meeting_id)[0]["status"], meetings.NOTICE_SENDING
        )
        # Past the window the claim is read as a process that died mid-send,
        # and the notice goes back to pending rather than being lost.
        stale = meetings.reclaim_stale(NOW + meetings.STALE_CLAIM_SECONDS + 1)
        self.assertEqual(stale, [notice_id])
        self.assertEqual(meetings.notice_rows(meeting_id)[0]["status"], meetings.NOTICE_PENDING)

    def test_cancel_stands_the_queue_down_and_is_idempotent(self):
        meeting_id = self._meeting()
        self.assertTrue(meetings.cancel(meeting_id, NOW))
        self.assertEqual(meetings.get(meeting_id)["status"], meetings.STATUS_CANCELLED)
        self.assertEqual(
            {r["status"] for r in meetings.notice_rows(meeting_id)},
            {meetings.NOTICE_CANCELLED},
        )
        self.assertFalse(meetings.cancel(meeting_id, NOW))
        self.assertEqual(meetings.upcoming(GUILD_ID), [])

    def test_reschedule_rebuilds_the_queue_for_the_new_start(self):
        meeting_id = self._meeting()
        new_start = NOW + 8 * 3600
        self.assertTrue(meetings.reschedule(meeting_id, new_start, [3600], NOW))
        rows = meetings.pending_notice_rows(meeting_id)
        self.assertEqual([(r["due_at"], r["offset_seconds"]) for r in rows], [(new_start - 3600, 3600)])
        self.assertEqual(meetings.get(meeting_id)["starts_at"], new_start)

    def test_a_cancelled_meeting_cannot_be_moved(self):
        meeting_id = self._meeting()
        self.assertTrue(meetings.cancel(meeting_id, NOW))
        self.assertFalse(meetings.reschedule(meeting_id, NOW + 8 * 3600, [], NOW))

    def test_a_card_finds_its_meeting_and_a_stranger_does_not(self):
        meeting_id = self._meeting()
        meetings.set_message_id(meeting_id, 424242)
        self.assertEqual(meetings.for_message(424242)["id"], meeting_id)
        self.assertIsNone(meetings.for_message(1))

    def test_prune_forgets_the_finished_and_keeps_the_live(self):
        old = self._meeting(starts_in=-meetings.HISTORY_DAYS * 86400 - 86400, notices=())
        live = self._meeting()
        self.assertGreaterEqual(meetings.prune(NOW), 1)
        self.assertIsNone(meetings.get(old))
        self.assertIsNotNone(meetings.get(live))


class CardText(unittest.TestCase):
    """The words on the cards — the whole visual surface, asserted as strings."""

    def _meeting(self, **kw):
        row = {
            "id": 12,
            "guild_id": GUILD_ID,
            "title": "Sprint planning",
            "agenda": "Q4 roadmap",
            "starts_at": NOW + 5 * 3600,
            "channel_id": CHANNEL_ID,
            "voice_channel_id": 99,
            "location": "https://meet.example/abc",
            "audience_role_id": 1234,
            "notices": [3600, 900],
            "status": meetings.STATUS_SCHEDULED,
            "announce_message_id": 424242,
            "created_by": 7,
            "created_by_name": "raj",
        }
        row.update(kw)
        return row

    def test_the_announcement_carries_every_part_of_the_promise(self):
        title, body, fields, footer = meetings.announcement_text(
            self._meeting(), {"yes": 2, "maybe": 1, "no": 0}
        )
        self.assertEqual(title, "📅 Sprint planning")
        self.assertIn("button", body)
        values = dict(fields)
        self.assertIn(f"<t:{NOW + 5 * 3600}:F>", values["When"])
        self.assertIn(f"<t:{NOW + 5 * 3600}:R>", values["When"])
        self.assertEqual(values["Place"], "<#99>\nhttps://meet.example/abc")
        self.assertEqual(values["Who"], "<@&1234>")
        self.assertEqual(values["Agenda"], "Q4 roadmap")
        self.assertEqual(values["Notices"], "1 hour before, 15 minutes before")
        self.assertEqual(values["Replies"], "✅ 2 · ❓ 1 · ❌ 0")
        self.assertEqual(footer, "Meeting #12 · arranged by raj")

    def test_a_card_with_no_place_and_no_role_omits_both_rather_than_saying_none(self):
        _title, _body, fields, _footer = meetings.announcement_text(
            self._meeting(voice_channel_id=None, location="", audience_role_id=None), None
        )
        names = [name for name, _value in fields]
        self.assertNotIn("Place", names)
        self.assertNotIn("Who", names)

    def test_the_cancelled_card_gives_a_reason_and_says_the_queue_is_down(self):
        _title, body, fields, _footer = meetings.announcement_text(
            self._meeting(), {"yes": 1, "maybe": 0, "no": 0},
            cancelled=True, reason="moved away",
        )
        self.assertIn("Cancelled", body)
        self.assertIn("moved away", body)
        self.assertIn("stood down", body)
        self.assertNotIn("Tap a button", body)

    def test_the_reminder_headlines_tell_the_three_apart(self):
        _t, body, _f, footer = meetings.reminder_text(self._meeting(), 3600)
        self.assertEqual(meetings.reminder_text(self._meeting(), 3600)[0], "⏰ Meeting reminder")
        self.assertEqual(meetings.reminder_text(self._meeting(), 0)[0], "▶️ Starting now")
        self.assertEqual(meetings.reminder_text(self._meeting(), 0, manual=True)[0], "📣 Meeting notice")
        self.assertIn(f"<t:{NOW + 5 * 3600}:R>", body)
        self.assertEqual(footer, "Meeting #12 · 1 hour before")

    def test_the_receipt_shows_the_rows_written_not_the_schedule_asked_for(self):
        """The law from the module docstring, as a card.

        Booking ten minutes out with an hour's notice writes *one* row, and
        the receipt must show that one — a card listing the hour would be
        promising a message the queue will never send.
        """
        row = self._meeting(starts_at=NOW + 600, notices=[3600, 300])
        rows = [{"due_at": NOW + 300, "offset_seconds": 300}]
        _title, _body, fields, _footer = meetings.receipt_text(
            row, rows, jump_url="https://discord.com/channels/1/2/3"
        )
        values = dict(fields)
        self.assertIn(f"<t:{NOW + 300}:F>", values["Notices going out"])
        self.assertIn("5 minutes before", values["Notices going out"])
        self.assertNotIn("1 hour", values["Notices going out"])
        self.assertIn("Open the announcement card", _body)

        _title, _body, fields, _footer = meetings.receipt_text(row, [])
        self.assertIn("None", dict(fields)["Notices going out"])

    def test_the_detail_card_reports_each_notices_fate(self):
        row = self._meeting(status=meetings.STATUS_CANCELLED)
        _title, body, fields, _footer = meetings.detail_text(
            row,
            notice_rows=[
                {"due_at": NOW, "offset_seconds": 3600,
                 "status": meetings.NOTICE_MISSED, "note": "the notice channel is gone"},
                {"due_at": NOW + 1, "offset_seconds": 900,
                 "status": meetings.NOTICE_SENT, "note": ""},
            ],
            tally={"yes": 1, "maybe": 0, "no": 0},
            names={"yes": ["alice"], "maybe": [], "no": []},
            jump_url="https://discord.com/channels/1/2/3",
        )
        self.assertIn("cancelled", body)
        values = dict(fields)
        self.assertIn("missed (the notice channel is gone)", values["Notices"])
        self.assertIn("sent", values["Notices"])
        self.assertIn("✅ Going (1) — alice", values["Replies"])
        self.assertIn("❓ Maybe — none yet", values["Replies"])

    def test_the_list_prints_the_number_the_next_command_asks_for(self):
        title, body, _fields, _footer = meetings.list_text(
            [self._meeting()], "Test Server"
        )
        self.assertIn("Test Server", title)
        self.assertIn("#12", body)
        self.assertIn(f"<t:{NOW + 5 * 3600}:F>", body)
        _title, body, _fields, _footer = meetings.list_text([], "")
        self.assertIn("/meeting create", body)

    def test_a_moved_card_names_the_new_start_and_the_new_queue(self):
        _title, body, fields, _footer = meetings.moved_text(
            self._meeting(starts_at=NOW + 8 * 3600),
            notice_rows=[{"due_at": NOW + 7 * 3600, "offset_seconds": 3600}],
            jump_url="https://discord.com/channels/1/2/3",
        )
        self.assertIn("now starts", body)
        self.assertIn(f"<t:{NOW + 8 * 3600}:F>", body)
        self.assertIn("1 hour before", dict(fields)["Notices going out"])

    def test_offsets_are_read_from_either_shape_they_arrive_in(self):
        """The bug this law was written for, watched to fail.

        ``get()`` decodes the JSON into a list; the first version of
        ``_safe_json_offsets`` called ``json.loads`` on that list, got a
        ``TypeError`` and answered *no notices* — silently, on the card a
        server's staff reads.
        """
        self.assertEqual(meetings.offsets_of({"notices": [3600, 900]}), [3600, 900])
        self.assertEqual(meetings.offsets_of({"notices": "[3600, 900]"}), [3600, 900])
        self.assertEqual(meetings.offsets_of({"notices": "not json"}), [])
        self.assertEqual(meetings.offsets_of({}), [])


class TheCog(unittest.IsolatedAsyncioTestCase):
    """The command group, the buttons, and the one who may ping whom."""

    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.failed: list[str] = []
        try:
            await self.bot.load_extension("cogs.meetings")
        except Exception as exc:  # pragma: no cover — reported as a failure
            self.failed.append(f"{exc!r}")

    async def asyncTearDown(self):
        try:
            await self.bot.close()
        except Exception:
            pass

    def test_the_cog_loads(self):
        self.assertEqual(self.failed, [])

    def test_the_six_verbs_live_under_one_group(self):
        """One group is one slot: Discord allows a hundred top-level commands
        and this bot is at ninety-nine of them."""
        group = next(
            (c for c in self.bot.tree.get_commands() if c.name == "meeting"), None
        )
        self.assertIsNotNone(group)
        self.assertEqual(
            {c.name for c in group.commands},
            {"create", "list", "view", "reschedule", "ping", "cancel"},
        )

    def test_the_registry_describes_exactly_this_group(self):
        feature = features.get("meetings")
        self.assertEqual(feature.gate_commands, ("meeting",))
        self.assertEqual(
            {permissions.COMMAND_ACCESS[f"meeting {name}"] for name in
             ("create", "reschedule", "ping", "cancel")},
            {permissions.GUILD},
        )
        self.assertEqual(
            {permissions.COMMAND_ACCESS[f"meeting {name}"] for name in ("list", "view")},
            {permissions.EVERYONE},
        )

    def test_every_declared_command_is_one_the_tree_holds(self):
        from cogs.help import HELP_MODULES

        self.assertIn("meetings", HELP_MODULES)
        self.assertIn("meetings", features.HELP_KEYS)
        for _name, note in features.feature_entries(features.get("meetings")):
            self.assertTrue(note, "a command row with no note reads as unfinished")

    def test_the_reply_buttons_keep_their_ids_and_their_callback(self):
        """What makes a card pressed tomorrow still answer after a redeploy.

        The ids are the whole mechanism — discord.py dispatches by custom_id
        when the process that sent the message is gone — and a button with no
        callback is a button that silently does nothing.
        """
        from cogs.meetings import RsvpView, BUTTON_PREFIX

        view = RsvpView()
        self.assertEqual(
            [item.custom_id for item in view.children],
            [f"{BUTTON_PREFIX}yes", f"{BUTTON_PREFIX}maybe", f"{BUTTON_PREFIX}no"],
        )
        for item in view.children:
            self.assertIsNotNone(item.callback)

    def test_the_card_pings_the_audience_role_and_nobody_else(self):
        from cogs.meetings import mentions_for

        allowed = mentions_for({"audience_role_id": 42})
        self.assertEqual(list(allowed.roles), [42])
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.users)

        quiet = mentions_for({"audience_role_id": None})
        self.assertFalse(quiet.everyone)
        self.assertFalse(quiet.users)
        self.assertFalse(quiet.roles)

    async def test_a_notice_for_a_server_the_bot_left_says_so(self):
        from cogs.meetings import Meetings

        cog = Meetings(self.bot)
        self.bot.get_guild = lambda gid: None
        sent, reason = await cog._send_notice({"guild_id": 1, "title": "x"})
        self.assertFalse(sent)
        self.assertIn("not in that server", reason)

    async def test_a_notice_with_the_system_switched_off_does_not_go_out(self):
        from cogs.meetings import Meetings

        cog = Meetings(self.bot)
        self.bot.get_guild = lambda gid: SimpleNamespace(id=gid)
        self.bot.is_module_enabled = lambda gid, module: False
        sent, reason = await cog._send_notice({"guild_id": 1, "title": "x"})
        self.assertFalse(sent)
        self.assertIn("switched off", reason)

    async def test_a_notice_with_nowhere_to_land_names_both_absences(self):
        from cogs.meetings import Meetings

        cog = Meetings(self.bot)
        guild = SimpleNamespace(
            id=1,
            get_channel_or_thread=lambda cid: None,
            system_channel=None,
        )
        self.bot.get_guild = lambda gid: guild
        self.bot.is_module_enabled = lambda gid, module: True
        sent, reason = await cog._send_notice(
            {"guild_id": 1, "title": "x", "channel_id": CHANNEL_ID}
        )
        self.assertFalse(sent)
        self.assertIn("no system channel", reason)

    async def test_the_gate_is_the_cogs_and_it_is_async(self):
        """``tests/test_command_scope`` only accepts an async cog-level check as
        evidence that a hidden command would really be refused — a synchronous
        ``interaction_check`` counts as no gate at all."""
        from cogs.meetings import Meetings

        self.assertTrue(asyncio.iscoroutinefunction(Meetings.interaction_check))
        self.bot.is_module_enabled = lambda gid, module: False

        class _Response:
            def __init__(self):
                self.messages = []

            async def send_message(self, content=None, **kwargs):
                self.messages.append({"content": content, **kwargs})

        interaction = SimpleNamespace(guild_id=GUILD_ID, response=_Response())
        cog = Meetings(self.bot)
        self.assertFalse(await cog.interaction_check(interaction))
        self.assertIn("turned off", interaction.response.messages[0]["content"])

        self.bot.is_module_enabled = lambda gid, module: True
        self.assertTrue(await cog.interaction_check(interaction))


class _Gone(Exception):
    """A message that is not there any more — a plain exception, because the
    real one needs an HTTP response object to construct."""


class _FakeResponse:
    def __init__(self):
        self.messages: list[dict] = []
        self.deferred = False
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False, **_kw):
        self._done = True
        self.deferred = True
        self.ephemeral = ephemeral

    async def send_message(self, content=None, **kwargs):
        self._done = True
        self.messages.append({"content": content, **kwargs})


class _FakeFollowup:
    def __init__(self):
        self.messages: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _FakeMessage:
    def __init__(self, guild_id: int, channel_id: int, index: int):
        self.id = 900_000 + index
        self.jump_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{self.id}"
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self


class _FakeChannel:
    def __init__(self, guild_id: int, channel_id: int = CHANNEL_ID):
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.sent: list[tuple[_FakeMessage, dict]] = []
        self._guild_id = guild_id

    def permissions_for(self, _member):
        return SimpleNamespace(send_messages=True)

    async def send(self, content=None, **kwargs):
        message = _FakeMessage(self._guild_id, self.id, len(self.sent) + 1)
        self.sent.append((message, kwargs))
        return message

    async def fetch_message(self, message_id: int):
        for message, _kwargs in self.sent:
            if message.id == message_id:
                return message
        raise _Gone(f"no message {message_id}")


class _FakeGuild:
    def __init__(self, guild_id: int, channel: _FakeChannel):
        self.id = guild_id
        self.name = "Test Server"
        self.me = SimpleNamespace(id=4)
        self.system_channel = None
        self._channel = channel

    def get_channel_or_thread(self, channel_id: int):
        return self._channel if channel_id == self._channel.id else None


def _interaction(guild, channel, bot, *, user_id: int = 5):
    return SimpleNamespace(
        guild=guild,
        guild_id=guild.id,
        channel=channel,
        channel_id=channel.id,
        user=SimpleNamespace(id=user_id, display_name="Raj", name="raj"),
        response=_FakeResponse(),
        followup=_FakeFollowup(),
        client=bot,
        message=None,
    )


def _buttons(view) -> list:
    """Every button on a card, in the order a reader sees them."""
    found: list = []

    def walk(node) -> None:
        for child in getattr(node, "children", []) or []:
            if isinstance(child, discord.ui.Button):
                found.append(child)
            else:
                walk(child)

    walk(view)
    return found


def _text(view) -> str:
    """Every word a card is carrying — the way a reader would see it."""
    words: list[str] = []
    stack = list(getattr(view, "children", []) or [])
    while stack:
        node = stack.pop()
        content = getattr(node, "content", None)
        if isinstance(content, str):
            words.append(content)
        stack.extend(getattr(node, "children", []) or [])
    return "\n".join(words)


class TheWholeLifeOfAMeeting(unittest.IsolatedAsyncioTestCase):
    """Book it, press a button, move it, cancel it — the flow, driven end to end.

    The pieces are each covered above; what this class exists for is the
    seams *between* them, which is where a command feature actually breaks: a
    receipt that references a meeting the rollback dropped, a card edit that
    cannot find its message, a sweep that sends into a channel the command
    never chose. Discord is replaced by four small fakes; everything else — the
    parsing, the rows, the cards, the callbacks — is the shipped code.
    """

    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.bot.is_module_enabled = lambda _gid, _module: True
        self.failed: list[str] = []
        try:
            await self.bot.load_extension("cogs.meetings")
        except Exception as exc:  # pragma: no cover — reported as a failure
            self.failed.append(f"{exc!r}")
        self.cog = self.bot.get_cog("Meetings")
        self.channel = _FakeChannel(GUILD_ID)
        self.guild = _FakeGuild(GUILD_ID, self.channel)
        self.bot.get_guild = lambda gid: self.guild if gid == GUILD_ID else None
        self.interaction = _interaction(self.guild, self.channel, self.bot)
        self.sent: list[tuple[object, object, bool, object]] = []

        sent = self.sent

        async def fake_send_panel(target, view, *, ephemeral=False, allowed_mentions=None):
            """The shipped send path with only the transport replaced.

            ``legal_rows`` still runs, the packets are still recorded in the
            order they were built, and an interaction is answered the way the
            real helper answers one: response first, followup once deferred.
            """
            sent.append((target, view, ephemeral, allowed_mentions))
            style.legal_rows(view)
            if isinstance(target, _FakeChannel):
                return await target.send(view=view, allowed_mentions=allowed_mentions)
            packet = {"view": view, "ephemeral": ephemeral}
            if target.response.is_done():
                await target.followup.send(**packet)
            else:
                await target.response.send_message(**packet)
            return None

        self._patcher = mock.patch.object(
            meetings_cog, "send_panel", new=fake_send_panel
        )
        self._patcher.start()
        self._base_send = fake_send_panel

    async def asyncTearDown(self):
        self._patcher.stop()
        conn = db.connect()
        try:
            conn.execute("DELETE FROM meetings WHERE guild_id = ?", (GUILD_ID,))
            conn.commit()
        finally:
            conn.close()
        try:
            await self.bot.close()
        except Exception:
            pass

    async def _create(self, **overrides) -> dict:
        args = dict(
            title="Sprint planning",
            when="tomorrow 18:00",
            channel=None,
            voice=None,
            role=SimpleNamespace(id=1234),
            where="Room 2",
            agenda="Q4 roadmap",
            notices="1h,15m",
        )
        args.update(overrides)
        await meetings_cog.Meetings.meeting_create.callback(
            self.cog, self.interaction, *args.values()
        )
        rows = meetings.upcoming(GUILD_ID)
        self.assertTrue(rows, f"nothing was booked: {self.interaction.response.messages}")
        return rows[-1]

    async def test_book_press_move_and_cancel(self):
        meeting = await self._create()

        # ── the announcement landed, with its buttons and its audience ──
        self.assertTrue(self.interaction.response.deferred)
        self.assertEqual(len(self.channel.sent), 1)
        message, packet = self.channel.sent[0]
        self.assertEqual(
            [b.custom_id for b in _buttons(packet["view"])],
            ["meeting_rsvp:yes", "meeting_rsvp:maybe", "meeting_rsvp:no"],
        )
        self.assertEqual(list(packet["allowed_mentions"].roles), [1234])
        self.assertEqual(meeting["announce_message_id"], message.id)

        # ── the receipt names the queue that was actually written ──
        receipt = [view for _t, view, eph, _a in self.sent if eph][-1]
        self.assertIn("Meeting scheduled", _text(receipt))
        self.assertIn("Open the announcement card", _text(receipt))
        self.assertIn("Room 2", _text(receipt))

        # ── a member presses "I'm in", and the tally moves ──
        press = _interaction(self.guild, self.channel, self.bot, user_id=77)
        press.message = message
        await _buttons(packet["view"])[0].callback(press)
        self.assertEqual(
            meetings.tally(meeting["id"]), {"yes": 1, "maybe": 0, "no": 0}
        )
        self.assertTrue(message.edits, "the card did not redraw with the new tally")
        self.assertIn("Recorded", press.response.messages[0]["content"])
        self.assertIn("✅ 1", _text(message.edits[-1]["view"]))

        # ── moved, and the card and the queue both follow ──
        before = int(time.time())
        await meetings_cog.Meetings.meeting_reschedule.callback(
            self.cog, self.interaction, meeting["id"], "in 3 hours", None
        )
        after = int(time.time())
        moved = meetings.get(meeting["id"])
        self.assertGreaterEqual(moved["starts_at"], before + 3 * 3600)
        self.assertLessEqual(moved["starts_at"], after + 3 * 3600)
        self.assertGreaterEqual(len(message.edits), 2, "the card was not re-drawn")
        rows = meetings.pending_notice_rows(meeting["id"])
        self.assertTrue(rows)
        self.assertTrue(all(r["due_at"] <= moved["starts_at"] for r in rows))

        # ── cancelled: the queue is stood down and the card says so ──
        await meetings_cog.Meetings.meeting_cancel.callback(
            self.cog, self.interaction, meeting["id"], "moved away"
        )
        self.assertEqual(meetings.get(meeting["id"])["status"], meetings.STATUS_CANCELLED)
        self.assertEqual(
            {r["status"] for r in meetings.notice_rows(meeting["id"])},
            {meetings.NOTICE_CANCELLED},
        )
        self.assertGreaterEqual(len(message.edits), 3)
        self.assertEqual(meetings.upcoming(GUILD_ID), [])

    async def test_a_junk_time_is_refused_before_anything_is_written(self):
        before = set(meetings.upcoming(GUILD_ID))
        await meetings_cog.Meetings.meeting_create.callback(
            self.cog, self.interaction, "Whatever", "banana", None, None, None, None, None, None
        )
        self.assertEqual(set(meetings.upcoming(GUILD_ID)), before)
        self.assertEqual(self.channel.sent, [], "a refused meeting posted a card")
        refusal = self.interaction.response.messages
        self.assertTrue(refusal, "the refusal was never sent")
        text = _text(refusal[0]["view"])
        self.assertIn("couldn't read", text)
        self.assertIn(meetings.WHEN_HINT, text)

    async def test_a_card_that_will_not_post_drops_the_meeting(self):
        """The rollback is the point: a list that promises a card nobody saw.

        Only the *channel* send fails — the refusal and the receipt still go
        through — because that is the real shape of the failure: Discord
        refusing the one call that makes the meeting visible to anybody else.
        """
        interaction = _interaction(self.guild, self.channel, self.bot)

        async def flaky(target, view, **kwargs):
            if isinstance(target, _FakeChannel):
                raise RuntimeError("Discord said no")
            return await self._base_send(target, view, **kwargs)

        with mock.patch.object(meetings_cog, "send_panel", new=flaky):
            await meetings_cog.Meetings.meeting_create.callback(
                self.cog, interaction, "Ghost", "tomorrow 18:00",
                None, None, None, None, None, None,
            )
        self.assertEqual(meetings.upcoming(GUILD_ID), [])
        self.assertIn("dropped the meeting", interaction.followup.messages[-1]["content"])

    async def test_the_sweep_sends_a_due_notice_into_the_meetings_own_channel(self):
        """The loop half of the feature, against the real queue.

        The notice row is aged into the past rather than slept on — what is
        under test is the claim, the card, the ping allowance and the receipt
        the row gets, not the clock.
        """
        meeting = await self._create()
        now = int(time.time())
        conn = db.connect()
        try:
            # One notice aged into the past, not both: what is under test is a
            # reminder going out while the rest of the schedule waits its turn.
            conn.execute(
                "UPDATE meeting_notices SET due_at = ? "
                "WHERE id = (SELECT id FROM meeting_notices WHERE meeting_id = ? "
                "            ORDER BY offset_seconds DESC LIMIT 1)",
                (now - 5, meeting["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        await self.cog._notice_sweep()

        self.assertEqual(len(self.channel.sent), 2, "the reminder was not posted")
        _message, packet = self.channel.sent[-1]
        self.assertEqual(list(packet["allowed_mentions"].roles), [1234])
        statuses = {r["status"] for r in meetings.notice_rows(meeting["id"])}
        self.assertIn(meetings.NOTICE_SENT, statuses)
        # The second notice is still queued for an hour before the start.
        self.assertEqual(
            statuses, {meetings.NOTICE_SENT, meetings.NOTICE_PENDING}
        )

    async def test_a_stale_notice_is_missed_with_a_reason_rather_than_late(self):
        meeting = await self._create()
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE meeting_notices SET due_at = ? WHERE meeting_id = ?",
                (int(time.time()) - meetings.STALE_NOTICE_SECONDS - 60, meeting["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        await self.cog._notice_sweep()

        rows = meetings.notice_rows(meeting["id"])
        stale = [r for r in rows if r["due_at"] < int(time.time()) - meetings.STALE_NOTICE_SECONDS]
        self.assertTrue(stale)
        self.assertTrue(all(r["status"] == meetings.NOTICE_MISSED for r in stale))
        self.assertTrue(all(r["note"] for r in stale), "a missed notice with no reason")
        self.assertEqual(len(self.channel.sent), 1, "a late notice went out anyway")

    async def test_the_sweep_starts_when_the_bot_is_ready(self):
        """The listener this whole feature is scheduled on — started, then stopped,
        so no test after this one inherits a running loop."""
        self.assertFalse(self.cog.notices.is_running())
        await self.cog.on_ready()
        try:
            self.assertTrue(self.cog.notices.is_running())
        finally:
            self.cog.notices.cancel()
            await asyncio.sleep(0)  # let the cancellation land before asserting it
        self.assertFalse(self.cog.notices.is_running())


if __name__ == "__main__":
    unittest.main()
