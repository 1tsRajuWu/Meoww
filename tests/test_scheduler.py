"""Scheduled messages: a promise the panel makes after the browser is closed.

Three properties are what make the feature worth having, and each of them is a
test here rather than a hope:

* the queue is a **row**, not a timer, so it survives the process (and the page)
  that created it;
* a due message is posted **exactly once** even when the delivery loop and an
  administrator's "Send now" look at it at the same moment;
* a message that could not be delivered **says so** — skipped, refused, or left
  mid-flight by a restart — instead of quietly disappearing.

The gateway stand-ins come from ``tests.test_composer``; this module drives the
same Flask app with a throwaway database behind it.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from tests.support import TempDatabaseMixin
from tests.test_composer import (
    CHANNEL_ID,
    CSRF,
    GUILD_ID,
    MEMBER_ID,
    OTHER_CHANNEL_ID,
    OTHER_GUILD_ID,
    QUIET_CHANNEL_ID,
    ComposerCase,
    DashboardTemplateCase,
)
from utils import db, scheduled


def payload(content="hello from the past", **overrides):
    """A payload in the shape ``_build_message`` stores."""
    body = {
        "content": content,
        "embed": None,
        "users": [],
        "roles": [],
        "everyone": False,
        "ignored_mentions": [],
    }
    body.update(overrides)
    return body


class SchedulerCase(TempDatabaseMixin, ComposerCase):
    """The composer's stand-ins, plus a database that never touches the project."""

    def setUp(self):
        self.use_temp_database("scheduler.db")
        super().setUp()  # ComposerCase: guild fakes, patched BOT_INSTANCE, client
        asyncio.run(db.setup())
        # Module state the loop keeps between ticks, reset per test so an hourly
        # prune in one test cannot hide the one in the next.
        self.keep_alive._last_prune = 0.0

    def tearDown(self):
        super().tearDown()
        self.release_temp_database()

    # ── helpers ──────────────────────────────────────────────────────────
    def queue(self, **overrides):
        """Post a scheduling request the way the page does."""
        body = {
            "guild_id": GUILD_ID,
            "channel_ids": [CHANNEL_ID],
            "content": "hello later",
            "mentions": [],
            "run_at_ms": int((time.time() + 3600) * 1000),
            "repeat_minutes": 0,
        }
        body.update(overrides)
        return self._post("/api/schedule-message", body)

    def listed(self):
        data = self._get(f"/api/scheduled?guild_id={GUILD_ID}").get_json()
        self.assertTrue(data["success"], data)
        return data

    def stored(self):
        return scheduled.list_for_guild(GUILD_ID)

    def tick(self, now=None):
        return asyncio.run(self.keep_alive._scheduler_tick(now))


class QueueStoreCase(SchedulerCase):
    """The store's own rules: what it hands out, and what it refuses to hand out."""

    def test_a_queued_message_outlives_the_connection_that_wrote_it(self):
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), int(time.time()) + 60)
        self.assertIsNotNone(msg_id)
        # Read back through a brand-new connection: nothing here is held in memory.
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["channel_ids"], [str(CHANNEL_ID)])
        self.assertEqual(row["payload"]["content"], "hello from the past")
        self.assertEqual(row["preview"], "hello from the past")
        self.assertEqual(row["repeat_minutes"], 0)

    def test_only_a_message_whose_time_has_come_is_handed_to_the_loop(self):
        now = int(time.time())
        due_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload("due"), now - 5)
        scheduled.create(GUILD_ID, [CHANNEL_ID], payload("later"), now + 600)
        ids = [m["id"] for m in scheduled.due(now)]
        self.assertEqual(ids, [due_id], "a future message must not be delivered early")
        self.assertEqual(
            [m["preview"] for m in scheduled.due(now + 700)],
            ["due", "later"],
            "a message is handed over only once its own time has come",
        )

    def test_two_claimers_cannot_both_post_the_same_message(self):
        """The conditional update is the entire exactly-once guarantee."""
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), int(time.time()) - 1)
        self.assertTrue(scheduled.claim(msg_id))
        self.assertFalse(scheduled.claim(msg_id), "a second claimer must lose")
        self.assertFalse(scheduled.claim(msg_id, now=int(time.time()) + 99))
        self.assertEqual(scheduled.get(GUILD_ID, msg_id)["status"], "sending")

    def test_a_message_the_process_died_while_sending_is_failed_not_retried(self):
        now = int(time.time())
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), now - 5)
        scheduled.claim(msg_id, now)
        self.assertEqual(scheduled.sweep_stale(now + 60), [], "not stale yet")
        self.assertEqual(scheduled.sweep_stale(now + scheduled.STALE_CLAIM_SECONDS + 1), [msg_id])
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "failed")
        self.assertIn("restarted", row["last_error"])
        # It is settled once, not swept over and over.
        self.assertEqual(scheduled.sweep_stale(now + 10_000), [])

    def test_a_repeat_moves_to_its_next_slot_instead_of_closing(self):
        now = int(time.time())
        msg_id = scheduled.create(
            GUILD_ID, [CHANNEL_ID], payload(), now - 5, repeat_minutes=1440
        )
        scheduled.claim(msg_id, now)
        scheduled.mark_sent(msg_id, now, results=[{"channel_id": CHANNEL_ID, "ok": True}])
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "pending", "a repeat is never finished by being sent")
        self.assertEqual(row["run_at"], now - 5 + 86400, "the next slot keeps the clock time")
        self.assertGreater(row["run_at"], now, "the next slot is in the future")
        self.assertEqual(row["fire_count"], 1)
        self.assertEqual(row["last_run_at"], now)
        self.assertTrue(row["results"], "what happened last time is kept")

    def test_a_repeat_that_keeps_failing_stops_instead_of_nagging_forever(self):
        now = int(time.time())
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), now - 1, repeat_minutes=1440)
        for attempt in range(scheduled.FAIL_LIMIT):
            scheduled.claim(msg_id, now + attempt)
            scheduled.mark_failed(msg_id, now + attempt, "Discord refused it")
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["fail_streak"], scheduled.FAIL_LIMIT)
        self.assertIn("refused", row["last_error"])

    def test_a_success_clears_the_failure_streak(self):
        now = int(time.time())
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), now - 1, repeat_minutes=1440)
        scheduled.claim(msg_id, now)
        scheduled.mark_failed(msg_id, now, "Discord refused it")
        scheduled.claim(msg_id, now + 1)
        scheduled.mark_sent(msg_id, now + 1)
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["fail_streak"], 0)
        self.assertEqual(row["status"], "pending")

    def test_a_missed_run_is_not_counted_as_a_failure(self):
        now = int(time.time())
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), now - 1, repeat_minutes=1440)
        scheduled.claim(msg_id, now)
        scheduled.mark_missed(msg_id, now, "skipped — the bot was offline")
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "pending", "a repeat keeps its place in the queue")
        self.assertEqual(row["fail_streak"], 0, "being offline is not the message's fault")
        self.assertIn("offline", row["last_error"])
        self.assertGreater(row["run_at"], now)

    def test_a_repeat_that_can_never_work_again_is_closed(self):
        now = int(time.time())
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), now - 1, repeat_minutes=10080)
        scheduled.claim(msg_id, now)
        scheduled.mark_failed(msg_id, now, "I am not in that server any more", final=True)
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "failed", "a weekly message for a left server must stop")

    def test_cancelling_is_scoped_to_the_server_it_belongs_to(self):
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), int(time.time()) + 60)
        self.assertFalse(scheduled.cancel(OTHER_GUILD_ID, msg_id), "another server may not cancel it")
        self.assertEqual(scheduled.get(GUILD_ID, msg_id)["status"], "pending")
        self.assertTrue(scheduled.cancel(GUILD_ID, msg_id))
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "cancelled")
        self.assertFalse(scheduled.cancel(GUILD_ID, msg_id), "cancelled once, not twice")
        # And it is still visible: a cancellation that removed the message from the
        # panel without a word would be indistinguishable from one that never
        # existed, or worse, from one that is still going to be posted.
        listing = scheduled.list_for_guild(GUILD_ID)
        self.assertEqual(listing["upcoming"], [])
        self.assertEqual([m["status"] for m in listing["recent"]], ["cancelled"])
        self.assertIsNotNone(row["last_run_at"])

    def test_the_queue_is_capped_per_server(self):
        now = int(time.time())
        for _ in range(scheduled.MAX_PENDING_PER_GUILD):
            self.assertIsNotNone(
                scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), now + 60)
            )
        self.assertIsNone(
            scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), now + 60),
            "the 26th message must be refused, not queued",
        )
        # The cap is per server: another one is unaffected.
        self.assertIsNotNone(
            scheduled.create(OTHER_GUILD_ID, [OTHER_CHANNEL_ID], payload(), now + 60)
        )

    def test_old_history_is_pruned_and_the_queue_is_not(self):
        now = int(time.time())
        keep = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), now - 90 * 86400)
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO scheduled_messages "
                "(guild_id, channel_ids, payload, preview, run_at, repeat_minutes, status, "
                " created_at, fire_count, fail_streak, last_run_at, last_error) "
                "VALUES (?, '[]', '{}', 'old', ?, 0, 'sent', ?, 1, 0, ?, 'gone')",
                (GUILD_ID, now - 200 * 86400, now, now - 200 * 86400),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(scheduled.prune(now), 1)
        self.assertIsNotNone(scheduled.get(GUILD_ID, keep), "a pending message is never pruned")
        self.assertEqual(self.listed()["recent"], [])

    def test_the_next_slot_does_not_drift_after_a_long_outage(self):
        """A daily 09:00 post is still at 09:00 after the bot was offline for a month."""
        start = 1_700_000_000
        now = start + 30 * 86400 + 60
        nxt = scheduled.next_occurrence(start, 1440, now)
        self.assertGreater(nxt, now)
        self.assertEqual((nxt - start) % 86400, 0, "the grid is the original time")
        self.assertLess(nxt - now, 86400, "and it is the *next* slot, not the 30th")
        self.assertEqual(scheduled.next_occurrence(start, 0, now), None)


class ScheduleEndpointCase(SchedulerCase):
    """What the endpoints accept, refuse, and hand back."""

    def test_scheduling_requires_a_session_and_a_csrf_token(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self._post("/api/schedule-message", {}).status_code, 401)
        self.assertEqual(self._get("/api/scheduled").status_code, 401)
        self._session()
        self.assertEqual(self._post("/api/schedule-message", {}, csrf=None).status_code, 403)
        self.assertEqual(
            self._post(f"/api/scheduled/{CHANNEL_ID}/cancel", {}, csrf="wrong").status_code, 403
        )

    def test_a_message_is_queued_and_listed_with_its_channel_and_author(self):
        with self.client.session_transaction() as session:
            session["user_id"] = MEMBER_ID
        resp = self.queue(content="Weekly town hall")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["message"]["status"], "pending")
        self.assertEqual(data["message"]["channel_ids"], [str(CHANNEL_ID)])
        self.assertEqual(data["message"]["channels"][0]["name"], "announcements")
        self.assertEqual(data["message"]["created_by_name"], "RAJ", "the queue says who queued it")

        listing = self.listed()
        self.assertEqual(len(listing["upcoming"]), 1)
        self.assertEqual(listing["upcoming"][0]["preview"], "Weekly town hall")
        self.assertEqual(listing["pending"], 1)
        self.assertEqual(listing["max_pending"], scheduled.MAX_PENDING_PER_GUILD)
        self.assertEqual(listing["recent"], [], "nothing has run yet")

    def test_the_list_names_a_channel_that_has_since_gone(self):
        msg_id = scheduled.create(GUILD_ID, [999999], payload(), int(time.time()) + 60)
        row = [m for m in self.listed()["upcoming"] if m["id"] == msg_id][0]
        self.assertFalse(row["channels"][0]["exists"])
        self.assertIsNone(row["channels"][0]["name"])

    def test_a_time_in_the_past_is_refused_with_the_servers_own_clock(self):
        resp = self.queue(run_at_ms=int((time.time() - 3600) * 1000))
        self.assertEqual(resp.status_code, 400)
        error = resp.get_json()["error"]
        self.assertIn("already passed", error)
        self.assertIn("UTC", error, "the message has to name the clock it is judging against")
        self.assertEqual(self.stored()["upcoming"], [], "nothing may be queued by a refusal")

    def test_a_time_far_beyond_the_horizon_is_refused(self):
        resp = self.queue(run_at_ms=int((time.time() + 400 * 86400) * 1000))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("year", resp.get_json()["error"])

    def test_a_time_a_few_seconds_away_is_queued_for_the_next_tick(self):
        """A browser clock behind this one makes "now" look like the past."""
        resp = self.queue(run_at_ms=int((time.time() - 20) * 1000))
        self.assertEqual(resp.status_code, 200, resp.get_json())
        row = self.stored()["upcoming"][0]
        self.assertGreaterEqual(row["run_at"], int(time.time()))

    def test_a_missing_or_unreadable_time_is_refused_not_guessed(self):
        for bad in ({}, {"run_at_ms": ""}, {"run_at_ms": "soon"}, {"run_at_ms": float("nan")}):
            body = {
                "guild_id": GUILD_ID,
                "channel_ids": [CHANNEL_ID],
                "content": "hello",
            }
            body.update(bad)
            resp = self._post("/api/schedule-message", body)
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIn("date and time", resp.get_json()["error"])

    def test_an_unknown_repeat_is_refused(self):
        resp = self.queue(repeat_minutes=7)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("repeat", resp.get_json()["error"])
        self.assertEqual(self.stored()["upcoming"], [])

    def test_a_queued_message_is_validated_exactly_like_an_immediate_one(self):
        """Same builder, so a refused send cannot become an accepted schedule."""
        for overrides, expected in (
            ({"channel_ids": []}, "Pick at least one channel."),
            ({"content": "", "title": "Only a title"}, "embed"),
            ({"content": ""}, "Write something to send."),
            ({"content": "x" * 2001}, "characters"),
            ({"mentions": [str(MEMBER_ID)] * 30}, "tags"),
        ):
            resp = self.queue(**overrides)
            self.assertEqual(resp.status_code, 400, overrides)
            self.assertIn(expected, resp.get_json()["error"], overrides)
        self.assertEqual(self.stored()["upcoming"], [])

    def test_a_tag_that_matches_nobody_is_reported_at_queue_time(self):
        """Resolved now, not later: the ping would otherwise be missing silently."""
        resp = self.queue(mentions=["1549177590328205393"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["ignored_mentions"], ["1549177590328205393"])

    def test_a_queue_request_cannot_be_pointed_at_another_server(self):
        """A pinned session stays pinned: the body names another server, and the
        channel it names is not in *this* one, so the request is refused."""
        resp = self.queue(guild_id=OTHER_GUILD_ID, channel_ids=[OTHER_CHANNEL_ID + 50])
        self.assertEqual(resp.status_code, 400, resp.get_json())
        self.assertIn("no longer exists", resp.get_json()["error"])
        self.assertEqual(self.stored()["upcoming"], [])
        self.assertEqual(scheduled.list_for_guild(OTHER_GUILD_ID)["upcoming"], [])

    def test_a_full_queue_is_refused_with_the_reason(self):
        now = int(time.time())
        for _ in range(scheduled.MAX_PENDING_PER_GUILD):
            scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), now + 60)
        resp = self.queue()
        self.assertEqual(resp.status_code, 429)
        self.assertIn("cancel one", resp.get_json()["error"])
        self.assertEqual(resp.get_json()["pending"], scheduled.MAX_PENDING_PER_GUILD)

    def test_cancelling_reports_a_message_that_already_went_out(self):
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), int(time.time()) - 1)
        scheduled.claim(msg_id)
        scheduled.mark_sent(msg_id, int(time.time()))
        resp = self._post(f"/api/scheduled/{msg_id}/cancel", {"guild_id": GUILD_ID})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already sent", resp.get_json()["error"])

    def test_a_cancelled_message_is_reported_as_cancelled_not_merely_gone(self):
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload("never mind"), 2_000_000_000)
        resp = self._post(f"/api/scheduled/{msg_id}/cancel", {"guild_id": GUILD_ID})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        listing = self.listed()
        self.assertEqual(listing["upcoming"], [])
        self.assertEqual([m["status"] for m in listing["recent"]], ["cancelled"])
        self.assertIn("cancelled", listing["recent"][0]["last_error"])

    def test_cancelling_a_message_of_another_server_is_a_404_not_a_silent_no_op(self):
        msg_id = scheduled.create(OTHER_GUILD_ID, [OTHER_CHANNEL_ID], payload(), 2_000_000_000)
        resp = self._post(f"/api/scheduled/{msg_id}/cancel", {"guild_id": GUILD_ID})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(scheduled.get(OTHER_GUILD_ID, msg_id)["status"], "pending")


class DeliveryCase(SchedulerCase):
    """What actually gets posted, and what gets written down afterwards."""

    def test_a_due_message_is_posted_by_the_tick(self):
        now = int(time.time())
        # The stored payload is exactly what ``_build_message`` writes: the tag is
        # in the content *and* in ``allowed_mentions``, which is what makes it ping.
        msg_id = scheduled.create(
            GUILD_ID,
            [CHANNEL_ID],
            payload(f"<@{MEMBER_ID}> tick tock", users=[MEMBER_ID]),
            now - 1,
        )
        done = self.tick(now)
        self.assertEqual([d["id"] for d in done], [msg_id])
        self.assertEqual(done[0]["status"], "sent")
        self.assertEqual(len(self.standard.sent), 1)
        posted = self.standard.sent[0]
        self.assertIn("tick tock", posted["content"])
        self.assertIn(f"<@{MEMBER_ID}>", posted["content"], "the stored tag is still a ping")
        self.assertEqual(posted["allowed_mentions"].users[0].id, MEMBER_ID)
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["results"][0]["channel_id"], str(CHANNEL_ID))
        self.assertTrue(row["results"][0]["jump_url"].startswith("https://discord.com/"))

    def test_the_tick_posts_a_message_once_and_only_once(self):
        now = int(time.time())
        scheduled.create(GUILD_ID, [CHANNEL_ID], payload("once"), now - 1)
        self.tick(now)
        self.tick(now)
        self.tick(now + 60)
        self.assertEqual(len(self.standard.sent), 1, "a delivered message must not repeat")

    def test_the_tick_consumes_nothing_while_the_bot_is_offline(self):
        now = int(time.time())
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), now - 1)
        offline = mock.MagicMock()
        offline._has_started = False
        with mock.patch.object(self.keep_alive, "BOT_INSTANCE", offline):
            self.assertEqual(self.tick(now), [])
        self.assertEqual(scheduled.get(GUILD_ID, msg_id)["status"], "pending")
        self.assertEqual(self.standard.sent, [])
        # And it goes out once the bot is back, provided it is not too late.
        self.assertEqual([d["id"] for d in self.tick(now + 30)], [msg_id])

    def test_a_message_more_than_a_day_late_is_skipped_and_says_so(self):
        now = int(time.time())
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), now - 2 * 86400)
        done = self.tick(now)
        self.assertEqual(done[0]["status"], "missed")
        self.assertEqual(self.standard.sent, [])
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "failed")
        self.assertIn("catch-up window", row["last_error"])

    def test_a_repeat_the_bot_slept_through_catches_up_once(self):
        now = int(time.time())
        msg_id = scheduled.create(
            GUILD_ID, [CHANNEL_ID], payload(), now - 3 * 86400, repeat_minutes=1440
        )
        self.tick(now)
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(self.standard.sent, [], "a three-day-old post is not worth sending")
        self.assertEqual(row["status"], "pending")
        self.assertGreater(row["run_at"], now)
        self.assertLessEqual(row["run_at"] - now, 86400)
        self.assertEqual(row["fire_count"], 1, "one catch-up, not three")

    def test_a_repeat_goes_out_today_and_is_queued_for_tomorrow(self):
        now = int(time.time())
        msg_id = scheduled.create(
            GUILD_ID, [CHANNEL_ID], payload("daily"), now - 30, repeat_minutes=1440
        )
        self.tick(now)
        self.assertEqual(len(self.standard.sent), 1)
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["run_at"], now - 30 + 86400)
        self.assertEqual(row["fire_count"], 1)

    def test_a_channel_that_refuses_the_message_is_named_in_the_outcome(self):
        now = int(time.time())
        msg_id = scheduled.create(GUILD_ID, [QUIET_CHANNEL_ID], payload(), now - 1)
        self.tick(now)
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "failed")
        self.assertIn("staff-only", row["last_error"])
        self.assertEqual(row["results"][0]["ok"], False)

    def test_a_server_the_bot_has_left_stops_its_repeat(self):
        now = int(time.time())
        msg_id = scheduled.create(
            OTHER_GUILD_ID, [OTHER_CHANNEL_ID], payload(), now - 1, repeat_minutes=1440
        )
        class _Left:
            """A bot that is online but is no longer in that server."""

            _has_started = True

            @staticmethod
            def get_guild(gid):
                return self.guild if gid == GUILD_ID else None

        with mock.patch.object(self.keep_alive, "BOT_INSTANCE", _Left()):
            done = self.tick(now)
        self.assertEqual(done[0]["status"], "failed")
        row = scheduled.get(OTHER_GUILD_ID, msg_id)
        self.assertEqual(row["status"], "failed", "a daily message for a left server must stop")
        self.assertIn("not in that server", row["last_error"])

    def test_firing_from_the_dashboard_sends_it_now_and_reports_a_receipt(self):
        now = int(time.time())
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload("send me now"), now + 3600)
        resp = self._post(f"/api/scheduled/{msg_id}/fire", {"guild_id": GUILD_ID})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data["sent"]), 1)
        self.assertEqual(data["sent"][0]["channel_id"], str(CHANNEL_ID))
        self.assertEqual(data["failed"], [])
        self.assertIn("send me now", self.standard.sent[0]["content"])
        self.assertEqual(scheduled.get(GUILD_ID, msg_id)["status"], "sent")

    def test_firing_a_message_that_is_no_longer_waiting_is_refused(self):
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), int(time.time()) + 60)
        scheduled.cancel(GUILD_ID, msg_id)
        resp = self._post(f"/api/scheduled/{msg_id}/fire", {"guild_id": GUILD_ID})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("cancelled", resp.get_json()["error"])
        self.assertEqual(self.standard.sent, [])

    def test_firing_a_message_of_another_server_is_a_404(self):
        msg_id = scheduled.create(OTHER_GUILD_ID, [OTHER_CHANNEL_ID], payload(), 2_000_000_000)
        resp = self._post(f"/api/scheduled/{msg_id}/fire", {"guild_id": GUILD_ID})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.other_guild.text_channels[0].sent, [])

    def test_a_refused_fire_does_not_leave_the_message_stuck_mid_flight(self):
        """Claiming before the rate check would strand it until the stale sweep."""
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), int(time.time()) + 60)
        with mock.patch.object(self.keep_alive, "_send_allowed", return_value=False):
            resp = self._post(f"/api/scheduled/{msg_id}/fire", {"guild_id": GUILD_ID})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            scheduled.get(GUILD_ID, msg_id)["status"],
            "pending",
            "a refused send must leave the message queued, not claimed",
        )
        self.assertEqual(self.standard.sent, [])

    def test_the_tick_survives_a_row_that_raises(self):
        now = int(time.time())
        scheduled.create(GUILD_ID, [CHANNEL_ID], payload("first"), now - 2)
        scheduled.create(GUILD_ID, [CHANNEL_ID], payload("second"), now - 1)
        with mock.patch.object(
            self.keep_alive, "_deliver_scheduled", side_effect=RuntimeError("boom")
        ):
            done = self.tick(now)
        self.assertEqual(len(done), 2, "one broken message must not stop the others")
        self.assertTrue(all(item["status"] == "failed" for item in done))


class ScheduleTemplateCase(unittest.TestCase):
    """The page has to carry the controls, and they have to be the right ones."""

    @staticmethod
    def render(**overrides):
        context = {
            "schedule_repeat_choices": [
                {"minutes": minutes, "label": label}
                for minutes, label in scheduled.REPEAT_CHOICES
            ],
            "schedule_max_pending": scheduled.MAX_PENDING_PER_GUILD,
            "schedule_catchup_hours": scheduled.MISS_CATCHUP_SECONDS // 3600,
            "server_now": 1_700_000_000,
        }
        context.update(overrides)
        return DashboardTemplateCase.render(**context)

    def test_the_scheduling_controls_are_on_the_page(self):
        html = self.render()
        for element_id in (
            "composer-when-now",
            "composer-when-later",
            "composer-when-fields",
            "composer-run-at",
            "composer-repeat",
            "composer-run-hint",
            "schedule-upcoming",
            "schedule-recent",
            "schedule-pending",
            "schedule-status",
        ):
            self.assertIn(f'id="{element_id}"', html, element_id)

    def test_the_time_is_a_real_date_and_time_field_with_a_real_repeat_list(self):
        html = self.render()
        self.assertIn('type="datetime-local"', html)
        self.assertIn('<option value="1440">Daily</option>', html)
        self.assertIn('<option value="10080">Weekly</option>', html)
        self.assertIn('id="composer-when-fields" hidden', html, "later is the opt-in")

    def test_the_page_calls_the_scheduling_endpoints(self):
        html = self.render()
        self.assertIn("/api/scheduled", html)
        self.assertIn("/api/schedule-message", html)
        self.assertIn("/cancel", html)
        self.assertIn("/fire", html)

    def _schedule_js(self, extra):
        """Run the page's own scheduling helpers in a real JavaScript engine."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        match = re.search(
            r"/\* schedule-format:start \*/(.*?)/\* schedule-format:end \*/", self.render(), re.S
        )
        self.assertIsNotNone(match, "the scheduling helpers are no longer marked for extraction")
        script = f"{match.group(1)}\n{extra}"
        handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
        try:
            handle.write(script)
            handle.close()
            run = subprocess.run([node, handle.name], capture_output=True, text=True)
        finally:
            os.unlink(handle.name)
        self.assertEqual(run.returncode, 0, run.stderr[:600])
        return json.loads(run.stdout)

    def test_the_time_is_described_the_way_a_person_reads_it(self):
        cases = [
            (30, "in less than a minute"),
            (60, "in 1 minute"),
            (3600, "in 1 hour"),
            (7200, "in 2 hours"),
            (3 * 86400, "in 3 days"),
            (-10, "just now"),
            (-600, "10 minutes ago"),
            (-2 * 3600, "2 hours ago"),
        ]
        for offset, expected in cases:
            out = self._schedule_js(
                f"console.log(JSON.stringify(scheduleWhen(1000000 + ({offset}), 1000000)));"
            )
            self.assertEqual(out["text"], expected, offset)
            self.assertEqual(out["late"], offset < 0)
            self.assertTrue(out["absolute"], "the exact moment is always given")

    def test_a_scheduled_outcome_is_read_as_sent_partial_failed_or_skipped(self):
        def outcome(msg):
            return self._schedule_js(
                f"console.log(JSON.stringify(scheduleOutcome({json.dumps(msg)})));"
            )

        sent = outcome({
            "status": "sent",
            "results": [{"ok": True}, {"ok": True}],
            "last_error": "",
        })
        self.assertEqual((sent["tone"], sent["label"]), ("ok", "sent"))

        partial = outcome({
            "status": "pending",
            "results": [{"ok": True}, {"ok": False}],
            "last_error": "1 channel(s) refused it: no permission",
        })
        self.assertEqual((partial["tone"], partial["label"]), ("warn", "partial"))
        self.assertIn("no permission", partial["detail"])

        failed = outcome({
            "status": "failed",
            "results": [{"ok": False}],
            "last_error": "I cannot send messages in #staff-only",
        })
        self.assertEqual((failed["tone"], failed["label"]), ("fail", "failed"))

        skipped = outcome({
            "status": "failed",
            "results": [],
            "last_error": "skipped — it was due 2 days ago",
        })
        self.assertEqual(skipped["label"], "not sent")
        self.assertIn("2 days", skipped["detail"])

        cancelled = outcome({"status": "cancelled", "results": [], "last_error": ""})
        self.assertEqual(cancelled["label"], "cancelled")

    def test_names_from_the_server_are_rendered_as_text_never_as_markup(self):
        html = self.render()
        for line in (
            "preview.textContent = msg.preview",
            "scheduleEls.pending.textContent",
            "whenEl.textContent = when.text",
        ):
            self.assertIn(line, html, line)
        self.assertNotIn("innerHTML = msg.preview", html)

    def test_a_finished_run_is_never_described_as_being_in_the_future(self):
        """The page's clock can trail the server's by a second, and "in less than a
        minute" beside a message that already went out reads as a bug."""
        html = self.render()
        self.assertIn(
            "const at = upcoming ? msg.run_at : Math.min(msg.last_run_at || msg.run_at, now - 1);",
            html,
        )
