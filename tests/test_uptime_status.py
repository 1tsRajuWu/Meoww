"""UptimeRobot's view of this deployment, as the bot reads and reports it.

The bot has always been able to say whether it is connected *right now* — the
gateway line in ``/health``, the pill on the landing page. It has never been able
to say whether it has been connected *lately*, which is the question a person
asking "is this reliable?" is actually asking. That history already exists:
UptimeRobot is already watching this host (its probe is why ``/`` answers a
monitor with a single line instead of a page), and its read-only API can say
what it has seen.

The tests here are about the three ways this can lie:

* **"Everything is down" when nothing was asked.** No key, a refused key, no
  network — each is its own answer, and none of them is an outage.
* **Naming something on a public endpoint.** ``/health`` is unauthenticated and
  already publishes counts rather than names for the configuration audit and the
  site check; a monitor's name is usually the address it watches, so it belongs
  behind the dashboard's sign-in with the rows.
* **Reporting a *state* as *news*.** A monitor that is down and was down is a
  standing condition; a card every five minutes about the same outage is how an
  alert channel gets muted, which is what costs the one card that mattered its
  reader.

Everything network-facing is injected, so the whole file runs offline.
"""
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import keep_alive
from tests.test_dashboard import CSRF, GUILD_ID, _DashboardFixture, make_bot, page_href
from utils import uptime_status


#: A payload shaped like ``getMonitors`` really answers. Two monitors in two
#: states on purpose: a parser that reads one and drops the other is the defect
#: this pins.
ANSWER = {
    "stat": "ok",
    "pagination": {"offset": 0, "limit": 50, "total": 2},
    "monitors": [
        {
            "id": 803978677,
            "friendly_name": "Meow Bot",
            "url": "https://meow-discord-bot.onrender.com",
            "type": 1,
            "status": 2,
            "average_response_time": "157",
            "custom_uptime_ratio": "100.000-99.990-99.970",
        },
        {
            "id": 803978678,
            "friendly_name": "Public site",
            "url": "https://meow-bot.pages.dev",
            "type": 1,
            "status": 0,
            "average_response_time": "0",
            "custom_uptime_ratio": "0.000-0.000-0.000",
        },
    ],
}


class TheParser(unittest.TestCase):
    """Someone else's JSON, made total."""

    def test_a_real_answer_becomes_rows(self):
        snapshot = uptime_status.parse(ANSWER)
        self.assertEqual(snapshot["total"], 2)
        self.assertEqual([row["name"] for row in snapshot["monitors"]], ["Meow Bot", "Public site"])
        up, paused = snapshot["monitors"]
        self.assertTrue(up["up"])
        self.assertFalse(up["down"])
        self.assertEqual(up["label"], "up")
        self.assertEqual(up["response_ms"], 157.0)
        self.assertEqual(up["ratios"], {"1d": 100.0, "7d": 99.99, "30d": 99.97})
        self.assertTrue(paused["paused"])
        self.assertEqual(snapshot["up"], 1)
        self.assertEqual(snapshot["paused"], 1)
        # A paused monitor is not evidence about uptime, so its zeroes are not
        # allowed to drag the headline figure down with them.
        self.assertEqual(snapshot["ratio_30d"], 99.97)
        self.assertEqual(snapshot["status"], "attention")
        self.assertIn("paused", snapshot["detail"])

    def test_a_down_monitor_outranks_everything_else(self):
        answer = {
            "stat": "ok",
            "monitors": [
                dict(ANSWER["monitors"][0]),
                {"id": 9, "friendly_name": "Public site", "status": 9, "url": "https://site.test"},
            ],
        }
        snapshot = uptime_status.parse(answer)
        self.assertEqual(snapshot["status"], "down")
        self.assertEqual(snapshot["down"], 1)
        self.assertIn("Public site", snapshot["detail"])

    def test_a_silent_account_is_not_an_outage(self):
        snapshot = uptime_status.parse({"stat": "ok", "monitors": []})
        self.assertEqual(snapshot["status"], "empty")
        self.assertEqual(snapshot["total"], 0)
        self.assertIn("no monitors", snapshot["detail"])

    def test_a_name_that_is_missing_is_not_a_crash(self):
        """Anything can arrive; a row without a name still has to read as one."""
        snapshot = uptime_status.parse({
            "stat": "ok",
            "monitors": [{"id": 1, "status": 2}, {"id": 2, "friendly_name": "  ", "status": 1}],
        })
        self.assertEqual(snapshot["total"], 2)
        self.assertEqual(snapshot["monitors"][0]["name"], "Unnamed monitor")
        self.assertEqual(snapshot["monitors"][1]["label"], "waiting for its first check")
        self.assertEqual(snapshot["status"], "attention")

    def test_a_payload_that_is_not_what_it_should_be_is_survived(self):
        for payload in ({}, {"monitors": None}, {"monitors": ["nonsense", 5]},
                        {"monitors": [{"id": 1, "status": "not a number"}]}):
            with self.subTest(payload=payload):
                snapshot = uptime_status.parse(payload)
                self.assertIn(snapshot["status"], ("empty", "attention"))


class WithoutAKey(unittest.TestCase):
    """"Off" and "down" are different sentences, and only one is alarming."""

    def setUp(self):
        uptime_status.reset()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("UPTIMEROBOT_API_KEY", None)
        os.environ.pop("UPTIMEROBOT_STATUS_PAGE", None)

    def tearDown(self):
        self._env.stop()
        uptime_status.reset()

    def test_an_absent_key_asks_nothing_and_says_so(self):
        calls = []

        def fetch(payload):
            calls.append(payload)
            raise AssertionError("no key means no request")

        snapshot = uptime_status.check(fetch=fetch)
        self.assertEqual(calls, [], "the key's absence is answerable without a request")
        self.assertEqual(snapshot["status"], "unconfigured")
        self.assertIn("UPTIMEROBOT_API_KEY", snapshot["detail"])
        self.assertIn("UPTIMEROBOT_STATUS_PAGE", snapshot["detail"])

    def test_the_snapshot_before_the_first_pass_invents_nothing(self):
        snapshot = uptime_status.snapshot()
        self.assertEqual(snapshot["status"], "unconfigured")
        self.assertEqual(snapshot["monitors"], [])

    def test_health_publishes_the_state_rather_than_a_count_of_zeroes(self):
        block = uptime_status.health_json()
        self.assertEqual(block["status"], "unconfigured")
        self.assertEqual(block["monitors"], 0)
        self.assertNotIn("uptime_30d", block)

    def test_nothing_was_read_so_no_time_of_reading_is_published(self):
        """`at` is set by every attempt, including the one that never asked.

        So an age computed from it unconditionally said a check had happened
        seconds ago on a deployment where nothing had been asked at all — and
        the panel put that sentence directly beside "Not connected: set
        UPTIMEROBOT_API_KEY", which is the one line that makes a reader conclude
        the integration is working when it is not.
        """
        block = uptime_status.health_json()
        self.assertNotIn("checked_seconds_ago", block)
        snapshot = uptime_status.snapshot()
        self.assertIsNone(snapshot["age_seconds"])
        self.assertEqual(snapshot["age_text"], "not connected")


class WithAKey(unittest.TestCase):
    def setUp(self):
        uptime_status.reset()
        self._env = mock.patch.dict(
            os.environ, {"UPTIMEROBOT_API_KEY": "ur-read-only-key"}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        uptime_status.reset()

    def test_the_key_is_sent_with_an_api_key_and_the_windows_asked_for(self):
        seen = {}

        def fetch(payload):
            seen.update(payload)
            return ANSWER

        uptime_status.check(fetch=fetch)
        self.assertEqual(seen["api_key"], "ur-read-only-key")
        self.assertEqual(seen["format"], "json")
        self.assertEqual(seen["custom_uptime_ratios"], "1-7-30")

    def test_a_network_failure_is_not_reported_as_an_outage(self):
        def fetch(payload):
            raise TimeoutError("the read timed out")

        snapshot = uptime_status.check(fetch=fetch)
        self.assertEqual(snapshot["status"], "unreachable")
        self.assertIn("TimeoutError", snapshot["detail"])
        self.assertEqual(snapshot["monitors"], [])

    def test_a_refusal_names_the_reason_the_api_gave(self):
        snapshot = uptime_status.check(fetch=lambda payload: {
            "stat": "fail",
            "error": {"message": "api_key not found."},
        })
        self.assertEqual(snapshot["status"], "refused")
        self.assertIn("api_key not found.", snapshot["detail"])

    def test_a_failed_refresh_keeps_the_rows_it_already_had(self):
        """Rows are kept; the *status* is the failure, and says how old they are.

        A panel that emptied itself the moment UptimeRobot had a bad minute
        would read as "you have no monitors" — which is a much worse thing to
        show an operator than a last-known reading with its age on it.
        """
        uptime_status.record(uptime_status.check(fetch=lambda payload: ANSWER))
        refused = uptime_status.check(fetch=lambda payload: (_ for _ in ()).throw(OSError("down")))
        uptime_status.record(refused)
        kept = uptime_status.snapshot()
        self.assertEqual(kept["status"], "unreachable")
        self.assertTrue(kept["stale"])
        self.assertEqual(kept["total"], 2)
        self.assertEqual(len(kept["monitors"]), 2)
        self.assertIn("last reading", kept["detail"])

    def test_a_kept_reading_is_aged_from_when_it_was_read(self):
        """The rows and their timestamp travel together.

        Keeping the rows but stamping them with the time of the *attempt* that
        failed aged somebody else's data: the panel called a twenty-minute-old
        reading "read just now" in one line and "the last reading" in the next.
        """
        import time

        old = int(time.time()) - 1200
        uptime_status.record(uptime_status.check(fetch=lambda payload: ANSWER, now=old))
        uptime_status.record(
            uptime_status.check(
                fetch=lambda payload: (_ for _ in ()).throw(OSError("down")),
                now=int(time.time()),
            )
        )
        kept = uptime_status.snapshot()
        self.assertEqual(kept["at"], old, "the rows kept a timestamp they were not read at")
        self.assertEqual(kept["age_text"], "read 20 minutes ago")
        # The same worder, not raw seconds: these two lines sit next to each
        # other, and "taken 1200 seconds ago" beside "read 20 minutes ago" is one
        # fact spelled twice — the defect the age line itself was fixed for.
        self.assertIn("taken 20 minutes ago", kept["detail"])
        self.assertNotIn("1200 seconds", kept["detail"])
        self.assertEqual(uptime_status.health_json()["checked_seconds_ago"], 1200)

    def test_health_publishes_counts_and_one_ratio_but_never_a_name(self):
        uptime_status.record(uptime_status.check(fetch=lambda payload: ANSWER))
        block = uptime_status.health_json()
        self.assertEqual(block["status"], "attention")
        self.assertEqual(block["monitors"], 2)
        self.assertEqual(block["up"], 1)
        self.assertIn("uptime_30d", block)
        blob = str(block)
        self.assertNotIn("Meow Bot", blob)
        self.assertNotIn("onrender.com", blob)
        self.assertNotIn("pages.dev", blob)

    def test_nothing_is_read_while_a_request_is_being_answered(self):
        """The routes read the snapshot; only the loop touches the network."""
        uptime_status.record(uptime_status.check(fetch=lambda payload: ANSWER))
        with mock.patch.object(uptime_status, "_fetch", side_effect=AssertionError("no request")):
            self.assertEqual(uptime_status.snapshot()["total"], 2)
            self.assertEqual(uptime_status.health_json()["monitors"], 2)


class WhatIsWorthSaying(unittest.TestCase):
    """A card per change, never a card per poll."""

    def setUp(self):
        uptime_status.reset()
        self._env = mock.patch.dict(os.environ, {"UPTIMEROBOT_API_KEY": "k"}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        uptime_status.reset()

    def _record(self, status):
        snapshot = uptime_status.parse({"stat": "ok", "monitors": [
            {"id": 1, "friendly_name": "Meow Bot", "status": status, "url": "https://x.test"},
        ]})
        uptime_status.record(snapshot)
        return snapshot

    def test_the_first_reading_is_a_baseline_rather_than_news(self):
        """Nothing has changed yet: there is nothing to have changed *from*."""
        first = self._record(9)
        self.assertIsNone(uptime_status.headline(first))

    def test_a_standing_condition_is_not_news(self):
        self._record(2)
        self._record(9)
        again = self._record(9)
        self.assertIsNone(
            uptime_status.headline(again),
            "the second poll of the same outage must not repeat itself",
        )

    def test_going_down_is_news(self):
        self._record(2)
        down = self._record(9)
        message = uptime_status.headline(down)
        self.assertIsNotNone(message)
        self.assertIn("Meow Bot", message)
        self.assertIn("down", message)

    def test_coming_back_is_news_once(self):
        self._record(2)
        self._record(9)
        back = self._record(2)
        self.assertIn("answering again", uptime_status.headline(back))
        self.assertIsNone(uptime_status.headline(back))

    def test_health_publishes_no_names_while_down_either(self):
        self._record(2)
        self._record(9)
        self.assertNotIn("Meow Bot", str(uptime_status.health_json()))


class HowOldTheReadingSounds(unittest.TestCase):
    """The wording of the age line, which is read far more than it is measured.

    Every one of these was wrong in the first draft, and the singular was found
    by opening the preview harness rather than by reading the function: it said
    "read 1 minutes ago" on a panel whose whole job is to be trusted.
    """

    def test_one_of_anything_is_singular(self):
        self.assertEqual(uptime_status.age_text(1), "read 1 second ago")
        # The bands are seconds below 90s, so the singular *minute* starts there —
        # 60s is deliberately still "60 seconds", which reads better than
        # "1 minute" for a reading taken a moment ago.
        self.assertEqual(uptime_status.age_text(90), "read 1 minute ago")
        # …and the singular *hour* starts at the hour band, 90 minutes.
        self.assertEqual(uptime_status.age_text(5400), "read 1 hour ago")

    def test_more_than_one_is_plural(self):
        self.assertEqual(uptime_status.age_text(0), "read 0 seconds ago")
        self.assertEqual(uptime_status.age_text(45), "read 45 seconds ago")
        self.assertEqual(uptime_status.age_text(120), "read 2 minutes ago")
        self.assertEqual(uptime_status.age_text(7200), "read 2 hours ago")

    def test_the_boundaries_between_units(self):
        self.assertEqual(uptime_status.age_text(89), "read 89 seconds ago")
        self.assertEqual(uptime_status.age_text(90), "read 1 minute ago")
        self.assertEqual(uptime_status.age_text(5399), "read 89 minutes ago")
        self.assertEqual(uptime_status.age_text(5400), "read 1 hour ago")

    def test_never_read_says_so_rather_than_guessing(self):
        self.assertEqual(uptime_status.age_text(None), "not read yet in this process")


class TheEndpointCarriesIt(unittest.TestCase):
    """Both surfaces, read the way the pages read them."""

    def setUp(self):
        uptime_status.reset()
        self._env = mock.patch.dict(os.environ, {"UPTIMEROBOT_API_KEY": "k"}, clear=False)
        self._env.start()
        uptime_status.record(uptime_status.check(fetch=lambda payload: ANSWER))
        self.client = keep_alive.app.test_client()

    def tearDown(self):
        self._env.stop()
        uptime_status.reset()

    def test_health_carries_the_uptime_block(self):
        body = self.client.get("/health").get_json()
        self.assertIn("uptime", body)
        self.assertEqual(body["uptime"]["monitors"], 2)
        self.assertEqual(body["uptime"]["status"], "attention")
        # Counts and a ratio, no names — the rule the endpoint already follows
        # for `config` and `site_guard`.
        self.assertNotIn("Public site", str(body))

    def test_the_rows_need_a_session(self):
        response = self.client.get("/api/uptime")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("Public site", response.get_data(as_text=True))

    def test_a_signed_in_session_gets_the_rows(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["auth_scope"] = "global"
            session["csrf_token"] = CSRF
        response = self.client.get("/api/uptime", headers={"X-CSRF-Token": CSRF})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual([row["name"] for row in data["monitors"]], ["Meow Bot", "Public site"])
        self.assertEqual(data["monitors"][0]["ratios"]["30d"], 99.97)


class ThePanel(_DashboardFixture):
    """What the dashboard actually draws, from the same snapshot."""

    def setUp(self):
        super().setUp()
        uptime_status.reset()
        self._env = mock.patch.dict(os.environ, {"UPTIMEROBOT_API_KEY": "k"}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(uptime_status.reset)

    def _sign_in(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["auth_scope"] = "global"
            session["guild_id"] = GUILD_ID
            session["csrf_token"] = CSRF

    def test_the_panel_renders_the_monitors_the_snapshot_holds(self):
        uptime_status.record(uptime_status.check(fetch=lambda payload: ANSWER))
        self._sign_in()
        page = self.client.get(page_href("status")).get_data(as_text=True)
        self.assertIn("System status", page)
        self.assertIn("Meow Bot", page)
        self.assertIn("Public site", page)
        self.assertIn("99.97%", page)
        self.assertIn("30d", page)

    def test_the_panel_explains_itself_when_there_is_no_key(self):
        """Not an empty list, which reads as "no monitors"."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UPTIMEROBOT_API_KEY", None)
            uptime_status.reset()
            self._sign_in()
            page = self.client.get(page_href("status")).get_data(as_text=True)
        self.assertIn("UPTIMEROBOT_API_KEY", page)
        self.assertIn("Not connected", page)

    def test_the_panel_does_not_claim_a_reading_it_never_made(self):
        """The panel contradicted itself in the one line a reader trusts.

        With no key, the age written beside the heading was "read 0 seconds
        ago" — computed from an attempt that was answered by *this process*
        rather than by UptimeRobot — while the box underneath said "Not
        connected". Both sentences were on screen at once, and the second one a
        reader believes is the one that looks like a measurement.
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UPTIMEROBOT_API_KEY", None)
            uptime_status.reset()
            self._sign_in()
            page = self.client.get(page_href("status")).get_data(as_text=True)
        self.assertNotIn("read 0 seconds ago", page)
        self.assertNotIn("read 1 second ago", page)
        self.assertIn("not connected", page)

    def test_the_age_of_the_reading_is_words_not_epoch_seconds(self):
        """The panel's first version rendered `at` as an age: "read 1789848173
        seconds ago". Age is a fact about *now*, so it is computed when it is
        read rather than stored with the answer.

        The assertion is about that sentence rather than about the number
        appearing nowhere on the page: the page legitimately carries the
        current epoch in ``SERVER_NOW``, the seed the schedule hints use to
        correct for a wrong device clock.
        """
        uptime_status.record(uptime_status.check(fetch=lambda payload: ANSWER))
        self._sign_in()
        page = self.client.get(page_href("status")).get_data(as_text=True)
        self.assertIn("read 0 seconds ago", page)
        at = uptime_status.snapshot()["at"]
        self.assertNotIn(f"read {at} seconds ago", page)
        self.assertNotIn(f"read {at} minutes ago", page)

    def test_the_panel_and_the_modules_sentence_do_not_both_name_the_setting(self):
        """One box, two renderers, and they disagreed after a Refresh.

        The script's empty state appended a chip naming the variable to paste,
        and the server's ``detail`` already names it in a sentence — so the page
        printed it once on load and twice the moment somebody pressed Refresh,
        the second copy stranded after a full stop. The name belongs to the
        module that composes the sentence, so the template must not spell it: a
        page that knows the variable name is a page that can fall out of step
        with the one that explains it.
        """
        template = Path(__file__).resolve().parents[1] / "templates" / "dashboard.html"
        source = template.read_text(encoding="utf-8")
        self.assertNotIn(
            "UPTIMEROBOT_API_KEY",
            source,
            "the dashboard hard-codes the uptime setting instead of rendering "
            "the sentence the module composes",
        )
        # …and the setting is still named where a reader needs it, because "do
        # not spell it here" must not become "do not say it at all". Asked with
        # no key, which is the one path that answers without a request.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UPTIMEROBOT_API_KEY", None)
            detail = uptime_status.check()["detail"]
        self.assertIn(uptime_status.ENV_KEY, detail)

    def test_the_panel_is_built_from_the_pages_own_snapshot(self):
        """The view hands the template the same block `/api/uptime` serves."""
        uptime_status.record(uptime_status.check(fetch=lambda payload: ANSWER))
        self._sign_in()
        page = self.client.get(page_href("status")).get_data(as_text=True)
        self.assertIn("Status", page)
        self.assertIn("/api/uptime", page, "the refresh button's endpoint is not on the page")

    def test_the_panel_keeps_up_without_being_asked(self):
        """The reading has an age, so the age has to move on by itself.

        This panel is rendered from a snapshot rather than fetched live, which
        is what keeps it off UptimeRobot's API — and it is also why the line
        beside the heading is *"read 2 minutes ago"*: a sentence about now,
        computed once when the page was rendered. Nothing asked again, so it
        said that for as long as the tab stayed open, and a panel whose age has
        stopped moving is indistinguishable from one that has stopped working.
        The refresh has to be a timer, and it has to read a route that answers
        from the snapshot rather than one that spends somebody's rate limit.
        """
        uptime_status.record(uptime_status.check(fetch=lambda payload: ANSWER))
        self._sign_in()
        page = self.client.get(page_href("status")).get_data(as_text=True)
        self.assertIn("setInterval", page, "nothing re-reads the reading, so its age freezes")
        self.assertIn("/api/uptime", page)


class TheConsoleKeepsUpToo(_DashboardFixture):
    """The operator console's copy of the panel, and the same complaint.

    Both control rooms draw these rows, and only one of them was ever asked
    again: the dashboard had a Refresh button while the console had a snapshot
    rendered at request time and nothing afterwards.
    """

    def setUp(self):
        super().setUp()
        uptime_status.reset()
        self._env = mock.patch.dict(os.environ, {"UPTIMEROBOT_API_KEY": "k"}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(uptime_status.reset)
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["auth_scope"] = "global"
            session["guild_id"] = GUILD_ID
            session["csrf_token"] = CSRF
        uptime_status.record(uptime_status.check(fetch=lambda payload: ANSWER))
        # The console's monitors are on its Infrastructure page, which is the page
        # that owns every reading this process holds.
        self.page = self.client.get("/owner/infrastructure").get_data(as_text=True)

    def test_the_console_shows_the_same_rows(self):
        self.assertIn("UptimeRobot monitors", self.page)
        self.assertIn("Meow Bot", self.page)
        self.assertIn("99.97%", self.page)

    def test_the_reading_is_re_read_rather_than_frozen_at_render_time(self):
        self.assertIn("/api/uptime", self.page, "the console never asks for a newer snapshot")
        self.assertIn("setInterval", self.page, "nothing re-reads the reading, so its age freezes")

    def test_the_polls_are_made_with_the_token_the_endpoint_demands(self):
        """A poll that is refused looks exactly like a panel that is not updating.

        ``/api/uptime`` is behind the sign-in *and* behind the CSRF check, so a
        timer that forgot the header would be answered 403 every thirty seconds
        while the panel sat there saying "read 2 minutes ago" — the same symptom
        this change is here to remove.
        """
        self.assertIn("X-CSRF-Token", self.page)
        self.assertIn("infra-refresh", self.page, "the manual refresh is gone")
        match = re.search(r"const CSRF_TOKEN = (\"[^\"]*\")", self.page)
        self.assertIsNotNone(match, "the console's script has no token to send")
        self.assertEqual(
            match.group(1).strip('"'),
            CSRF,
            "the token rendered into the script is not the session's, so the "
            "poll would be refused and the panel would look stuck",
        )

    def test_every_id_the_scripts_poll_reaches_for_is_on_the_page(self):
        for element_id in ("infra-monitors", "infra-seen", "infra-refresh"):
            with self.subTest(element_id=element_id):
                self.assertEqual(
                    self.page.count(f'id="{element_id}"'), 1,
                    f"`{element_id}` is missing or ambiguous, so the poll writes nowhere",
                )

    def test_the_console_does_not_spell_the_setting_beside_the_sentence(self):
        template = Path(__file__).resolve().parents[1] / "templates" / "owner.html"
        self.assertNotIn(
            "UPTIMEROBOT_API_KEY",
            template.read_text(encoding="utf-8"),
            "the console hard-codes the uptime setting instead of rendering the "
            "sentence the module composes",
        )


if __name__ == "__main__":
    unittest.main()
