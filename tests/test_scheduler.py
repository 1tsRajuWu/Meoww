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

from types import SimpleNamespace

from tests.support import TempDatabaseMixin
from tests.test_composer import (
    CHANNEL_ID,
    CSRF,
    GUILD_ID,
    MEMBER_ID,
    OTHER_CHANNEL_ID,
    OTHER_GUILD_ID,
    QUIET_CHANNEL_ID,
    _Channel,
    _ComposerFixture,
    _Guild,
    _Member,
    _VoiceChannel,
    render_dashboard,
)
from utils import db, digest, scheduled

#: A fixed instant for the digest tests: 2023-11-14 22:13:20 UTC, so "today" and
#: both seven-day windows are known strings rather than whatever day it is.
FIXED_NOW = 1_700_000_000
VOICE_CHANNEL_ID = 555010
GONE_CHANNEL_ID = 555011
GONE_MEMBER_ID = MEMBER_ID + 90
LOG_CHANNEL_ID = 555012


def _layout_text(node) -> str:
    """Every readable string inside a Components V2 layout, flattened.

    A local copy rather than an import from another test module: importing a module
    that defines ``TestCase`` classes makes pytest collect them here as well.
    """
    parts = []
    content = getattr(node, "content", None)
    if isinstance(content, str):
        parts.append(content)
    for child in getattr(node, "children", None) or []:
        parts.append(_layout_text(child))
    return "\n".join(parts)


def _card_text(message: dict) -> str:
    """What a card says, whether it went out as Components V2 or as an embed."""
    if message.get("view") is not None:
        return _layout_text(message["view"]) + "\n" + str(message.get("content") or "")
    embed = message.get("embed")
    if embed is not None:
        parts = [message.get("content") or "", embed.title or "", embed.description or ""]
        for field in embed.fields:
            parts += [field.name, field.value]
        if embed.footer:
            parts.append(embed.footer.text)
        return "\n".join(part for part in parts if part)
    return str(message.get("content") or "")


class _Tellable(_Member):
    """A member who can be sent a direct message."""

    def __init__(self, uid, name, display=None, error=None):
        super().__init__(uid, name, display)
        self.dms = []
        self.dm_error = error

    async def send(self, content=None, embed=None, **kwargs):
        if self.dm_error is not None:
            raise self.dm_error
        self.dms.append({"content": content, "embed": embed, **kwargs})
        return SimpleNamespace(id=len(self.dms), jump_url="")


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


class SchedulerCase(TempDatabaseMixin, _ComposerFixture):
    """The composer's stand-ins, plus a database that never touches the project.

    The *fixture*, not ``ComposerCase``: inheriting a class that carries tests
    re-collects every one of them here, and pytest reports each re-run as a pass.
    """

    def setUp(self):
        self.use_temp_database("scheduler.db")
        super().setUp()  # the fixture: guild fakes, patched BOT_INSTANCE, client
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

    def seed_activity(self, *, messages=(), voice=()):
        """Put rows in the tracker's tables, which is where a digest reads from."""
        conn = db.connect()
        try:
            for uid, cid, day, count in messages:
                conn.execute(
                    "INSERT OR REPLACE INTO activity_messages "
                    "(guild_id, user_id, channel_id, date, count) VALUES (?, ?, ?, ?, ?)",
                    (GUILD_ID, uid, cid, day, count),
                )
            for uid, cid, day, seconds in voice:
                conn.execute(
                    "INSERT OR REPLACE INTO activity_voice "
                    "(guild_id, user_id, channel_id, date, seconds) VALUES (?, ?, ?, ?, ?)",
                    (GUILD_ID, uid, cid, day, seconds),
                )
            conn.commit()
        finally:
            conn.close()


class QueueStoreCase(SchedulerCase):
    """The store's own rules: what it hands out, and what it refuses to hand out."""

    def test_a_settle_that_requires_the_old_status_refuses_a_row_already_settled(self):
        """The sweep's ownership check: one interruption, one settle.

        The write itself is the claim, exactly as it is on the delivery path — so two
        instances sharing a database cannot each record the same interruption (and
        each post the card that goes with it).
        """
        now = int(time.time())
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), now - 1)
        scheduled.claim(msg_id, now)

        self.assertTrue(scheduled.mark_failed(msg_id, now, "first", only_if_status="sending"))
        self.assertFalse(
            scheduled.mark_failed(msg_id, now, "second", only_if_status="sending"),
            "the second writer must be told it was not the one that settled it",
        )
        self.assertEqual(scheduled.get(GUILD_ID, msg_id)["last_error"], "first")

    def test_an_interrupted_repeat_keeps_its_slot_and_the_sweep_says_so(self):
        """The sweep reports what the store recorded, not an assumed failure."""
        now = int(time.time())
        msg_id = scheduled.create(
            GUILD_ID, [CHANNEL_ID], payload("daily"), now - 1, repeat_minutes=1440
        )
        scheduled.claim(msg_id, now)

        settled = scheduled.sweep_stale(now + scheduled.STALE_CLAIM_SECONDS + 1)

        self.assertEqual([row["id"] for row in settled], [msg_id])
        self.assertEqual(settled[0]["status"], "pending", "a repeat is not finished by a restart")
        self.assertEqual(scheduled.get(GUILD_ID, msg_id)["status"], "pending")
        self.assertGreater(scheduled.get(GUILD_ID, msg_id)["run_at"], now)

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
        # The settled row itself, not just its id: whoever is told about it needs
        # the server it belongs to and the person who queued it.
        settled = scheduled.sweep_stale(now + scheduled.STALE_CLAIM_SECONDS + 1)
        self.assertEqual([row["id"] for row in settled], [msg_id])
        self.assertEqual(settled[0]["guild_id"], GUILD_ID)
        self.assertEqual(settled[0]["status"], "failed")
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

    def test_the_failure_write_is_conditional_on_the_claim(self):
        """The read that checks the claim and the write that ends it are two steps.

        Another process (a second instance, or the sweep) can finish the row in
        between, and an unconditional write would overwrite a real outcome with a
        failure — and post a card for a message that may well have arrived. The
        interleaving cannot be driven from here without a hook inside the handler,
        so what is pinned is the contract the fix rests on.
        """
        now = int(time.time())
        scheduled.create(GUILD_ID, [CHANNEL_ID], payload("x"), now - 1)
        seen = []
        real = scheduled.mark_failed

        def recording(*args, **kwargs):
            seen.append(kwargs)
            return real(*args, **kwargs)

        with mock.patch.object(
            self.keep_alive, "_deliver_scheduled", side_effect=RuntimeError("boom")
        ), mock.patch.object(
            self.keep_alive, "_alert_schedule_outcome", new=mock.AsyncMock()
        ), mock.patch.object(self.keep_alive.scheduled, "mark_failed", side_effect=recording):
            self.tick(now)

        self.assertTrue(seen, "the failure was never recorded")
        self.assertEqual(
            seen[0].get("only_if_status"),
            "sending",
            "the failure write is unconditional: it can overwrite another process's outcome",
        )

    def test_a_row_another_process_already_settled_is_not_failed_again(self):
        """A row someone else finished is left as they recorded it, and not carded."""
        now = int(time.time())
        msg_id = scheduled.create(GUILD_ID, [CHANNEL_ID], payload("x"), now - 1)

        def settled_elsewhere_then_raised(_msg, _now):
            scheduled.mark_sent(
                msg_id, _now, [{"channel_id": CHANNEL_ID, "ok": True}]
            )
            raise RuntimeError("boom")

        with mock.patch.object(
            self.keep_alive, "_deliver_scheduled", side_effect=settled_elsewhere_then_raised
        ), mock.patch.object(
            self.keep_alive, "_alert_schedule_outcome", new=mock.AsyncMock()
        ) as alert:
            self.tick(now)

        self.assertEqual(
            scheduled.get(GUILD_ID, msg_id)["status"],
            "sent",
            "the tick overwrote an outcome another process had already recorded",
        )
        alert.assert_not_awaited()


class SendTimeoutCase(SchedulerCase):
    """A send that times out is recorded as unconfirmed, never as a refusal.

    ``asyncio.run_coroutine_threadsafe``'s future is not cancelled by a
    client-side timeout: the coroutine keeps running on the bot's loop and may
    post the message a second later. The old path answered "Discord did not accept
    it" — which it had no way to know — and skipped the history row entirely, so
    the operator's natural retry posted a second copy of an announcement that had
    already arrived.
    """

    def recent(self):
        data = self.listed()
        self.assertEqual(data["upcoming"], [], "an unconfirmed send is not queued either")
        return data["recent"]

    def _send_with_timeout(self):
        with mock.patch.object(
            self.keep_alive, "_run_on_bot_loop", side_effect=TimeoutError("no answer")
        ):
            return self._send(content="did it go out?")

    def test_a_timed_out_send_answers_504_and_says_unconfirmed(self):
        resp = self._send_with_timeout()
        self.assertEqual(resp.status_code, 504)
        payload = resp.get_json()
        self.assertFalse(payload["success"])
        self.assertTrue(payload["unknown"], "the page has to know it is unconfirmed")
        self.assertIn("may still", payload["error"], "and be told what to do about it")

    def test_a_timed_out_send_leaves_a_history_row_that_cannot_be_reposted(self):
        self._send_with_timeout()
        rows = self.recent()
        self.assertEqual(len(rows), 1, "an unconfirmed send is still a thing that happened")
        self.assertEqual(rows[0]["status"], "unknown")
        self.assertEqual(rows[0]["preview"], "did it go out?")
        # Nothing claimable: a row the delivery loop could find would post it again.
        self.assertEqual(scheduled.pending_count(GUILD_ID), 0)
        self.assertEqual(self.tick(int(time.time()) + 1), [])
        self.assertEqual(self.standard.sent, [], "and nothing was sent twice")

    def test_the_panel_calls_it_unconfirmed_not_not_sent(self):
        """The row's label is what the retry button is read as permission for."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        page = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('label: "unconfirmed"', page)
        self.assertIn('msg.status === "unknown"', page)


class SendRecordCase(SchedulerCase):
    """A message sent *now* is written down too.

    "Send now" was the one path whose report had nowhere to live: it was handed to
    the page and died with the tab, so the panel's history held only the scheduled
    half of what the dashboard had posted.
    """

    def recent(self):
        data = self.listed()
        self.assertEqual(data["upcoming"], [], "a sent message must not become a queued one")
        return data["recent"]

    def test_a_send_now_is_kept_as_the_record_of_it(self):
        resp = self._send(content="the announcement")
        self.assertTrue(resp.get_json()["success"], resp.get_json())
        rows = self.recent()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source"], "now")
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["preview"], "the announcement")
        self.assertEqual(row["channels"][0]["name"], "announcements")
        self.assertEqual([r["channel_id"] for r in row["results"]], [str(CHANNEL_ID)])
        self.assertTrue(row["results"][0]["ok"])
        self.assertTrue(row["results"][0]["jump_url"].startswith("https://discord.com/"))
        self.assertEqual(scheduled.pending_count(GUILD_ID), 0)
        # The strongest thing that can be said about a history row: the delivery
        # loop can never find it, so the record can never become a second post.
        self.assertEqual(self.tick(int(time.time()) + 1), [])
        self.assertEqual(len(self.standard.sent), 1)

    def test_a_send_that_reached_nobody_is_recorded_as_a_failure_with_the_reason(self):
        resp = self._send(content="quiet please", channel_ids=[QUIET_CHANNEL_ID])
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["success"])
        row = self.recent()[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("staff-only", row["last_error"])
        self.assertEqual(row["results"][0]["ok"], False)

    def test_a_partial_send_keeps_the_channel_that_refused_it(self):
        resp = self._send(content="split", channel_ids=[CHANNEL_ID, QUIET_CHANNEL_ID])
        self.assertTrue(resp.get_json()["success"])
        row = self.recent()[0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual([r["ok"] for r in row["results"]], [True, False])
        self.assertIn("staff-only", row["last_error"], "the refusal is kept beside it")

    def test_history_never_spends_the_queue_cap(self):
        for i in range(3):
            self._send(content=f"number {i}")
        self.assertEqual(scheduled.pending_count(GUILD_ID), 0)
        self.assertIsNotNone(
            scheduled.create(GUILD_ID, [CHANNEL_ID], payload(), int(time.time()) + 60),
            "a recorded send is not a queued message",
        )

    def test_the_record_is_pruned_like_any_other_history_row(self):
        old = int(time.time()) - 100 * 86400
        scheduled.record_sent(
            GUILD_ID,
            [CHANNEL_ID],
            payload("ancient"),
            [{"channel_id": CHANNEL_ID, "name": "announcements", "ok": True}],
            now=old,
        )
        self.assertEqual(len(self.recent()), 1)
        self.assertEqual(scheduled.prune(int(time.time())), 1)
        self.assertEqual(self.recent(), [])

    def test_the_record_names_who_sent_it(self):
        with self.client.session_transaction() as session:
            session["user_id"] = MEMBER_ID
        self._send(content="attributed")
        row = self.recent()[0]
        self.assertEqual(row["created_by"], str(MEMBER_ID))
        self.assertEqual(row["created_by_name"], "RAJ")


class ScheduleAlertCase(SchedulerCase):
    """A scheduled message that does not go out has to reach a person.

    Every other part of delivery is engineered to be trustworthy, and the failure
    of it was the one part with no audience: recorded on the row, and visible only in
    a panel somebody has to open. A daily announcement that quietly stopped is
    invisible that way — so these pin where the failure goes instead.
    """

    def setUp(self):
        super().setUp()
        self.logs = _Channel(LOG_CHANNEL_ID, "logs")
        # ``text_channels`` for the picker's sake, ``_channels`` because that is
        # what a lookup resolves through — a log channel the gateway can find.
        self.guild.text_channels.append(self.logs)
        self.guild._channels[LOG_CHANNEL_ID] = self.logs

    def configure_log_channel(self):
        asyncio.run(db.set_setting(GUILD_ID, "log_channel_id", LOG_CHANNEL_ID))

    def cards(self):
        return [_card_text(message) for message in self.logs.sent]

    def fail_a_message(self, *, content="the weekly reminder", channels=None, **create):
        """Queue something that cannot be delivered, with a known queuer."""
        now = int(time.time())
        msg_id = scheduled.create(
            GUILD_ID,
            channels or [QUIET_CHANNEL_ID],
            payload(content),
            now - 1,
            created_by=MEMBER_ID,
            **create,
        )
        self.tick(now)
        return msg_id

    def test_a_message_that_reached_nobody_tells_the_server_why(self):
        self.configure_log_channel()
        self.fail_a_message()

        cards = self.cards()
        self.assertEqual(len(cards), 1, self.logs.sent)
        text = cards[0]
        self.assertIn("did not go out", text)
        self.assertIn("the weekly reminder", text, "the message itself is named")
        self.assertIn("staff-only", text, "and why it was refused")
        self.assertIn("#staff-only", text, "and the channel it was meant for")
        self.assertIn(f"<@{MEMBER_ID}>", text, "the person who queued it is tagged")
        self.assertIn("Nothing else will be sent", text, "and what happens next")
        # It may ping that one person, and nobody else.
        allowed = self.logs.sent[0]["allowed_mentions"]
        self.assertEqual([user.id for user in allowed.users], [MEMBER_ID])
        self.assertFalse(allowed.roles)
        self.assertFalse(allowed.everyone)

    def test_a_message_that_arrived_is_not_news(self):
        self.configure_log_channel()
        now = int(time.time())
        scheduled.create(GUILD_ID, [CHANNEL_ID], payload("it went out"), now - 1)
        self.tick(now)
        self.assertEqual(len(self.standard.sent), 1)
        self.assertEqual(self.cards(), [], "a delivered message is visible where it landed")

    def test_a_repeat_that_failed_once_says_when_it_tries_again(self):
        self.configure_log_channel()
        msg_id = self.fail_a_message(repeat_minutes=1440)

        text = self.cards()[0]
        self.assertIn("did not go out", text)
        self.assertIn("tries again", text)
        self.assertIn("failure 1 of 3", text)
        self.assertEqual(scheduled.get(GUILD_ID, msg_id)["status"], "pending", "still queued")

    def test_a_repeat_that_gives_up_is_reported_as_stopped(self):
        self.configure_log_channel()
        msg_id = self.fail_a_message(repeat_minutes=1440)
        # Due again with one failure to spare, so this run is the one that ends it.
        due = int(time.time()) - 1
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE scheduled_messages SET fail_streak = ?, status = 'pending', "
                "run_at = ?, claimed_at = NULL, last_run_at = NULL WHERE id = ?",
                (scheduled.FAIL_LIMIT - 1, due, msg_id),
            )
            conn.commit()
        finally:
            conn.close()
        self.logs.sent.clear()
        self.tick(int(time.time()))

        text = self.cards()[0]
        self.assertIn("has been stopped", text)
        self.assertIn("will not try again", text)
        self.assertEqual(scheduled.get(GUILD_ID, msg_id)["status"], "failed")

    def test_a_partial_send_names_the_channel_that_refused_it(self):
        self.configure_log_channel()
        self.fail_a_message(channels=[CHANNEL_ID, QUIET_CHANNEL_ID])

        cards = self.cards()
        self.assertEqual(len(cards), 1, "most of it arrived, which is exactly the case to report")
        self.assertIn("Part of a scheduled message was refused", cards[0])
        self.assertIn("staff-only", cards[0])
        self.assertEqual(len(self.standard.sent), 1)

    def test_a_partial_send_of_a_repeat_does_not_claim_a_failure(self):
        self.configure_log_channel()
        now = int(time.time())
        msg_id = scheduled.create(
            GUILD_ID, [CHANNEL_ID, QUIET_CHANNEL_ID], payload("both of them"), now - 1,
            repeat_minutes=1440, created_by=MEMBER_ID,
        )
        self.tick(now)

        text = self.cards()[0]
        self.assertIn("stays queued and posts again", text)
        self.assertNotIn("failure 0 of", text, "no failure happened to count")
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["fail_streak"], 0)

    def test_a_skipped_run_of_a_repeat_does_not_count_as_this_run_s_failure(self):
        """A missed run keeps the streak as it was — the card must not imply otherwise."""
        self.configure_log_channel()
        now = int(time.time())
        msg_id = scheduled.create(
            GUILD_ID, [CHANNEL_ID], payload("good morning"), now - 2 * 86400,
            repeat_minutes=1440, created_by=MEMBER_ID,
        )
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE scheduled_messages SET fail_streak = 2 WHERE id = ?", (msg_id,)
            )
            conn.commit()
        finally:
            conn.close()
        self.tick(now)

        text = self.cards()[0]
        self.assertIn("keeps its slot and posts again", text)
        self.assertNotIn("failure 2 of 3", text, "this run was skipped, not failed")
        self.assertIn("Failures recorded: 2 of 3", text)
        self.assertEqual(scheduled.get(GUILD_ID, msg_id)["fail_streak"], 2)

    def test_a_run_that_was_too_late_to_send_is_still_reported(self):
        self.configure_log_channel()
        now = int(time.time())
        scheduled.create(
            GUILD_ID, [CHANNEL_ID], payload("good morning"), now - 2 * 86400,
            created_by=MEMBER_ID,
        )
        self.tick(now)

        text = self.cards()[0]
        self.assertIn("was skipped", text)
        self.assertIn("catch-up window", text)

    def test_a_generated_message_with_nothing_to_say_is_not_news(self):
        self.configure_log_channel()
        now = int(time.time())
        scheduled.create(
            GUILD_ID, [CHANNEL_ID], payload(None), now - 1,
            generator="weekly_digest", preview="Weekly server digest",
            created_by=MEMBER_ID,
        )
        self.tick(now)
        self.assertEqual(self.cards(), [], "a quiet week is not a failure to report")

    def test_a_send_interrupted_by_a_restart_is_reported_as_that(self):
        self.configure_log_channel()
        now = int(time.time())
        msg_id = scheduled.create(
            GUILD_ID, [CHANNEL_ID], payload("half sent?"), now - 1, created_by=MEMBER_ID
        )
        scheduled.claim(msg_id, now)
        self.tick(now + scheduled.STALE_CLAIM_SECONDS + 1)

        text = self.cards()[0]
        self.assertIn("interrupted by a restart", text)
        self.assertIn("check the channel", text)

    def test_an_interrupted_repeat_says_it_posts_again_rather_than_re_queue(self):
        """"Queue it again" would be wrong: a repeat keeps its next slot regardless."""
        self.configure_log_channel()
        now = int(time.time())
        msg_id = scheduled.create(
            GUILD_ID, [CHANNEL_ID], payload("standup"), now - 1,
            repeat_minutes=1440, created_by=MEMBER_ID,
        )
        scheduled.claim(msg_id, now)
        self.tick(now + scheduled.STALE_CLAIM_SECONDS + 1)

        text = self.cards()[0]
        self.assertIn("interrupted by a restart", text)
        self.assertIn("still queued and posts again", text)
        self.assertIn("cancel it if part of it already arrived", text)

    def test_a_queuer_the_cache_has_never_seen_is_still_named(self):
        """The Members intent is off, so a name is not something to assume."""
        self.configure_log_channel()
        self.guild.members = [m for m in self.guild.members if m.id != MEMBER_ID]
        self.fail_a_message()

        text = self.cards()[0]
        self.assertIn(f"<@{MEMBER_ID}>", text)
        self.assertNotIn("()", text, "an absent name must not leave an empty parenthesis")

    def test_the_dm_fallback_asks_discord_for_a_member_the_cache_has_not_met(self):
        """"The person who queued it is told" has to be true, not merely likely."""
        stranger = _Tellable(GONE_MEMBER_ID, "unseen", "Unseen")
        self.guild.fetchable[GONE_MEMBER_ID] = stranger
        now = int(time.time())
        scheduled.create(
            GUILD_ID, [QUIET_CHANNEL_ID], payload("nobody cached me"), now - 1,
            created_by=GONE_MEMBER_ID,
        )
        self.tick(now)

        self.assertEqual(len(stranger.dms), 1)
        self.assertIn("did not go out", _card_text(stranger.dms[0]))

    def test_with_no_log_channel_the_person_who_queued_it_is_told(self):
        tellable = _Tellable(MEMBER_ID, "raj", "RAJ")
        self.guild.members = [
            tellable if member.id == MEMBER_ID else member for member in self.guild.members
        ]
        self.fail_a_message()

        self.assertEqual(self.logs.sent, [])
        self.assertEqual(len(tellable.dms), 1, "the one person who asked for it is told")
        text = _card_text(tellable.dms[0])
        self.assertIn("did not go out", text)
        self.assertIn("staff-only", text)

    def test_a_card_is_sent_once_and_not_on_every_tick(self):
        self.configure_log_channel()
        self.fail_a_message()
        self.assertEqual(len(self.cards()), 1)
        self.tick(int(time.time()) + 120)
        self.tick(int(time.time()) + 240)
        self.assertEqual(len(self.cards()), 1, "one failure, one card")

    def test_a_failure_long_enough_to_be_refused_still_makes_a_sendable_card(self):
        """Every value in the card is text an error produced.

        Discord refuses an over-long field, an empty one, or a sixth of a kind — and
        a card that is refused is a failure nobody is told about, which is the whole
        thing this exists to prevent.
        """
        self.configure_log_channel()
        msg_id = self.fail_a_message()
        row = scheduled.get(GUILD_ID, msg_id)
        embed = self.keep_alive._schedule_alert_embed(
            "failed", row, self.keep_alive._schedule_view(self.guild, row), "discord said: " * 400
        )
        payload = embed.to_dict()

        self.assertLessEqual(len(payload.get("title") or ""), 256)
        self.assertLessEqual(len((payload.get("footer") or {}).get("text") or ""), 2048)
        self.assertEqual(len(payload["fields"]), 5)
        for field in payload["fields"]:
            self.assertLessEqual(len(field["name"]), 256, field["name"])
            self.assertTrue(field["value"], f"Discord refuses an empty '{field['name']}'")
            self.assertLessEqual(len(field["value"]), 1024, field["name"])
        # The message itself and the person who queued it are both still readable.
        values = "\n".join(field["value"] for field in payload["fields"])
        self.assertIn("the weekly reminder", values)
        self.assertIn(f"<@{MEMBER_ID}>", values)

    def test_a_card_that_cannot_be_delivered_does_not_break_the_tick(self):
        self.configure_log_channel()
        import discord

        self.logs.send_error = discord.Forbidden(
            SimpleNamespace(status=403, reason="Missing Permissions"), "nope"
        )
        now = int(time.time())
        scheduled.create(
            GUILD_ID, [QUIET_CHANNEL_ID], payload("quiet"), now - 2, created_by=GONE_MEMBER_ID
        )
        scheduled.create(GUILD_ID, [CHANNEL_ID], payload("still goes out"), now - 1)

        done = self.tick(now)

        self.assertEqual([item["status"] for item in done], ["failed", "sent"])
        self.assertEqual(len(self.standard.sent), 1, "the next message still went out")
        self.assertEqual(scheduled.get(GUILD_ID, 1)["status"], "failed")


class ScheduleTemplateCase(unittest.TestCase):
    """The page has to carry the controls, and they have to be the right ones."""

    @staticmethod
    def render(**overrides):
        context = {
            "schedule_content_choices": [
                {"value": "", "label": "Message I write", "blurb": ""},
                *[
                    {"value": name, "label": entry["label"], "blurb": entry["blurb"]}
                    for name, entry in digest.GENERATORS.items()
                ],
            ],
            "schedule_repeat_choices": [
                {"minutes": minutes, "label": label}
                for minutes, label in scheduled.REPEAT_CHOICES
            ],
            "schedule_max_pending": scheduled.MAX_PENDING_PER_GUILD,
            "schedule_catchup_hours": scheduled.MISS_CATCHUP_SECONDS // 3600,
            "server_now": 1_700_000_000,
        }
        context.update(overrides)
        return render_dashboard(**context)

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

    def test_an_immediate_send_is_labelled_as_one_not_as_a_schedule(self):
        """"once" beside a message that already went out reads as a queue, not a record."""
        out = self._schedule_js(
            "console.log(JSON.stringify(["
            "scheduleOrigin({source: 'now'}),"
            "scheduleOrigin({source: 'schedule', repeat_minutes: 1440, repeat: 'Daily'}),"
            "scheduleOrigin({repeat_minutes: 0, repeat: 'Once'})" "]));"
        )
        self.assertEqual(out[0]["text"], "sent now")
        self.assertIn("now", out[0]["className"])
        self.assertNotEqual(out[0]["className"], out[1]["className"])
        self.assertEqual(out[1]["text"], "Daily")
        self.assertEqual(out[2]["text"], "once")

    def test_the_history_says_it_holds_immediate_sends_too(self):
        self.assertIn("sent now or on a schedule", self.render())

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

    def test_the_content_choice_is_offered_from_the_generators_that_exist(self):
        html = self.render()
        self.assertIn('id="composer-content"', html)
        self.assertIn('<option value="" data-blurb="">Message I write</option>', html)
        self.assertIn(
            '<option value="weekly_digest" data-blurb="Messages, voice time', html
        )
        self.assertIn('"weekly_digest": { label: "Weekly server digest"', html)

    def test_generated_content_disables_the_box_it_would_have_been_typed_into(self):
        """A generator's text does not exist yet, so the page must not imply that
        whatever is in the message box will be part of it."""
        html = self.render()
        for line in (
            "composerEls.body.disabled = generated;",
            "composerEls.embed.disabled = generated;",
            "composerEls.contentHint.textContent = generated",
        ):
            self.assertIn(line, html, line)

    def test_the_page_says_a_generated_message_is_built_when_it_posts(self):
        html = self.render()
        self.assertIn("built when it posts", html)
        self.assertIn("if (msg.generator)", html)
        self.assertIn("composerEls.content.addEventListener(\"change\", applyContentMode);", html)


class GeneratorDeliveryCase(SchedulerCase):
    """A schedule whose content is built at fire time, not typed at queue time."""

    def queue_digest(self, **overrides):
        body = {
            "guild_id": GUILD_ID,
            "channel_ids": [CHANNEL_ID],
            "content": "",
            "mentions": [],
            "generator": "weekly_digest",
            "repeat_minutes": 10080,
            "run_at_ms": int((time.time() + 3600) * 1000),
        }
        body.update(overrides)
        return self._post("/api/schedule-message", body)

    def test_a_generated_message_can_be_queued_without_any_text(self):
        resp = self.queue_digest()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        row = resp.get_json()["message"]
        self.assertEqual(row["generator"], "weekly_digest")
        self.assertEqual(row["generator_label"], "Weekly server digest")
        self.assertIn("built when it posts", row["preview"])
        # Nothing was written, so there is nothing to preview and no tags to drop.
        self.assertIsNone(row["payload"].get("content"))
        self.assertEqual(resp.get_json()["ignored_mentions"], [])

    def test_an_unknown_content_source_is_refused(self):
        resp = self.queue_digest(generator="rm -rf")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unknown content source", resp.get_json()["error"])
        self.assertEqual(self.stored()["upcoming"], [])

    def test_a_generated_message_still_needs_a_channel(self):
        resp = self.queue_digest(channel_ids=[])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("at least one channel", resp.get_json()["error"])

    def test_the_tick_builds_it_when_it_posts(self):
        now = int(time.time())
        day = digest._window(now)["end"]
        self.seed_activity(
            messages=[(MEMBER_ID, CHANNEL_ID, day, 42)],
            voice=[(MEMBER_ID, self.voice.id, day, 3660)],
        )
        msg_id = scheduled.create(
            GUILD_ID, [CHANNEL_ID], {"generator": "weekly_digest"}, now - 1,
            generator="weekly_digest", repeat_minutes=10080,
        )
        done = self.tick(now)
        self.assertEqual(done[0]["status"], "sent", done[0])
        self.assertEqual(len(self.standard.sent), 1)
        posted = self.standard.sent[0]
        self.assertIsNone(posted["content"], "a digest is an embed with no text of its own")
        self.assertIn("42 messages", posted["embed"].description)
        self.assertIn("#announcements", posted["embed"].description)
        self.assertIn("RAJ", posted["embed"].description)
        self.assertIn("Meow Server", posted["embed"].title)
        # A weekly summary must never ping anyone, whoever it names in the card.
        self.assertEqual(posted["allowed_mentions"].users, [])
        self.assertEqual(posted["allowed_mentions"].roles, [])
        self.assertFalse(posted["allowed_mentions"].everyone)
        self.assertEqual(scheduled.get(GUILD_ID, msg_id)["status"], "pending",
                         "a weekly digest stays queued for next week")

    def test_a_quiet_week_is_skipped_and_the_schedule_survives(self):
        now = int(time.time())
        msg_id = scheduled.create(
            GUILD_ID, [CHANNEL_ID], {"generator": "weekly_digest"}, now - 1,
            generator="weekly_digest", repeat_minutes=10080,
        )
        done = self.tick(now)
        self.assertEqual(done[0]["status"], "skipped", done[0])
        self.assertEqual(self.standard.sent, [], "no card of zeros is posted")
        row = scheduled.get(GUILD_ID, msg_id)
        self.assertEqual(row["status"], "pending", "next week still happens")
        self.assertEqual(row["fail_streak"], 0, "a quiet week is not a failure")
        self.assertIn("nothing to report", row["last_error"])
        self.assertGreater(row["run_at"], now)

    def test_firing_a_generated_message_now_builds_it_now(self):
        now = int(time.time())
        day = digest._window(now)["end"]
        self.seed_activity(messages=[(MEMBER_ID, CHANNEL_ID, day, 7)])
        msg_id = scheduled.create(
            GUILD_ID, [CHANNEL_ID], {"generator": "weekly_digest"}, now + 3600,
            generator="weekly_digest",
        )
        resp = self._post(f"/api/scheduled/{msg_id}/fire", {"guild_id": GUILD_ID})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertFalse(data["skipped"])
        self.assertIn("7 messages", self.standard.sent[0]["embed"].description)


class DigestCase(TempDatabaseMixin, unittest.TestCase):
    """The digest itself: what it counts, and what it refuses to invent."""

    def setUp(self):
        self.use_temp_database("digest.db")
        asyncio.run(db.setup())
        self.standard = _Channel(CHANNEL_ID, "general")
        self.voice = _VoiceChannel(VOICE_CHANNEL_ID, "General Voice", category="Voice")
        self.member = _Member(MEMBER_ID, "raj", "RAJ")
        self.gone_member = _Member(GONE_MEMBER_ID, "ghost", "Ghost")
        # ``get_member`` only knows the people still in the server, which is the
        # point: a member who has left cannot be featured under a nickname.
        self.guild = _Guild(
            GUILD_ID,
            "Meow Server",
            [self.standard],
            members=[self.member],
            voice_channels=[self.voice],
        )

    def tearDown(self):
        self.release_temp_database()

    def seed(self, *, messages=(), voice=()):
        conn = db.connect()
        try:
            for uid, cid, day, count in messages:
                conn.execute(
                    "INSERT OR REPLACE INTO activity_messages "
                    "(guild_id, user_id, channel_id, date, count) VALUES (?, ?, ?, ?, ?)",
                    (GUILD_ID, uid, cid, day, count),
                )
            for uid, cid, day, seconds in voice:
                conn.execute(
                    "INSERT OR REPLACE INTO activity_voice "
                    "(guild_id, user_id, channel_id, date, seconds) VALUES (?, ?, ?, ?, ?)",
                    (GUILD_ID, uid, cid, day, seconds),
                )
            conn.commit()
        finally:
            conn.close()

    def busy_week(self):
        self.seed(
            messages=[
                (MEMBER_ID, CHANNEL_ID, "2023-11-08", 40),        # first day, inside
                (MEMBER_ID, CHANNEL_ID, "2023-11-14", 60),        # today, inside
                (GONE_MEMBER_ID, GONE_CHANNEL_ID, "2023-11-10", 20),
                (MEMBER_ID, CHANNEL_ID, "2023-11-07", 30),        # the window before
                (MEMBER_ID, CHANNEL_ID, "2023-10-31", 900),       # outside both
            ],
            voice=[
                (MEMBER_ID, VOICE_CHANNEL_ID, "2023-11-09", 3600),
                (MEMBER_ID, VOICE_CHANNEL_ID, "2023-11-07", 1800),
            ],
        )

    def test_the_window_is_seven_days_ending_today_in_utc(self):
        window = digest._window(FIXED_NOW)
        self.assertEqual(
            window,
            {
                "start": "2023-11-08",
                "end": "2023-11-14",
                "prev_start": "2023-11-01",
                "prev_end": "2023-11-07",
            },
        )

    def test_it_counts_the_window_it_claims_and_nothing_else(self):
        self.busy_week()
        data = digest.server_activity(GUILD_ID, FIXED_NOW)
        self.assertEqual(data["messages"], 120, "40 + 60 + 20; not the 900 outside")
        self.assertEqual(data["voice_seconds"], 3600)
        self.assertEqual(data["prev_messages"], 30, "the week before, for comparison")
        self.assertEqual(data["prev_voice_seconds"], 1800)
        self.assertEqual(data["members"], 2, "message senders and voice sitters together")

    def test_the_busiest_channels_and_talkers_are_ranked(self):
        self.busy_week()
        data = digest.server_activity(GUILD_ID, FIXED_NOW)
        self.assertEqual(
            data["message_channels"], [(CHANNEL_ID, 100), (GONE_CHANNEL_ID, 20)]
        )
        self.assertEqual(data["voice_channels"], [(VOICE_CHANNEL_ID, 3600)])
        self.assertEqual(
            data["members_by_messages"], [(MEMBER_ID, 100), (GONE_MEMBER_ID, 20)]
        )
        self.assertEqual(data["member_voice"].get(MEMBER_ID), 3600)

    def test_a_quiet_week_has_nothing_to_say(self):
        payload, reason = digest.build("weekly_digest", self.guild, FIXED_NOW)
        self.assertIsNone(payload, "no card of zeros")
        self.assertIn("nothing to report", reason)
        self.assertIn("2023-11-14", reason)

    def test_the_card_names_live_channels_members_and_durations(self):
        self.busy_week()
        payload, reason = digest.build("weekly_digest", self.guild, FIXED_NOW)
        self.assertEqual(reason, "")
        card = payload["embed"]
        self.assertIn("Meow Server", card["title"])
        self.assertIn("last 7 days", card["title"])
        body = card["description"]
        self.assertIn("**120 messages**", body)
        # The same spelling `/activity` prints: an hour is "1.00 h" there too.
        self.assertIn("**1.00 h**", body)
        self.assertIn("#general — 100 messages", body)
        self.assertIn("🔊 General Voice — 1.00 h", body)
        self.assertIn("RAJ — 100 messages · 1.00 h", body)
        self.assertIn("2023-11-08 → 2023-11-14 (UTC)", body)
        self.assertLess(len(body), 4096, "an embed description has a hard limit")

    def test_a_channel_that_is_gone_is_still_counted_and_still_named(self):
        self.busy_week()
        body = digest.build("weekly_digest", self.guild, FIXED_NOW)[0]["embed"]["description"]
        self.assertIn("since deleted", body)
        self.assertIn("20 messages", body)

    def test_a_member_who_has_left_counts_but_is_not_named(self):
        self.busy_week()
        body = digest.build("weekly_digest", self.guild, FIXED_NOW)[0]["embed"]["description"]
        self.assertIn("**120 messages**", body, "their activity still counts")
        self.assertNotIn("Ghost", body, "but they cannot be featured by a name we no longer have")
        self.assertIn("RAJ", body)

    def test_the_comparison_is_like_for_like_or_says_it_cannot_be(self):
        self.busy_week()
        body = digest.build("weekly_digest", self.guild, FIXED_NOW)[0]["embed"]["description"]
        self.assertIn("+300% vs the week before", body)
        conn = db.connect()
        try:
            conn.execute("DELETE FROM activity_messages WHERE date < '2023-11-08'")
            conn.commit()
        finally:
            conn.close()
        body = digest.build("weekly_digest", self.guild, FIXED_NOW)[0]["embed"]["description"]
        self.assertIn("no comparable week before this one", body)

    def test_a_generated_message_never_pings_anyone(self):
        self.busy_week()
        payload, _ = digest.build("weekly_digest", self.guild, FIXED_NOW)
        self.assertEqual(payload["users"], [])
        self.assertEqual(payload["roles"], [])
        self.assertFalse(payload["everyone"])
        self.assertIsNone(payload["content"])

    def test_an_unknown_generator_is_a_reason_not_a_crash(self):
        payload, reason = digest.build("no_such_thing", self.guild, FIXED_NOW)
        self.assertIsNone(payload)
        self.assertIn("no longer exists", reason)
        self.assertEqual(digest.label("no_such_thing"), "")

    def test_a_generator_that_throws_is_a_reason_not_a_crash(self):
        def explode(guild, now):
            raise RuntimeError("boom")

        with mock.patch.dict(
            digest.GENERATORS, {"boom": {"label": "Boom", "blurb": "", "build": explode}}
        ):
            payload, reason = digest.build("boom", self.guild, FIXED_NOW)
        self.assertIsNone(payload)
        self.assertIn("could not build boom", reason.lower())

    def test_the_tracker_and_the_digest_spell_things_the_same_way(self):
        """One spelling of a duration, used by both, or a reader concludes the two
        views disagree about the same seconds."""
        from cogs import tracker

        for seconds in (0, 45, 60, 125, 3600, 3725, 86_400):
            self.assertEqual(tracker._fmt_voice(seconds), digest.human_voice(seconds), seconds)
        self.assertEqual(tracker._fmt_count(1_234_567), digest.human_count(1_234_567))

    def test_the_read_only_touches_this_guild(self):
        self.seed(messages=[(MEMBER_ID, CHANNEL_ID, "2023-11-14", 5)])
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO activity_messages "
                "(guild_id, user_id, channel_id, date, count) VALUES (?, ?, ?, ?, ?)",
                (OTHER_GUILD_ID, MEMBER_ID, CHANNEL_ID, "2023-11-14", 500),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(digest.server_activity(GUILD_ID, FIXED_NOW)["messages"], 5)
        self.assertEqual(digest.server_activity(OTHER_GUILD_ID, FIXED_NOW)["messages"], 500)
