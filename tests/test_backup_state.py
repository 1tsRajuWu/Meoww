"""The backup is the one reading whose *failure* has no symptom anywhere else.

A bot whose gateway drops stops answering. A site check that finds the wrong
directory serves the wrong page. An unset variable switches a feature off, and a
moderation log with a deleted channel says nothing to the server that lost it.
A backup that has quietly stopped does **none** of those things: it runs, it
renders, and the only copy of everybody's coins, ranks and warnings is weeks old
until the day somebody needs it and it is not there.

So this file is written as four claims about making that state visible:

1. **The reading is read, not asked.** ``cloud_sync.backup_state`` reports what
   the sync loop already recorded — no request, no write, no grace tick spent —
   because it is drawn by ``/health``, which a monitor may poll every minute. A
   health check that reaches the network is a health check that can hang while
   reporting on whether something else hangs.
2. **Silence is its own state.** An unchanged database uploads nothing, so "no
   upload for an hour" is *normal* and only "no answer for an hour" is a dead
   target. That is why the reading tracks the last time the bucket answered
   rather than only the last time a write landed.
3. **Not knowing is never a clean answer.** A process that has asked nothing of
   the bucket says ``unknown``; it does not say ``ok`` because nothing has failed
   yet — which is exactly how a backup is discovered to be weeks dead.
4. **Every surface says the same thing, and none of them can become the outage
   they report.** ``/health``, ``/api/status`` and the dashboard's Status page
   all read the one function, and a reading that raises is published as
   ``unknown`` rather than turned into a 500 for the whole service.
"""
import os
import unittest
from unittest import mock

import keep_alive
from tests.support import TempDatabaseMixin
from utils import cloud_sync as cs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: A fixed clock, so an age in a reading is a number rather than a race.
NOW = 1_800_000_000.0

#: Every global the reading consults, at the values a process that has just
#: started and has asked nothing of the bucket holds.
EMPTY = {
    "_restore_blocked_reason": None,
    "_last_refusal_reason": None,
    "_last_contact_ts": 0.0,
    "_last_upload_ts": 0.0,
    "_consecutive_upload_failures": 0,
    "_restored_from": None,
    "_restored_counts": {},
    "_restored_at": 0.0,
    "_remote_checked": False,
}


def reading(*, configured=True, **state) -> dict:
    """``backup_state`` over exactly this state, everything else empty.

    The reading's whole input is module state, which is what makes these laws
    possible without a network, a bucket or a database — and it is the fixture
    rather than the reader: a law that inherited whatever the previous test left
    behind would be asserting about the wrong process.
    """
    held = dict(EMPTY)
    held.update(state)
    for name, value in held.items():
        setattr(cs, name, value)
    url = "https://example.supabase.co" if configured else ""
    key = "service-role" if configured else ""
    with mock.patch.object(cs, "SUPABASE_URL", url), \
         mock.patch.object(cs, "SUPABASE_KEY", key):
        return cs.backup_state(now=NOW)


class _TheModuleStateIsTheInput(unittest.TestCase):
    """Laws drive the module's own memory, so each one puts it back afterwards."""

    def setUp(self):
        self._saved = {name: getattr(cs, name) for name in EMPTY}
        self.addCleanup(self._put_back)

    def _put_back(self):
        for name, value in self._saved.items():
            setattr(cs, name, value)

    def reading(self, **state) -> dict:
        return reading(**state)


class TheReadingIsReadRatherThanAsked(_TheModuleStateIsTheInput):
    """No request, no write, no grace tick — what the loop already recorded."""

    def test_a_deployment_with_no_supabase_says_there_is_no_backup(self):
        """``unconfigured`` is a state, not a crash and not a silence."""
        asked = mock.Mock(side_effect=AssertionError("the reading made a request"))
        with mock.patch.object(cs, "_storage_list_raw", asked):
            state = self.reading(configured=False)
        self.assertEqual(state["status"], "unconfigured")
        self.assertFalse(state["configured"])
        self.assertEqual(asked.call_count, 0, "it asked a bucket it does not have")

    def test_the_reading_makes_no_request_of_its_own(self):
        """The whole reason it is safe on an endpoint a monitor polls."""
        for method in ("_storage_list_raw", "_storage_upload", "_storage_download"):
            with self.subTest(method=method):
                with mock.patch.object(
                    cs, method, side_effect=AssertionError("a request left the process")
                ):
                    self.reading(_last_contact_ts=NOW - 5)

    def test_the_block_a_failed_restore_left_is_still_the_reason(self):
        """The latched reason is the one that matters most, and it always wins.

        A process that could not read the backup must not write over it, and that
        decision is made once, at startup, and never lifts by itself — so it has
        to be the answer a reader gets even if a later pass recorded something
        else entirely.
        """
        state = self.reading(
            _restore_blocked_reason="the cloud backup could not be downloaded at startup",
            _last_refusal_reason="rows fell from 252 to 100 (check 2/4)",
        )
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(
            state["refusal"], "the cloud backup could not be downloaded at startup"
        )

    def test_the_reason_the_upload_path_last_gave_is_reported(self):
        """The other half of the refusal: what a *pass* decided, not startup."""
        state = self.reading(
            _last_refusal_reason="the bucket could not be listed",
            _last_contact_ts=NOW - 600,
        )
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["refusal"], "the bucket could not be listed")

    def test_a_target_that_has_gone_quiet_is_not_reported_as_healthy(self):
        """The failure this reading exists for: a dead target on a quiet server.

        Nothing about an unchanged database fails — it uploads nothing, so there
        is no error to log and no count to fall. The last *answer* is the only
        figure that moves, and it is the one that has to be read.
        """
        state = self.reading(
            _last_contact_ts=NOW - (cs.SILENT_AFTER + 1),
            _last_upload_ts=NOW - (cs.SILENT_AFTER + 1),
        )
        self.assertEqual(state["status"], "silent")
        self.assertGreater(state["contacted_seconds_ago"], cs.SILENT_AFTER)

    def test_one_missed_tick_is_a_slow_tick_rather_than_a_dead_target(self):
        """The threshold has to outlast the loop, or a healthy target flaps."""
        state = self.reading(
            _last_contact_ts=NOW - (cs.SYNC_INTERVAL + 30),
            _last_upload_ts=NOW - (cs.SYNC_INTERVAL + 30),
        )
        self.assertEqual(
            state["status"],
            "ok",
            "a target one tick late was called dead, so this reading would alarm "
            "on a network hiccup",
        )

    def test_nothing_asked_yet_is_not_a_clean_answer(self):
        """The state that must never read as fine just because nothing failed."""
        state = self.reading()
        self.assertEqual(state["status"], "unknown")
        self.assertIsNone(state["refusal"])
        self.assertIsNone(state["uploaded_seconds_ago"])
        self.assertIsNone(state["contacted_seconds_ago"])
        self.assertFalse(state["checked"])

    def test_asked_and_unanswered_is_not_the_same_claim_as_never_asked(self):
        """Found live rather than reasoned: the deployed answer said `unknown`.

        The bucket had been asked — the startup restore had tried and got no
        answer — and the reading called it *nothing has been asked yet*, which is
        the one confusion this whole figure exists to remove: a machine quietly
        saving nothing, reporting a state that reads as *no news*.
        """
        state = self.reading(_remote_checked=True, _restore_blocked_reason=None)
        self.assertEqual(state["status"], "unreachable")
        self.assertTrue(state["checked"], "the reading knows it asked")
        self.assertIsNone(state["contacted_seconds_ago"])

    def test_an_upload_that_landed_is_ok_and_carries_its_age(self):
        state = self.reading(_last_contact_ts=NOW - 60, _last_upload_ts=NOW - 300)
        self.assertEqual(state["status"], "ok")
        self.assertEqual(state["uploaded_seconds_ago"], 300)
        self.assertEqual(state["contacted_seconds_ago"], 60)

    def test_a_target_that_answers_with_nothing_saved_yet_is_not_called_ok(self):
        """A boot that restored has an answer but no upload of its own yet."""
        state = self.reading(
            _last_contact_ts=NOW - 10,
            _restored_from="bot.db",
            _restored_counts={"economy": 17},
            _restored_at=NOW - 10,
        )
        self.assertEqual(state["status"], "waiting")

    def test_uploads_that_are_failing_are_named_before_the_target_is(self):
        """An attempted-and-refused upload is a louder answer than a quiet one."""
        state = self.reading(
            _last_contact_ts=NOW - 5,
            _last_upload_ts=NOW - 5,
            _consecutive_upload_failures=3,
        )
        self.assertEqual(state["status"], "failing")
        self.assertEqual(state["failures"], 3)


class TheReadingIsAboutAFileAndNotADatabase(_TheModuleStateIsTheInput):
    """Ages and row counts come from what the restore recorded, not from SQLite."""

    def test_a_restore_is_named_with_its_rows_and_aged_from_memory(self):
        """``/health`` may be polled every minute: it may not count rows either.

        The rows are the ones the restore *verified*, which is also the figure
        that makes a wipe spottable — so reading them live would be both slower
        and about a different database than the one this process calls home.
        """
        with mock.patch.object(
            cs, "_row_counts", side_effect=AssertionError("the reading counted rows")
        ):
            state = self.reading(
                _last_contact_ts=NOW - 5,
                _restored_from="bot.db",
                _restored_counts={"economy": 17, "user_levels": 16},
                _restored_at=NOW - 60,
            )
        self.assertEqual(state["restored_from"], "bot.db")
        self.assertEqual(state["restored_rows"], 33)
        self.assertEqual(state["restored_seconds_ago"], 60)

    def test_a_process_that_restored_nothing_says_nothing_to_age(self):
        """``0s ago`` for a restore that never happened is a claim, and a false one."""
        state = self.reading(_last_contact_ts=NOW - 5)
        self.assertIsNone(state["restored_from"])
        self.assertEqual(state["restored_rows"], 0)
        self.assertIsNone(state["restored_seconds_ago"])


class TheUploadPathRemembersItsReason(unittest.TestCase):
    """The refusal sentence used to live only in a host log nobody reads."""

    def setUp(self):
        self._saved = {
            name: getattr(cs, name)
            for name in ("_restore_blocked_reason", "_last_refusal_reason")
        }
        self.addCleanup(self._put_back)

    def _put_back(self):
        for name, value in self._saved.items():
            setattr(cs, name, value)

    def test_the_reason_a_pass_refused_is_the_one_published(self):
        reason = "the bucket could not be listed, so uploading could replace a backup"
        with mock.patch.object(cs, "upload_refusal_reason", return_value=reason):
            self.assertFalse(cs._backup_inner())
        self.assertEqual(cs._last_refusal_reason, reason)
        with mock.patch.object(cs, "SUPABASE_URL", "https://example.supabase.co"), \
             mock.patch.object(cs, "SUPABASE_KEY", "service-role"):
            self.assertEqual(cs.backup_state()["refusal"], reason)

    def test_a_pass_that_may_upload_clears_the_old_reason(self):
        """A reason that has stopped being true is worse than no reason at all."""
        cs._last_refusal_reason = "rows fell from 252 to 100 (check 2/4)"
        with mock.patch.object(cs, "upload_refusal_reason", return_value=None), \
             mock.patch.object(cs, "db_path", return_value=__file__), \
             mock.patch.object(cs, "_snapshot_bytes", return_value=None):
            self.assertFalse(cs._backup_inner())
        self.assertIsNone(cs._last_refusal_reason)


class OnlyAnAnswerFromTheBucketCounts(unittest.TestCase):
    """The heartbeat has to mean *the target answered*, or silence reads as fine."""

    class _Answer:
        status = 200

        def __init__(self, body=b"[]"):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def setUp(self):
        self._saved = (cs._last_contact_ts, cs.SUPABASE_URL, cs.SUPABASE_KEY)
        cs._last_contact_ts = 0.0
        # A real address, because `urllib.request.Request` refuses a relative one
        # before any of this — which is what an unconfigured deployment does, and
        # the state law above already covers that case.
        cs.SUPABASE_URL = "https://example.supabase.co"
        cs.SUPABASE_KEY = "service-role"
        self.addCleanup(self._put_back)

    def _put_back(self):
        cs._last_contact_ts, cs.SUPABASE_URL, cs.SUPABASE_KEY = self._saved

    def test_an_upload_that_landed_is_an_answer(self):
        with mock.patch.object(cs.urllib.request, "urlopen", return_value=self._Answer()):
            self.assertTrue(cs._storage_upload("bot.db", b"x" * 2048))
        self.assertGreater(cs._last_contact_ts, 0)

    def test_a_listing_that_came_back_is_an_answer(self):
        with mock.patch.object(cs.urllib.request, "urlopen", return_value=self._Answer()):
            self.assertEqual(cs._storage_list_raw(), [])
        self.assertGreater(cs._last_contact_ts, 0)

    def test_a_target_that_does_not_answer_is_not_a_heartbeat(self):
        """The half that matters: the one case the figure must not move for."""
        with mock.patch.object(
            cs.urllib.request, "urlopen",
            side_effect=OSError("name or service not known"),
        ):
            self.assertFalse(cs._storage_upload("bot.db", b"x" * 2048))
            self.assertIsNone(cs._storage_list_raw())
        self.assertEqual(cs._last_contact_ts, 0.0, "a failed call counted as an answer")


class TheEndpointsPublishTheSameReading(TempDatabaseMixin, unittest.TestCase):
    """``/health`` and ``/api/status`` state the backup in the same words."""

    #: The one block a reader most needs to see, in the words a failed startup
    #: restore actually records.
    BLOCKED = "the cloud backup could not be downloaded at startup"

    def setUp(self):
        self.use_temp_database("backup-endpoints.db")
        self._saved = (cs._restore_blocked_reason, cs.SUPABASE_URL, cs.SUPABASE_KEY)
        cs._restore_blocked_reason = self.BLOCKED
        # Configured, because "unconfigured" is a different state and takes
        # precedence — a deployment with no bucket cannot be blocked from it.
        cs.SUPABASE_URL = "https://example.supabase.co"
        cs.SUPABASE_KEY = "service-role"
        self.addCleanup(self._restore)
        self.client = keep_alive.app.test_client()

    def _restore(self):
        cs._restore_blocked_reason, cs.SUPABASE_URL, cs.SUPABASE_KEY = self._saved

    def test_health_publishes_the_block_and_its_reason(self):
        body = self.client.get("/health").get_json()
        self.assertIn("backup", body)
        self.assertEqual(body["backup"]["status"], "blocked")
        self.assertEqual(body["backup"]["refusal"], self.BLOCKED)
        # …and it does not fold that into the top-level status, which is about the
        # *process*: a monitor that pages on `status` must not be told the service
        # is down because nobody can write to a bucket.
        self.assertEqual(body["status"], "ok")

    def test_api_status_publishes_the_same_reading(self):
        payload = self.client.get("/api/status").get_json()
        self.assertIn("backup", payload)
        self.assertEqual(payload["backup"]["status"], "blocked")
        self.assertEqual(payload["backup"]["refusal"], self.BLOCKED)
        self.assertEqual(payload["backup"]["configured"], cs.backup_state()["configured"])

    def test_a_reading_that_cannot_be_taken_is_not_a_failure_of_the_service(self):
        """The reader must not be able to cause the outage it reports on."""
        with mock.patch.object(cs, "backup_state", side_effect=RuntimeError("boom")):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["backup"]["status"], "unknown")


class TheSpaceAnswersTheMonitoredAddressToo(unittest.TestCase):
    """The one public address a stranger watches is answered by ``app.py``.

    The Space registers its own ``/health`` handler *above* the catch-all that
    forwards to Flask, so the database's own ``/health`` — and the block this
    pass added to it — is not what a monitor polling
    ``1tsrajowo-meow.hf.space/health`` ever reads. The request is about exactly
    that address, so the answer it gives has to carry the backup too; otherwise
    the machine goes on saying ``healthy`` while the only copy of everybody's
    progress is weeks old, which is the failure the whole change exists for.

    A *source* law rather than a driven one, and the reason is named: importing
    ``app.py`` here would import Gradio and the ZeroGPU supervisor, neither of
    which is part of this environment and one of which is why the file exists.
    What a source law can hold is the shape of the answer — that it carries the
    reading, from the one reader, without losing the two fields the release check
    and the monitor read.
    """

    def handler(self) -> str:
        with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('api_route("/health"', source, "app.py no longer answers /health")
        return source.split('api_route("/health"', 1)[1].split("@demo.server_app.api_route", 1)[0]

    def test_the_monitored_answer_carries_the_backup_reading(self):
        handler = self.handler()
        self.assertIn('"backup"', handler, "the monitored /health says nothing about the backup")
        self.assertIn(
            "keep_alive.backup_reading()",
            handler,
            "the Space's answer is not the reader the dashboard's /health uses, "
            "so the two surfaces can disagree about one backup",
        )

    def test_the_two_fields_every_reader_already_depends_on_are_intact(self):
        """``status`` is what a monitor pages on and what ``ship.py`` holds a
        release to; ``service`` is the name it holds it *as*. A backup block
        added by moving either of them would trade one silent failure for two."""
        handler = self.handler()
        self.assertIn('"status": "healthy"', handler)
        self.assertIn("brand.GATEWAY_SERVICE", handler)


class TheDashboardDrawsIt(_TheModuleStateIsTheInput):
    """The Status page's card, from the one display function the view calls."""

    def display(self, **state) -> dict:
        return keep_alive._backup_display(state)

    def card(self, **state) -> str:
        from tests.test_composer import render_dashboard

        return render_dashboard(dash_page="status", backup_status=self.display(**state))

    def test_the_card_states_when_it_last_succeeded_and_why_it_stopped(self):
        """The two halves the request is about, both on the page a person opens."""
        html = self.card(
            status="blocked",
            configured=True,
            checked=True,
            refusal="the cloud backup could not be downloaded at startup",
            uploaded_seconds_ago=4200,
            contacted_seconds_ago=60,
            restored_from="bot.db",
            restored_rows=252,
            restored_seconds_ago=90,
        )
        self.assertIn("the cloud backup could not be downloaded at startup", html)
        for label in ("Last upload", "Last restore", "Target last answered"):
            self.assertIn(f"<dt>{label}</dt>", html, label)
        self.assertIn("bot.db — 252 rows", html)
        self.assertIn("70 min ago", html)
        # The alarming treatment, because this is the state a reader has to act on.
        self.assertIn("notice--bad", html)

    def test_a_quiet_target_is_drawn_as_alarming_rather_than_as_fine(self):
        html = self.card(
            status="silent",
            configured=True,
            checked=True,
            contacted_seconds_ago=cs.SILENT_AFTER + 60,
            uploaded_seconds_ago=cs.SILENT_AFTER + 60,
        )
        self.assertIn("gone quiet", html)
        # No refusal sentence: nothing was *refused*, the target simply stopped
        # answering. A card that printed one here would be inventing a reason.
        self.assertNotIn("Why it is not uploading", html)
        self.assertEqual(self.display(status="silent")["tone"], "bad")

    def test_an_unconfigured_deployment_says_the_file_is_the_only_copy(self):
        html = self.card(status="unconfigured", configured=False)
        self.assertIn("the file on disk is the only one there is", html)
        self.assertEqual(self.display(status="unconfigured")["tone"], "warn")

    def test_a_reading_that_never_happened_is_drawn_as_one(self):
        """A fresh process must not be drawn as a working backup."""
        html = self.card(status="unknown", configured=True)
        self.assertIn("Not checked yet", html)
        self.assertNotIn("Backing up", html)

    def test_every_state_the_reader_can_return_has_words_of_its_own(self):
        """A state this build has not learned to describe must not say nothing.

        The states are driven out of the reader rather than listed here, so a
        branch added to ``backup_state`` without a sentence beside it fails this
        law instead of shipping as an empty card.
        """
        driven = [
            (False, {}),
            (True, {"_restore_blocked_reason": "blocked for the test"}),
            (True, {"_last_refusal_reason": "refused by a pass", "_last_contact_ts": NOW - 1}),
            (True, {"_last_contact_ts": NOW - 5, "_consecutive_upload_failures": 2}),
            (True, {"_last_contact_ts": NOW - (cs.SILENT_AFTER + 1)}),
            (True, {"_last_contact_ts": NOW - 5, "_last_upload_ts": NOW - 5}),
            (True, {"_last_contact_ts": NOW - 5, "_restored_from": "bot.db", "_restored_at": NOW - 5}),
            (True, {"_remote_checked": True}),
            (True, {}),
        ]
        words = [self.reading(configured=on, **state)["status"] for on, state in driven]
        self.assertEqual(
            sorted(set(words)),
            [
                "blocked", "failing", "ok", "silent", "unconfigured", "unknown",
                "unreachable", "waiting",
            ],
            "the reader stopped producing one of the states this file drives",
        )
        self.assertEqual(
            set(keep_alive._BACKUP_STATES),
            set(keep_alive._BACKUP_NOTES),
            "a state is given a headline and no sentence, or the reverse",
        )
        for word in words:
            with self.subTest(state=word):
                shown = keep_alive._backup_display({"status": word})
                self.assertTrue(shown["headline"], f"{word} has no headline")
                self.assertTrue(shown["note"], f"{word} has no sentence to read")


if __name__ == "__main__":
    unittest.main()
