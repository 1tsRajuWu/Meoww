"""What the landing page's two cards are allowed to say, and where they get it.

``utils/figures`` is the one module in this project that turns the deployment's
own counters into a page a *stranger* reads, and the page it feeds is the front
door. So the tests here are the four rules in its own docstring, in the order it
states them, plus the endpoint that serves the same payload to the published
copy on the CDN:

1. **Counts, never names.** The figures are drawn from objects that are full of
   names — a gateway holds every server's name, every member's name and every
   channel's name — and none of them may reach a payload a visitor can read.
2. **A reading that was never taken is absent, not zero.** A sleeping free-tier
   host has no gateway to ask, and a confident ``0 servers`` reads as a bot
   nobody uses rather than as a process that has not woken up.
3. **Nothing is fetched while a request is being answered.** The read is cached
   for a minute, and a cache hit and a live read have to be the same shape, or
   the page drifts depending on whether it was the first visitor of the minute.
4. **The windows belong to the tracker.** ``week`` has to mean the same seven
   days ``/activity`` and the weekly digest mean, which is why the arithmetic is
   asserted against ``utils.digest``'s rather than against a literal.
"""
import asyncio
import datetime
import json
import re
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import keep_alive
from tests.support import TempDatabaseMixin
from utils import db, digest, figures

ROOT = Path(__file__).resolve().parents[1]


def _fake_gateway(*, servers=3, members=42, systems=11, commands=90, connected=True):
    """A gateway object that is entirely names, which is the point.

    Whatever reads it may count what it finds; nothing here is allowed to carry
    one of these strings onward.
    """
    guilds = []
    for index in range(servers):
        guilds.append(mock.Mock(
            name=f"Secret Server {index}",
            member_count=members // max(servers, 1),
            channels=[mock.Mock(name=f"secret-channel-{index}")],
            members=[mock.Mock(name=f"secret-member-{index}")],
        ))
    gateway = mock.Mock()
    gateway._has_started = True
    gateway.is_ready.return_value = connected
    gateway.guilds = guilds
    gateway.user.name = "Meow Bot"
    return gateway


class TheWindowsBelongToTheTracker(unittest.TestCase):
    """Rule 4. These ids are the tracker's, not this module's invention."""

    def test_the_week_is_the_digests_own_seven_days(self):
        """Both are "today and the six days before it", and both are inclusive.

        The arithmetic is repeated in `figures` rather than imported — the way
        `utils.digest` already repeats it — so the only thing stopping the two
        from drifting apart quietly is this comparison.
        """
        now = int(datetime.datetime(2026, 9, 20, 12, 0, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(
            figures.period_start("week", day=datetime.date(2026, 9, 20)),
            digest._window(now)["start"],
            "`figures` and the weekly digest disagree about which seven days are 'last 7 days'",
        )

    def test_each_window_includes_today(self):
        """A day counted from midnight yesterday is a different number."""
        day = datetime.date(2026, 9, 20)
        self.assertEqual(figures.period_start("today", day=day), "2026-09-20")
        self.assertEqual(figures.period_start("week", day=day), "2026-09-14")
        self.assertEqual(figures.period_start("month", day=day), "2026-08-22")

    def test_all_reaches_further_back_than_any_other_window(self):
        """Ordered by construction, so no window can be the narrowest one.

        `all` is a floor rather than a date the tracker would write, and the
        tracker prunes what is older than its retention anyway — so the floor's
        only job is to be *below* everything that can still be in the table.
        """
        day = datetime.date(2026, 9, 20)
        starts = [
            figures.period_start(period, day=day)
            for period in ("all", "month", "week", "today")
        ]
        self.assertEqual(starts, sorted(starts), "a wider window starts later than a narrower one")
        self.assertLess(figures.EPOCH_FLOOR, figures.period_start("month", day=day))

    def test_every_period_the_page_offers_can_be_computed(self):
        for period, label, days in figures.PERIODS:
            with self.subTest(period=period):
                self.assertTrue(figures.period_start(period))
                self.assertEqual(figures.period_start(period) <= figures.period_start("today"), True)
                self.assertTrue(label, "a window with no label is a window nobody can read")

    def test_an_unknown_period_is_refused_rather_than_defaulted(self):
        """Silently answering "today" for a typo would be a wrong number, not an error."""
        with self.assertRaises(KeyError):
            figures.period_start("yesterday")


class TheCountsAreCounts(TempDatabaseMixin, unittest.TestCase):
    """Rules 1 and 2, over a database holding known rows."""

    def setUp(self):
        self.use_temp_database("figures.db")
        asyncio.run(db.setup())
        figures.reset()

    def tearDown(self):
        figures.reset()
        self.release_temp_database()

    def _plant(self, rows):
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.executemany(
                "INSERT INTO activity_messages (guild_id, user_id, channel_id, date, count)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def test_the_totals_are_the_rows_that_are_there(self):
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        self._plant([
            (1, 10, 100, today, 5),
            (1, 11, 100, today, 7),
            (2, 10, 200, today, 3),
        ])
        totals = figures.tracker_totals(period="today")
        self.assertEqual(totals["messages"], 15)
        self.assertEqual(totals["message_channels"], 2, "two channels were used, not three")
        self.assertEqual(totals["people"], 2, "two people spoke")
        self.assertEqual(totals["messages_text"], digest.human_count(15))

    def test_a_row_outside_the_window_is_not_in_it(self):
        old = (datetime.date.today() - datetime.timedelta(days=40)).isoformat()
        self._plant([(1, 10, 100, old, 99)])
        self.assertEqual(figures.tracker_totals(period="week")["messages"], 0)
        self.assertEqual(figures.tracker_totals(period="all")["messages"], 99)

    def test_a_sleeping_gateway_yields_an_absence_and_not_a_zero(self):
        """Rule 2. The four gateway figures, and the two the registry always knows."""
        payload = figures.deployment(gateway=None)
        for key in ("servers", "members", "systems", "commands"):
            with self.subTest(key=key):
                self.assertIsNone(payload[key], f"{key} was answered without a gateway")
                self.assertEqual(payload["text"][key], "", f"{key} was spelled as a number anyway")
        self.assertFalse(payload["connected"])

    def test_a_gateway_that_answers_is_counted_and_spelled(self):
        payload = figures.deployment(gateway={
            "servers": 3, "members": 42, "systems": 11, "commands": 90, "connected": True,
        })
        self.assertEqual(payload["servers"], 3)
        self.assertEqual(payload["text"]["members"], digest.human_count(42))
        self.assertTrue(payload["connected"])

    def test_a_figure_that_arrives_as_junk_is_absent_rather_than_crashing(self):
        """The gateway is mocked in half the tests that build one of these."""
        payload = figures.deployment(gateway={
            "servers": "many", "members": object(), "systems": None, "commands": 90,
        })
        self.assertIsNone(payload["servers"])
        self.assertIsNone(payload["members"])
        self.assertEqual(payload["commands"], 90)

    def test_no_name_from_the_gateway_reaches_the_payload(self):
        """Rule 1, tested against a gateway made of nothing but names."""
        gateway = mock.Mock()
        gateway._has_started = True
        gateway.is_ready.return_value = True
        gateway.guilds = [
            mock.Mock(
                name="Secret Server",
                member_count=7,
                members=[mock.Mock(name="secret-member")],
                channels=[mock.Mock(name="secret-channel")],
            )
        ]
        counts = keep_alive._gateway_counts()
        payload = figures.deployment(gateway=counts)
        written = json.dumps(payload)
        for leak in ("Secret Server", "secret-member", "secret-channel"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, written, "a name reached a page any visitor can read")

    def test_the_counts_a_gateway_produces_are_only_counts(self):
        """`_gateway_counts` is the boundary, so it is where this is checked."""
        with mock.patch.object(keep_alive, "BOT_INSTANCE", _fake_gateway()):
            counts = keep_alive._gateway_counts()
        self.assertEqual(
            set(counts), {"servers", "members", "systems", "commands", "connected"},
            "the payload grew a key nobody has decided may be public",
        )
        for key, value in counts.items():
            with self.subTest(key=key):
                self.assertIn(
                    type(value), (int, bool, type(None)),
                    f"{key} is a {type(value).__name__}, and only counts belong here",
                )


class WithoutTheTrackersTables(TempDatabaseMixin, unittest.TestCase):
    """What the front door says when there is no database to count in.

    This is not a hypothetical shape: ``bot.db`` is git-ignored, so the Pages
    build clones a checkout with no database in it and renders the **real app's**
    pages to make the published copy. The first real publish answered
    ``500`` for ``/`` — *"no such table: activity_messages"* — because a
    reporting read raised, and the site build refuses to ship a page that
    answered 500, so the deploy failed and the site kept serving the commit
    before it. A marketing page must not be brought down by a table it only
    wanted a count from.

    The rule the module already states for a sleeping host applies at its
    extreme: a reading that was never taken is **absent**, not zero. The tiles
    say so with an em dash, and ``/api/figures`` fills the same card in from a
    host that does have a database.
    """

    def setUp(self):
        # The path exists as a name and nothing else: no `db.setup()`, so no
        # schema — which is exactly what a fresh clone of this repository has.
        self.use_temp_database("never-set-up.db")
        figures.reset()

    def tearDown(self):
        figures.reset()
        self.release_temp_database()

    def test_a_window_with_no_table_to_read_reports_absent_rather_than_raising(self):
        totals = figures.tracker_totals(period="week")
        for key in ("messages", "voice_seconds", "message_channels", "voice_channels", "people"):
            with self.subTest(key=key):
                self.assertIsNone(totals[key], f"{key} was reported as a measurement")
        for key in ("messages_text", "voice_text", "channels_text", "people_text"):
            with self.subTest(key=key):
                self.assertEqual(totals[key], "", f"{key} was spelled as a figure")

    def test_the_payload_still_has_every_window_the_page_offers(self):
        """A card with no windows is a card whose buttons do nothing."""
        payload = figures.snapshot(gateway=None)
        self.assertEqual([p["id"] for p in payload["periods"]], [p[0] for p in figures.PERIODS])
        for period in payload["periods"]:
            self.assertIsNone(period["messages"])

    def test_the_front_door_renders_and_says_it_has_not_read_them(self):
        """The build's own question, asked of the build's own client.

        A shell has no Chromium to check what the tile *looks* like, so the two
        things that are checkable are checked: the page answers, and the figure
        it prints for a reading it never took is not a zero.
        """
        response = keep_alive.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        for key in ("messages_text", "voice_text", "people_text", "channels_text"):
            with self.subTest(key=key):
                self.assertIn(
                    f'data-figure="activity:{key}">{keep_alive.NOT_READ}<',
                    body,
                    f"the {key} tile claims a reading the database could not give",
                )

    def test_the_script_does_not_turn_an_absent_window_into_a_zero(self):
        """The other renderer, read as the rule rather than as an instance.

        The card is painted twice — by the server above and by the page's own
        script — and the script's fallback was `"0"`, so an unread window was
        spelled as a measurement by whichever renderer ran last. Both fall back
        to the same em dash the control figures already used.
        """
        source = (ROOT / "static" / "figures.js").read_text(encoding="utf-8")
        activity = re.search(r'\["messages_text".*?\]\.forEach\(.*?\n\s*\}\);', source, re.S)
        self.assertIsNotNone(activity, "the activity tiles moved; re-point this test")
        self.assertNotIn(
            '"0"', activity.group(0),
            "an absent window is spelled as a zero by the page's own script",
        )
        self.assertIn("\\u2014", activity.group(0))


class TheReadingIsCachedForAMinute(unittest.TestCase):
    """Rule 3, and the shape promise that keeps a cache hit from reading differently."""

    def setUp(self):
        figures.reset()

    def tearDown(self):
        figures.reset()

    def test_one_read_serves_every_visitor_of_the_minute(self):
        calls = []
        real = figures.deployment

        def counted(**kwargs):
            calls.append(kwargs)
            return real(**kwargs)

        with mock.patch.object(figures, "deployment", counted):
            first = figures.snapshot(now=1000.0)
            second = figures.snapshot(now=1000.0 + figures.CACHE_SECONDS - 1)
            third = figures.snapshot(now=1000.0 + figures.CACHE_SECONDS)
        self.assertIs(first, second, "a second visitor paid for a second read")
        self.assertEqual(len(calls), 2, "the minute's floor was not honoured")
        self.assertIsNot(first, third, "a reading outlived its own cache window")

    def test_a_cached_reading_has_the_same_shape_as_a_live_one(self):
        """The defect class a hand-written stub keeps producing in this repository."""
        live = figures.snapshot(now=2000.0)
        cached = figures.snapshot(now=2000.0 + figures.CACHE_SECONDS - 1)
        self.assertEqual(
            json.dumps(live, sort_keys=True), json.dumps(cached, sort_keys=True),
            "the cached payload and the live one are not the same thing",
        )

    def test_the_snapshot_carries_the_moment_it_was_read(self):
        payload = figures.snapshot(now=3000.0)
        self.assertEqual(payload["at"], 3000, "a reader cannot tell how old the reading is")


class TheEndpoint(unittest.TestCase):
    """`/api/figures` — the published copy's way of keeping up without a rebuild."""

    def setUp(self):
        figures.reset()
        self.client = keep_alive.app.test_client()

    def tearDown(self):
        figures.reset()

    def test_it_answers_a_visitor_with_no_session(self):
        """The caller is a browser on the Pages host, which has no session here."""
        response = self.client.get("/api/figures")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            set(payload) >= {"servers", "members", "systems", "commands", "text", "periods", "at"},
            True,
            "the page's cards cannot be drawn from this",
        )
        self.assertEqual(
            [window["id"] for window in payload["periods"]],
            [period for period, _label, _days in figures.PERIODS],
        )

    def test_it_is_cached_for_the_same_minute_the_snapshot_is(self):
        """Twenty open tabs are a page being read, not twenty reads."""
        response = self.client.get("/api/figures")
        self.assertIn(
            f"max-age={figures.CACHE_SECONDS}", response.headers.get("Cache-Control", ""),
        )

    def test_the_site_may_call_it_and_a_stranger_may_not(self):
        """A refused fetch is invisible, so the allowance has to be exact."""
        allowed = "https://meow-bot.pages.dev"
        with mock.patch.object(keep_alive.site_guard, "site_url", return_value=allowed):
            permitted = self.client.get("/api/figures", headers={"Origin": allowed})
            other = self.client.get("/api/figures", headers={"Origin": "https://not-us.test"})
        self.assertEqual(permitted.headers.get("Access-Control-Allow-Origin"), allowed)
        self.assertIsNone(
            other.headers.get("Access-Control-Allow-Origin"),
            "any origin may read this deployment's figures",
        )

    def test_it_names_nothing(self):
        with mock.patch.object(keep_alive, "BOT_INSTANCE", _fake_gateway()):
            body = self.client.get("/api/figures").get_data(as_text=True)
        self.assertNotIn("Secret Server", body)


if __name__ == "__main__":
    unittest.main()
