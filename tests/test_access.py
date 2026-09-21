"""The Access page's two subjects: the session ledger and the guard windows.

Both replace something that could only be *inferred* before, and the claims here
are about the difference between inferring and knowing:

* **A session could not be ended.** Flask's session is a signed cookie, so the
  only way out was rotating ``FLASK_SECRET_KEY`` and restarting — which signs out
  the browser you are doing it from. A row per session makes \"get that device
  out\" an action: it is checked on the next request, and the test that matters is
  that the *old cookie still works as a cookie* while the session behind it is
  refused.
* **A window could not be seen.** \"Too many failed attempts\" was a sentence with
  no subject — the operator could not tell their own mistyped passkey from a
  stranger probing the form, or a proxy naming everybody as one client. The
  reading is now a number with an address beside it, and the tests below are
  mostly about the one thing a reading must never do: report a zero for a
  question nobody asked.
"""

import contextlib
import sqlite3
import time
import unittest
from unittest import mock

import keep_alive
from tests.test_dashboard import CSRF, _DashboardFixture
from utils import db, ratelimit, sessions

GUILD_ID = 777001


@contextlib.contextmanager
def frozen(module, when: float):
    """Move one module's clock without moving anybody else's.

    ``ratelimit.time`` *is* the ``time`` module, so patching through it would
    freeze SQLite, the logger and the test runner at once. Replacing the module's
    own reference is the same trick the code allows for on purpose: the clock is
    a module attribute rather than a captured function.
    """
    with mock.patch.object(module, "time", mock.Mock(time=lambda: when)):
        yield


def _clear_ledger_backoff():
    """Undo the module-level state a failure test leaves behind."""
    sessions._write_failed_at = 0.0
    sessions._forget_cache()
    sessions._last_seen_write.clear()


class TheWindowIsOneImplementation(unittest.TestCase):
    """The arithmetic itself, driven directly rather than through a door."""

    def setUp(self):
        self.limiter = ratelimit.RateLimiter("test", 60, 3)
        self.addCleanup(self.limiter.clear_all)

    def test_it_allows_exactly_the_limit_and_then_refuses(self):
        self.assertTrue(all(self.limiter.allow("a") for _ in range(3)))
        self.assertFalse(self.limiter.allow("a"))
        self.assertEqual(self.limiter.remaining("a"), 0)

    def test_keys_do_not_share_a_window(self):
        for _ in range(3):
            self.limiter.allow("a")
        self.assertTrue(self.limiter.allow("b"))
        self.assertEqual(self.limiter.remaining("b"), 2)

    def test_time_is_what_reopens_the_door(self):
        """A sliding window, not a counter that has to be reset by somebody."""
        for _ in range(3):
            self.limiter.allow("a")
        self.assertFalse(self.limiter.allow("a"))
        with frozen(ratelimit, time.time() + 61):
            self.assertTrue(self.limiter.allow("a"))

    def test_recording_and_testing_are_separate(self):
        """The sign-in form asks how many are left without spending one."""
        for _ in range(2):
            self.limiter.record("a")
        self.assertEqual(self.limiter.remaining("a"), 1)
        self.assertEqual(self.limiter.remaining("a"), 1)
        self.limiter.record("a")
        self.assertEqual(self.limiter.remaining("a"), 0)

    def test_clearing_a_key_forgets_only_that_key(self):
        self.limiter.record("a")
        self.limiter.record("b")
        self.limiter.clear("a")
        self.assertEqual(self.limiter.remaining("a"), 3)
        self.assertEqual(self.limiter.remaining("b"), 2)

    def test_the_snapshot_names_who_is_blocked(self):
        for _ in range(3):
            self.limiter.record("203.0.113.9")
        self.limiter.record("198.51.100.4")
        picture = self.limiter.snapshot()
        self.assertEqual(picture["name"], "test")
        self.assertEqual(picture["limit"], 3)
        self.assertEqual(picture["tracked"], 2)
        self.assertEqual([row["key"] for row in picture["blocked"]], ["203.0.113.9"])
        self.assertEqual(picture["blocked"][0]["attempts"], 3)
        self.assertGreater(picture["blocked"][0]["seconds_left"], 0)

    def test_a_refusal_is_counted_where_it_happens(self):
        for _ in range(3):
            self.limiter.allow("a")
        self.limiter.allow("a")
        self.limiter.note_refused()
        picture = self.limiter.snapshot()
        self.assertEqual(picture["refused"], 2)
        self.assertIsNotNone(picture["last_refused_at"])

    def test_a_scanner_walking_addresses_does_not_grow_the_map_forever(self):
        """The cap sweeps keys that have gone quiet, and says so.

        It is deliberately *not* a hard limit that evicts a live key: dropping a
        bucket to make room would let whoever fills the map choose which window to
        reset, including their own. What it bounds is the ordinary shape of this
        traffic — a scanner walking a range, one address after another — where the
        keys that came before are stale by the time the map is large.
        """
        small = ratelimit.RateLimiter("small", 60, 1, max_keys=4)
        base = time.time()
        for index in range(40):
            with frozen(ratelimit, base + index * 61):
                small.allow(f"10.0.0.{index}")
        self.assertLessEqual(len(small.buckets), 5)

    def test_an_expired_bucket_is_forgotten_rather_than_kept(self):
        self.limiter.record("a")
        with frozen(ratelimit, time.time() + 61):
            self.assertEqual(self.limiter.snapshot()["tracked"], 0)


class TheLedgerIsTheAuthority(unittest.TestCase):
    """``utils/sessions`` on its own, against a throwaway database."""

    def setUp(self):
        import tempfile

        self.addCleanup(_clear_ledger_backoff)
        self.tmp = tempfile.mkdtemp(prefix="meow-sessions-")
        self.original = db.DB_PATH
        db.DB_PATH = f"{self.tmp}/sessions.db"
        self.addCleanup(self._restore)

    def _restore(self):
        db.DB_PATH = self.original
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_started_session_is_live_and_a_revoked_one_is_not(self):
        sid = sessions.start("global", label="test", ip="127.0.0.1", agent="curl/8")
        self.assertTrue(sid)
        self.assertTrue(sessions.live(sid))
        self.assertTrue(sessions.revoke(sid))
        self.assertFalse(sessions.live(sid))
        # Ending it twice is not an error, and not a second change.
        self.assertFalse(sessions.revoke(sid))

    def test_a_session_with_no_id_is_respected(self):
        """The cookie that was signed before this ledger existed still works."""
        self.assertTrue(sessions.live(None))
        self.assertTrue(sessions.live(""))

    def test_an_id_the_ledger_does_not_know_is_refused(self):
        """A restore that lost the rows must not be answered \"yes\"."""
        self.assertFalse(sessions.live("not-a-session-this-server-minted"))

    def test_an_unreadable_database_does_not_sign_everybody_out(self):
        """The other half of that rule, and the reason both are here.

        A host whose disk hiccuped cannot know that a session was revoked — and
        answering \"no\" would sign out every browser in the deployment, which is a
        worse failure than the one it would be preventing.
        """
        sid = sessions.start("global")
        with mock.patch.object(sessions, "_connect", side_effect=sqlite3.Error("nope")):
            sessions._forget_cache()
            self.assertTrue(sessions.live(sid))
            picture = sessions.reading(current_sid=sid)
        self.assertFalse(picture["available"])
        self.assertEqual(picture["rows"], [])
        self.assertTrue(picture["because"])

    def test_a_read_only_host_cannot_record_a_session_but_still_signs_in(self):
        with mock.patch.object(sessions, "_connect", side_effect=sqlite3.Error("nope")):
            sessions._forget_cache()
            self.assertEqual(sessions.start("global"), "")
            self.assertFalse(sessions.writable())

    def test_the_reading_masks_the_id_it_lists(self):
        sid = sessions.start("global", label="R A J", ip="203.0.113.9", agent="Firefox/140")
        row = sessions.reading(current_sid=sid)["rows"][0]
        self.assertTrue(row["current"])
        self.assertNotIn(sid, row["sid"])
        self.assertEqual(row["agent"], "Firefox")
        self.assertEqual(row["ip"], "203.0.113.9")
        self.assertEqual(row["state"], "live")
        self.assertEqual(row["label"], "R A J")

    def test_revoking_everything_spares_the_caller(self):
        mine = sessions.start("global", label="mine")
        theirs = sessions.start("user", label="theirs")
        closed = sessions.revoke_all(keep=mine)
        self.assertEqual(closed, 1)
        self.assertTrue(sessions.live(mine))
        self.assertFalse(sessions.live(theirs))

    def test_an_expired_session_is_not_live(self):
        sid = sessions.start("global")
        with frozen(sessions, time.time() + sessions.IDLE_TTL_SECONDS + 5):
            sessions._forget_cache()
            self.assertFalse(sessions.live(sid))

    def test_a_used_session_slides_its_deadline(self):
        """An open console is used on every poll, so it must not expire mid-shift."""
        sid = sessions.start("global")
        first = sessions.reading()["rows"][0]["expires_at"]
        with frozen(sessions, time.time() + 120):
            sessions._forget_cache()
            sessions._last_seen_write.clear()
            self.assertTrue(sessions.live(sid))
        self.assertGreater(sessions.reading()["rows"][0]["expires_at"], first)


class TheAccessPageIsTheOperators(_DashboardFixture):
    """The endpoint and its two writes, through the real app."""

    def setUp(self):
        super().setUp()
        _clear_ledger_backoff()
        keep_alive._SIGN_IN.clear_all()
        keep_alive._PASSKEY.clear_all()
        self.addCleanup(_clear_ledger_backoff)

    def sid_of(self, client=None):
        with (client or self.client).session_transaction() as session:
            return session.get("sid")

    def test_the_page_needs_the_operator_scope(self):
        denied = self.client.get("/api/console/access")
        self.assertEqual(denied.status_code, 401)
        self.sign_in(scope="guild")
        self.assertEqual(self.client.get("/api/console/access").status_code, 403)
        self.sign_in(scope="global")
        self.assertEqual(self.client.get("/api/console/access").status_code, 200)

    def test_it_reports_the_three_windows_and_the_two_switches(self):
        self.sign_in(scope="global")
        body = self.api("GET", "/api/console/access").get_json()
        self.assertEqual(set(body["limits"]), {"sign-in", "passkey", "send"})
        self.assertEqual(body["limits"]["passkey"]["limit"], keep_alive.PASSKEY_MAX_ATTEMPTS)
        self.assertIn("passkey_enabled", body["doors"])
        self.assertIn("visitor_address_signed", body["doors"])
        self.assertTrue(body["read"]["sessions"]["ok"])
        self.assertGreaterEqual(body["process"]["uptime_seconds"], 0)

    def test_a_blocked_window_is_named_with_the_address_in_it(self):
        """The reading that turns a 429 sentence into an answer."""
        self.sign_in(scope="global")
        # A passkey has to be configured for a wrong one to count: with none set,
        # the endpoint refuses without recording an attempt at all, because there
        # is nothing to guess.
        with mock.patch.object(keep_alive, "ADMIN_PASSKEY", "secret-passkey"):
            for _ in range(keep_alive.PASSKEY_MAX_ATTEMPTS):
                self.client.post(
                    "/api/set-module", headers={"X-Admin-Passkey": "wrong"}, json={}
                )
        body = self.api("GET", "/api/console/access").get_json()
        blocked = body["limits"]["passkey"]["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["attempts"], keep_alive.PASSKEY_MAX_ATTEMPTS)
        self.assertTrue(blocked[0]["key"])

    def test_a_signed_in_session_is_recorded(self):
        """Every door writes a row, and the row describes the session it made."""
        # The form's token belongs to the session the page was rendered for: the
        # route validates CSRF against that session, and a POST with no page
        # behind it is a 403 by design.
        with self.client.session_transaction() as session:
            session["csrf_token"] = CSRF
        with mock.patch.object(keep_alive, "ADMIN_PASSKEY", "secret-passkey"):
            response = self.client.post(
                "/owner/login",
                data={"passkey": "secret-passkey", "csrf_token": CSRF},
            )
        self.assertEqual(response.status_code, 302)
        sid = self.sid_of()
        self.assertTrue(sid)
        row = sessions.reading(current_sid=sid)["rows"][0]
        self.assertEqual(row["scope"], "global")
        self.assertEqual(row["label"], "passkey")
        self.assertTrue(row["current"])

    def test_a_session_signed_in_before_the_ledger_is_adopted(self):
        """Otherwise deploying this signs out every open console."""
        self.sign_in(scope="global")
        self.assertIsNone(self.sid_of())
        self.api("GET", "/api/console/access")
        self.assertTrue(self.sid_of())
        self.assertEqual(sessions.reading()["live"], 1)

    def test_revoking_ends_that_session_on_its_next_request(self):
        """The claim the whole module exists for: the cookie still parses."""
        self.sign_in(scope="global")
        self.api("GET", "/api/console/access")  # adopt, so a row exists
        victim = self.sid_of()
        row_id = sessions.reading()["rows"][0]["id"]

        other = keep_alive.app.test_client()
        with other.session_transaction() as session:
            session["authenticated"] = True
            session["csrf_token"] = CSRF
            session["auth_scope"] = "global"
        ended = other.post(
            "/api/console/access/revoke", json={"id": row_id}, headers={"X-CSRF-Token": CSRF}
        )
        self.assertEqual(ended.status_code, 200)
        self.assertTrue(ended.get_json()["revoked"])
        self.assertFalse(sessions.live(victim))

        refused = self.client.get("/api/console/access")
        self.assertEqual(refused.status_code, 401)
        self.assertFalse(refused.get_json()["success"])

    def test_signing_in_again_replaces_a_session_the_ledger_refused(self):
        """The advice the console gives — "reload to sign in again" — has to work.

        A revoked browser that signed in a second time used to *keep* the sid it
        was holding, because a session that already had one was left alone: the
        sign-in succeeded, the row was still refused, and every request went on
        being answered 401 with a reload that could never fix it. Clearing cookies
        was the only way out, and nothing said so. A sign-in is exactly the moment
        to mint a new session, and a live sid is still adopted rather than
        replaced — the two halves are asserted together so neither can be "fixed"
        by removing the other.
        """
        self.sign_in(scope="global")
        self.api("GET", "/api/console/access")
        dead = self.sid_of()
        self.assertTrue(dead)
        sessions.revoke(dead)
        self.assertEqual(self.client.get("/api/console/access").status_code, 401)

        fresh = self._sign_in_with_passkey()
        self.assertNotEqual(fresh, dead)
        self.assertTrue(sessions.live(fresh))
        self.assertEqual(self.client.get("/api/console/access").status_code, 200)
        self.assertEqual(sessions.reading(current_sid=fresh)["live"], 1)

    def test_signing_in_twice_leaves_one_live_row(self):
        """The other half, and why the doors *retire* the session they replace.

        Every door clears the session on purpose before signing somebody in — a
        sign-in that keeps the previous session id is a session-fixation bug — but
        clearing only forgets the id: the row stays, still believed, with no
        browser attached to it, so one person signing in twice appeared in the
        console's own list twice. The row is closed before the cookie is dropped,
        and the list describes browsers rather than sign-ins.
        """
        self.sign_in(scope="global")
        self.api("GET", "/api/console/access")
        first = self.sid_of()
        second = self._sign_in_with_passkey()
        self.assertNotEqual(second, first)
        self.assertFalse(sessions.live(first))
        picture = sessions.reading()
        self.assertEqual(picture["live"], 1)
        # Two rows and one live session: the ended one is kept deliberately, so an
        # operator can see what was signed in rather than only what is.
        self.assertEqual(
            sorted(row["state"] for row in picture["rows"]), ["live", "revoked"]
        )

    def _sign_in_with_passkey(self):
        """The owner's own form, and the sid the session holds afterwards."""
        with self.client.session_transaction() as session:
            session["csrf_token"] = CSRF
        with mock.patch.object(keep_alive, "ADMIN_PASSKEY", "secret-passkey"):
            response = self.client.post(
                "/owner/login",
                data={"passkey": "secret-passkey", "csrf_token": CSRF},
            )
        self.assertEqual(response.status_code, 302)
        return self.sid_of()

    def test_it_refuses_to_end_the_browser_asking(self):
        self.sign_in(scope="global")
        self.api("GET", "/api/console/access")
        row_id = sessions.reading()["rows"][0]["id"]
        response = self.api("POST", "/api/console/access/revoke", {"id": row_id})
        self.assertEqual(response.status_code, 400)
        self.assertIn("this browser's own session", response.get_json()["error"])
        self.assertTrue(sessions.live(self.sid_of()))

    def test_revoking_a_row_that_is_already_over_is_reported_as_such(self):
        self.sign_in(scope="global")
        self.api("GET", "/api/console/access")

        # Somebody else's row, ended out from under the page. A revocation that
        # changes nothing is a different answer from one that did, and the page
        # says which it got rather than a bare acknowledgement.
        other = keep_alive.app.test_client()
        with other.session_transaction() as session:
            session["authenticated"] = True
            session["csrf_token"] = CSRF
            session["auth_scope"] = "global"
        other.get("/api/console/access")
        theirs = self.sid_of(other)
        row_id = next(
            row["id"] for row in sessions.reading()["rows"] if row["sid"] == sessions._mask(theirs)
        )
        sessions.revoke_row(row_id)

        response = self.api("POST", "/api/console/access/revoke", {"id": row_id})
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["revoked"])
        self.assertIn("already", body["note"] if "note" in body else body["error"])

    def test_revoking_all_spares_this_browser_and_ends_the_others(self):
        self.sign_in(scope="global")
        self.api("GET", "/api/console/access")
        mine = self.sid_of()

        other = keep_alive.app.test_client()
        with other.session_transaction() as session:
            session["authenticated"] = True
            session["csrf_token"] = CSRF
            session["auth_scope"] = "global"
        other.get("/api/console/access")
        theirs = self.sid_of(other)
        self.assertNotEqual(mine, theirs)

        ended = self.api("POST", "/api/console/access/revoke", {"all": True})
        self.assertEqual(ended.get_json()["revoked"], 1)
        self.assertTrue(sessions.live(mine))
        self.assertFalse(sessions.live(theirs))
        self.assertEqual(other.get("/api/console/access").status_code, 401)

    def test_a_revocation_needs_the_csrf_token(self):
        self.sign_in(scope="global")
        self.api("GET", "/api/console/access")
        response = self.client.post("/api/console/access/revoke", json={"all": True})
        self.assertEqual(response.status_code, 403)

    def test_signing_out_closes_the_row(self):
        self.sign_in(scope="global")
        self.api("GET", "/api/console/access")
        sid = self.sid_of()
        self.assertTrue(sessions.live(sid))
        self.client.post("/logout", headers={"X-CSRF-Token": CSRF})
        row = sessions.reading()["rows"][0]
        self.assertEqual(row["state"], "revoked")


if __name__ == "__main__":
    unittest.main()
