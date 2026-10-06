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
import os
import sqlite3
import sys
import tempfile
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
        """Read at the clock this file books against, not at the wall's.

        The listing compares against the moment it is asked, so a test with a
        fixed ``NOW`` and no way to hand it over was a law that could only pass
        on the day its constant named.
        """
        meeting_id = self._meeting()
        self.assertEqual([r["id"] for r in meetings.upcoming(GUILD_ID, now=NOW)], [meeting_id])
        self.assertEqual(meetings.upcoming(GUILD_ID + 1, now=NOW), [])

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
    """Everything a card is sent, edited or asked with, in the order it happened.

    ``edits`` carries both spellings of an in-place edit, because which one the
    shipped code reaches for depends on whether it deferred first — and the
    draft card is edited through both across one booking.
    """

    def __init__(self):
        self.messages: list[dict] = []
        self.edits: list[dict] = []
        self.modals: list = []
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

    async def edit_message(self, content=None, **kwargs):
        self._done = True
        self.edits.append({"content": content, **kwargs})

    async def edit_original_response(self, content=None, **kwargs):
        self._done = True
        self.edits.append({"content": content, **kwargs})

    async def send_modal(self, modal):
        self._done = True
        self.modals.append(modal)


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

    def get_role(self, role_id: int):
        # A draft keeps the audience as an *id* — it has to survive a restart the
        # role object does not — so booking re-resolves it here, and a fake guild
        # that cannot answer one silently loses the ping the card promised.
        return SimpleNamespace(id=int(role_id), mention=f"<@&{role_id}>")


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


#: Every kind of menu a card in this feature can carry. ``ChannelSelect`` and
#: ``RoleSelect`` are their own classes rather than ``Select`` subclasses, so a
#: walker that only knew about one of them would find the time menu and miss
#: the channel one.
SELECTS = (discord.ui.Select, discord.ui.ChannelSelect, discord.ui.RoleSelect)


def _components(view, kinds) -> list:
    """Every component of one kind on a card, in the order a reader sees them."""
    found: list = []

    def walk(node) -> None:
        for child in getattr(node, "children", []) or []:
            if isinstance(child, kinds):
                found.append(child)
            else:
                walk(child)

    walk(view)
    return found


def _button(view, label: str):
    """One button, by the words on it."""
    for button in _components(view, discord.ui.Button):
        if (button.label or "").startswith(label):
            return button
    raise AssertionError(f"no button starting with {label!r} on this card")


def _select(view, placeholder: str):
    """One menu, by the placeholder a reader reads."""
    for menu in _components(view, SELECTS):
        if menu.placeholder == placeholder:
            return menu
    raise AssertionError(f"no select labelled {placeholder!r} on this card")


class MeetingsHarness(unittest.IsolatedAsyncioTestCase):
    """The fakes and the wiring the two end-to-end classes are driven with.

    The pieces are each covered above; what the classes below exist for is the
    seams *between* them, which is where a command feature actually breaks: a
    receipt that references a meeting the rollback dropped, a card edit that
    cannot find its message, a sweep that sends into a channel the command
    never chose, a private draft that writes half of something. Discord is
    replaced by four small fakes; everything else — the parsing, the rows, the
    cards, the callbacks — is the shipped code.

    ``cog_module`` is the load-bearing part. ``load_extension`` re-executes the
    file, so the class behind ``self.cog`` has globals of its own and a fake
    patched onto the module the ``import`` at the top of this file handed back
    is one the shipped code never looks up.
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

        # The module the cog is actually *made of*, which is not the one the
        # `import` at the top of this file handed back. ``load_extension``
        # re-executes the file, so the class behind ``self.cog`` has its own
        # globals — and a fake patched onto the imported module is a fake the
        # shipped code never looks up. Every callback below is invoked through
        # this module so the test drives one coherent copy of the code: the
        # class, the instance and the patch all belong to the same globals.
        self.cog_module = sys.modules[type(self.cog).__module__]
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
            self.cog_module, "send_panel", new=fake_send_panel
        )
        self._patcher.start()
        self._base_send = fake_send_panel

        # The second way a card reaches Discord is an in-place edit, which the
        # draft uses on every pick. Replaced at the same seam — the help panel
        # edits itself the same way — and it keeps ``legal_rows``, so a card the
        # layout helper would have refused still fails here.
        self.edits: list[tuple[object, object, object]] = []
        edits = self.edits

        async def fake_edit_card(target, *, content=None, view=None, **rest):
            if view is not None:
                style.legal_rows(view)
            edits.append((target, content, view))
            target.response.edits.append({"content": content, "view": view})
            return None

        self._edit_patcher = mock.patch.object(style, "edit_card", new=fake_edit_card)
        self._edit_patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()
        self._edit_patcher.stop()
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

    def _press(self, *, user_id: int = 5, values=None):
        """A component press on the draft card: fresh interaction, what the menu sent."""
        interaction = _interaction(self.guild, self.channel, self.bot, user_id=user_id)
        interaction.data = {"values": list(values)} if values else {}
        return interaction

    async def _open_draft(self, **overrides):
        """Run `/meeting create` with nothing filled in; hand back the draft card."""
        args = dict(
            title=None, when=None, channel=None, voice=None, role=None,
            where=None, agenda=None, notices=None,
        )
        args.update(overrides)
        await self.cog_module.Meetings.meeting_create.callback(
            self.cog, self.interaction, *args.values()
        )
        packets = self.interaction.response.messages
        self.assertTrue(packets, "the draft was never sent")
        self.assertTrue(packets[-1]["ephemeral"], "the draft was not private")
        return packets[-1]["view"]


class TheWholeLifeOfAMeeting(MeetingsHarness):
    """The private card, then the button, then the flow — driven end to end.

    `/meeting create` has no one-shot path any more: every option seeds the draft
    and the meeting exists only after *Book it*, so this class presses the button
    rather than reaching past it.
    """

    async def _create(self, **overrides) -> dict:
        """A whole meeting through the button, leaving the press on ``self.book``."""
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
        panel = await self._open_draft(**args)
        # Nothing exists yet: what the command sent is the private card, and the
        # channel has heard nothing.
        self.assertEqual(meetings.upcoming(GUILD_ID), [], "the command booked without asking")
        self.assertEqual(self.channel.sent, [], "the command announced itself")
        press = self._press()
        await _button(panel, "Book it").callback(press)
        self.book = press
        rows = meetings.upcoming(GUILD_ID)
        self.assertTrue(rows, f"nothing was booked: {press.response.edits}")
        return rows[-1]

    async def test_book_press_move_and_cancel(self):
        meeting = await self._create()

        # ── the announcement landed, with its buttons and its audience ──
        self.assertTrue(self.book.response.deferred, "the booking skipped the defer")
        self.assertEqual(len(self.channel.sent), 1)
        message, packet = self.channel.sent[0]
        self.assertEqual(
            [b.custom_id for b in _buttons(packet["view"])],
            ["meeting_rsvp:yes", "meeting_rsvp:maybe", "meeting_rsvp:no"],
        )
        self.assertEqual(list(packet["allowed_mentions"].roles), [1234])
        self.assertEqual(meeting["announce_message_id"], message.id)

        # ── the receipt names the queue that was actually written ──
        receipt = self.book.response.edits[-1]["view"]
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
        await self.cog_module.Meetings.meeting_reschedule.callback(
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
        await self.cog_module.Meetings.meeting_cancel.callback(
            self.cog, self.interaction, meeting["id"], "moved away"
        )
        self.assertEqual(meetings.get(meeting["id"])["status"], meetings.STATUS_CANCELLED)
        self.assertEqual(
            {r["status"] for r in meetings.notice_rows(meeting["id"])},
            {meetings.NOTICE_CANCELLED},
        )
        self.assertGreaterEqual(len(message.edits), 3)
        self.assertEqual(meetings.upcoming(GUILD_ID), [])

    async def test_a_junk_time_keeps_the_draft_and_writes_nothing(self):
        """A bad time is something the card can still be corrected on.

        What used to be a refusal that took the whole command down with it is now
        the private card carrying the warning — every other word it was given is
        still on it — and nothing has been booked either way.
        """
        panel = await self._open_draft(title="Whatever", when="banana")
        self.assertEqual(meetings.upcoming(GUILD_ID), [], "a draft wrote a meeting")
        self.assertEqual(self.channel.sent, [], "a card nobody could read was announced")
        text = _text(panel)
        self.assertIn("couldn't read", text)
        self.assertIn(meetings.WHEN_HINT, text)
        self.assertIn("Whatever", text, "a bad time lost the name that came with it")
        self.assertTrue(_button(panel, "Book it").disabled, "a timeless draft is bookable")

    async def test_a_card_that_will_not_post_drops_the_meeting(self):
        """The rollback is the point: a list that promises a card nobody saw.

        Only the *channel* send fails — the refusal and the receipt still go
        through — because that is the real shape of the failure: Discord
        refusing the one call that makes the meeting visible to anybody else.
        """
        async def flaky(target, view, **kwargs):
            if isinstance(target, _FakeChannel):
                raise RuntimeError("Discord said no")
            return await self._base_send(target, view, **kwargs)

        with mock.patch.object(self.cog_module, "send_panel", new=flaky):
            panel = await self._open_draft(title="Ghost", when="tomorrow 18:00")
            press = self._press()
            await _button(panel, "Book it").callback(press)
        self.assertEqual(meetings.upcoming(GUILD_ID), [])
        said = [edit["content"] for edit in press.response.edits if edit["content"]]
        self.assertTrue(said, "the rollback said nothing at all")
        self.assertIn("dropped the meeting", said[-1])

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


class ThePrivateDraft(MeetingsHarness):
    """`/meeting create` with nothing filled in: one private card, one write.

    Three promises the draft makes, and the way each could go wrong: **only the
    asker sees it** (an ephemeral card, plus a check that refuses anybody else),
    **nothing exists while it is a draft** (no row, so `/meeting list` cannot
    promise a meeting whose card is nowhere), and **finishing it writes exactly
    the meeting the command described** — the row, the announcement with its
    reply buttons, the notices and the receipt all come out of the one code path
    the button and the options share, which is what these tests are really about.
    """

    async def test_the_draft_opens_privately_and_writes_nothing(self):
        panel = await self._open_draft()
        self.assertEqual(meetings.upcoming(GUILD_ID), [], "a draft wrote a meeting")
        self.assertEqual(self.channel.sent, [], "a draft announced itself")
        text = _text(panel)
        self.assertIn("Only you can see this draft", text)
        self.assertIn("not picked yet", text)
        self.assertEqual(
            style.legal_rows(panel), 0, "the draft card is not a layout Discord accepts"
        )
        # Four menus and one button row: the card is what a reader can act on.
        self.assertEqual(len(_components(panel, SELECTS)), 4)
        self.assertEqual(len(_buttons(panel)), 3)

    async def test_the_whole_draft_ends_in_the_same_meeting(self):
        """Every step, then the moment it becomes real — driven through the card."""
        panel = await self._open_draft()

        # ── the time, chosen rather than typed ──
        pick = self._press(values=["tomorrow 18:00"])
        await _select(panel, "When does it start?").callback(pick)
        panel = pick.response.edits[-1]["view"]
        self.assertEqual(meetings.upcoming(GUILD_ID), [], "picking a time wrote a meeting")
        self.assertIn("Tomorrow at 18:00", _text(panel))

        # ── the words, from the one modal ──
        opened = self._press()
        await _button(panel, "Title, place, agenda").callback(opened)
        modal = opened.response.modals[-1]
        # discord.py fills these from the submitted payload; a test writes the
        # same private slot directly rather than faking an HTTP body.
        modal.name._value = "Sprint planning"
        modal.where._value = "Room 2"
        modal.agenda._value = "Q4 roadmap"
        submit = self._press()
        await modal.on_submit(submit)
        panel = submit.response.edits[-1]["view"]
        self.assertEqual(meetings.upcoming(GUILD_ID), [], "the modal wrote a meeting")
        self.assertIn("Sprint planning", _text(panel))
        self.assertFalse(_button(panel, "Book it").disabled, "Book it opened too late")

        # ── the notices, then the booking ──
        pick = self._press(values=["1h,15m"])
        await _select(panel, "Remind people when?").callback(pick)
        panel = pick.response.edits[-1]["view"]
        book = self._press()
        await _button(panel, "Book it").callback(book)

        rows = meetings.upcoming(GUILD_ID)
        self.assertEqual(len(rows), 1, "the draft booked nothing")
        meeting = rows[0]
        self.assertEqual(meeting["title"], "Sprint planning")
        self.assertEqual(meeting["location"], "Room 2")
        self.assertEqual(meeting["agenda"], "Q4 roadmap")
        self.assertEqual(meeting["channel_id"], CHANNEL_ID)
        self.assertEqual(meeting["notices"], [3600, 900])
        self.assertEqual(meeting["created_by"], 5)

        # ── the announcement landed where people can answer it ──
        self.assertEqual(len(self.channel.sent), 1, "the draft announced nothing")
        message, packet = self.channel.sent[0]
        self.assertEqual(
            [b.custom_id for b in _buttons(packet["view"])],
            ["meeting_rsvp:yes", "meeting_rsvp:maybe", "meeting_rsvp:no"],
        )
        self.assertEqual(meeting["announce_message_id"], message.id)

        # ── and the private card became the receipt ──
        receipt = book.response.edits[-1]["view"]
        self.assertIn("Meeting scheduled", _text(receipt))
        self.assertIn("Room 2", _text(receipt))
        self.assertIn("Open the announcement card", _text(receipt))

    async def test_book_it_is_shut_until_there_is_a_name_and_a_time(self):
        panel = await self._open_draft()
        self.assertTrue(_button(panel, "Book it").disabled, "a blank draft is bookable")

        pick = self._press(values=["in 1 hour"])
        await _select(panel, "When does it start?").callback(pick)
        panel = pick.response.edits[-1]["view"]
        self.assertTrue(_button(panel, "Book it").disabled, "a nameless draft is bookable")

        # Pressed anyway: it refuses and keeps the draft rather than writing.
        press = self._press()
        await _button(panel, "Book it").callback(press)
        self.assertEqual(meetings.upcoming(GUILD_ID), [])
        self.assertEqual(self.channel.sent, [], "a nameless draft was announced")
        self.assertIn("Give it a name first", _text(press.response.edits[-1]["view"]))

    async def test_a_time_it_cannot_read_keeps_the_rest_of_the_draft(self):
        panel = await self._open_draft()
        opened = self._press()
        await _button(panel, "Title, place, agenda").callback(opened)
        modal = opened.response.modals[-1]
        modal.name._value = "Sprint planning"
        modal.when._value = "banana"
        submit = self._press()
        await modal.on_submit(submit)

        redrawn = submit.response.edits[-1]["view"]
        self.assertIn("couldn't read", _text(redrawn))
        self.assertIn(meetings.WHEN_HINT, _text(redrawn))
        self.assertIn("Sprint planning", _text(redrawn), "the name was lost to one bad field")
        self.assertEqual(meetings.upcoming(GUILD_ID), [])

    async def test_the_custom_choice_asks_instead_of_guessing(self):
        panel = await self._open_draft()
        pick = self._press(values=[self.cog_module.CUSTOM_WHEN])
        await _select(panel, "When does it start?").callback(pick)
        self.assertEqual(len(pick.response.modals), 1, "the modal was not opened")
        self.assertEqual(pick.response.edits, [], "the card was redrawn instead of asked")

    async def test_the_draft_belongs_to_whoever_opened_it(self):
        panel = await self._open_draft()
        stranger = self._press(user_id=99)
        self.assertFalse(await panel.interaction_check(stranger))
        self.assertIn("belongs to somebody else", stranger.response.messages[0]["content"])
        self.assertTrue(await panel.interaction_check(self._press()))

    async def test_cancelling_a_draft_leaves_nothing_behind(self):
        panel = await self._open_draft()
        pick = self._press(values=["in 1 hour"])
        await _select(panel, "When does it start?").callback(pick)
        panel = pick.response.edits[-1]["view"]

        press = self._press()
        await _button(panel, "Cancel").callback(press)
        final = press.response.edits[-1]["view"]
        self.assertIn("Draft closed", _text(final))
        self.assertEqual(_buttons(final), [], "a closed draft still has buttons")
        self.assertEqual(meetings.upcoming(GUILD_ID), [])
        self.assertEqual(self.channel.sent, [])

    async def test_a_double_press_books_one_meeting_not_two(self):
        """Two presses are in flight the moment somebody double-clicks, and the
        card cannot be edited out from under the second one fast enough to stop
        it. The draft remembers what it became."""
        panel = await self._open_draft()
        opened = self._press()
        await _button(panel, "Title, place, agenda").callback(opened)
        modal = opened.response.modals[-1]
        modal.name._value = "Sprint planning"
        submit = self._press()
        await modal.on_submit(submit)
        panel = submit.response.edits[-1]["view"]
        pick = self._press(values=["in 1 hour"])
        await _select(panel, "When does it start?").callback(pick)
        panel = pick.response.edits[-1]["view"]

        first = self._press()
        second = self._press()
        button = _button(panel, "Book it")
        await button.callback(first)
        await button.callback(second)

        self.assertEqual(len(meetings.upcoming(GUILD_ID)), 1, "a double press booked twice")
        self.assertEqual(len(self.channel.sent), 1, "two cards were announced")
        self.assertIn("already booked", _text(second.response.edits[-1]["view"]))

    async def test_a_channel_it_cannot_post_in_is_refused_on_the_card(self):
        """The channel is picked before the write, so a bad one is a line on the
        draft rather than a meeting with no card anywhere."""
        panel = await self._open_draft()
        opened = self._press()
        await _button(panel, "Title, place, agenda").callback(opened)
        opened.response.modals[-1].name._value = "Sprint planning"
        submit = self._press()
        await opened.response.modals[-1].on_submit(submit)
        panel = submit.response.edits[-1]["view"]
        pick = self._press(values=["in 1 hour"])
        await _select(panel, "When does it start?").callback(pick)
        panel = pick.response.edits[-1]["view"]

        self.channel.permissions_for = lambda _me: SimpleNamespace(send_messages=False)
        press = self._press()
        await _button(panel, "Book it").callback(press)

        self.assertEqual(meetings.upcoming(GUILD_ID), [], "a refused channel booked anyway")
        self.assertEqual(self.channel.sent, [])
        self.assertIn("I can't post in", _text(press.response.edits[-1]["view"]))

    async def test_a_start_time_that_has_gone_by_is_refused_on_the_card(self):
        """A draft can sit open for fifteen minutes, so the time is checked at
        the moment it is spent rather than only when it was picked."""
        panel = await self._open_draft()
        opened = self._press()
        await _button(panel, "Title, place, agenda").callback(opened)
        opened.response.modals[-1].name._value = "Sprint planning"
        submit = self._press()
        await opened.response.modals[-1].on_submit(submit)
        panel = submit.response.edits[-1]["view"]
        pick = self._press(values=["in 1 hour"])
        await _select(panel, "When does it start?").callback(pick)
        panel = pick.response.edits[-1]["view"]

        panel.draft.starts_at = int(time.time()) - 60
        press = self._press()
        await _button(panel, "Book it").callback(press)

        self.assertEqual(meetings.upcoming(GUILD_ID), [])
        self.assertEqual(self.channel.sent, [])
        self.assertIn("passed while this draft was open", _text(press.response.edits[-1]["view"]))

    async def test_every_option_still_opens_the_private_card(self):
        """The inverse of the door that used to stand here: nothing books in one go.

        Two options, every option, or none — the card is the first surface each
        time, because the announcement is a decision rather than a side effect of
        a long command, and the person who typed it is the person who owns the
        draft. A filled-in one is a press away from the meeting, not a meeting.
        """
        await self.cog_module.Meetings.meeting_create.callback(
            self.cog, self.interaction, "Standup", "tomorrow 09:00",
            None, None, None, None, None, None,
        )
        self.assertEqual(meetings.upcoming(GUILD_ID), [], "the command booked without asking")
        self.assertEqual(self.channel.sent, [], "the command announced itself")
        packets = self.interaction.response.messages
        self.assertTrue(packets and packets[-1]["ephemeral"], "the card was not private")
        panel = packets[-1]["view"]
        self.assertIn("Standup", _text(panel))
        self.assertFalse(_button(panel, "Book it").disabled, "a filled-in draft is not bookable")

    async def test_half_the_options_open_a_draft_instead_of_arguing(self):
        """A title with no time is not an error; it is the start of a draft."""
        panel = await self._open_draft(title="Sprint planning")
        self.assertEqual(meetings.upcoming(GUILD_ID), [])
        self.assertIn("Sprint planning", _text(panel), "what was given was thrown away")
        self.assertTrue(_button(panel, "Book it").disabled)


class TheWriteThatWouldNotLand(MeetingsHarness):
    """`/meeting create` once answered "the database refused the write".

    The tables are made by ``db.setup()`` on boot, and that is not a promise
    about the file underneath: the cloud sync replaces ``bot.db`` with
    ``os.replace`` when it restores a backup, so a running process can find
    itself holding a database that predates its own release. The insert then
    failed with ``no such table: meetings`` — and the reason lived only in a log
    line, which is why the operator's card could say nothing they could act on.
    Both halves are pinned here: the feature repairs its own tables, and a
    failure that survives that is reported in the database's own words.
    """

    async def test_a_meeting_is_stored_in_a_database_that_never_ran_setup(self):
        """The operator's own ``bot.db`` was in exactly this state: no tables.

        ``PRAGMA table_info(meetings)`` answered nothing for all three of them,
        which is the shape of a database whose boot happened before this
        release — or whose boot happened against a different file. The write is
        not allowed to depend on a boot that has already been and gone.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bot.db")
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE guild_settings (guild_id INTEGER, key TEXT, value TEXT)"
                )
                conn.commit()
            finally:
                conn.close()

            now = int(time.time())
            with mock.patch.object(db, "DB_PATH", path):
                meeting_id = meetings.create(
                    GUILD_ID,
                    title="Schema heals",
                    starts_at=now + 3 * 3600,
                    channel_id=CHANNEL_ID,
                    notices=[3600, 900],
                    now=now,
                )
                self.assertIsNotNone(
                    meeting_id,
                    f"create() answered None on a writable database: "
                    f"{meetings.last_write_error()}",
                )
                self.assertEqual(meetings.last_write_error(), "")
                stored = meetings.get(meeting_id)
                self.assertEqual(stored["title"], "Schema heals")

            conn = sqlite3.connect(path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                notices = conn.execute("SELECT COUNT(*) FROM meeting_notices").fetchone()[0]
                index = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' "
                    "AND name LIKE 'idx_meeting%'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertLessEqual({"meetings", "meeting_notices", "meeting_rsvps"}, tables)
            self.assertEqual(notices, 2)
            self.assertEqual(index, 3)

    async def test_a_write_that_still_fails_is_reported_in_the_databases_words(self):
        """The card an operator reads must say which failure it was."""
        with mock.patch.object(
            db, "run_atomic", side_effect=sqlite3.OperationalError("no such table: meetings")
        ):
            # Nothing is written while it is a draft, so the failure only has one
            # moment it can happen at: the button.
            panel = await self._open_draft(title="Doomed", when="tomorrow 18:00")
            press = self._press()
            await _button(panel, "Book it").callback(press)
        self.assertEqual(meetings.upcoming(GUILD_ID), [])
        self.assertEqual(self.channel.sent, [], "a failed write announced itself")
        refusal = [edit["content"] for edit in press.response.edits if edit["content"]][-1]
        self.assertIn("refused the write", refusal)
        self.assertIn("no such table: meetings", refusal)
        self.assertIn("nothing is half-announced", refusal)


if __name__ == "__main__":
    unittest.main()
