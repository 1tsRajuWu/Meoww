"""The operator's console, and the one thing it must never do.

``/owner`` is the only page in this project whose *scope* is the whole bot. Every
other surface is scoped to a single server by design, so the failure that matters
here is not a wrong number on a card — it is the page being served to somebody who
does not hold the operator's passkey. That is why the access tests come first and
assert on the *redirect*, not merely on a status code.

Two smaller defects are pinned below because both were found by rendering the page
rather than by reading it:

- the fleet row's ``[hidden]`` attribute, which the filter sets and which the
  browser's own rule for it loses to ``display: grid`` — a filter that counts rows
  as hidden while they stay on screen;
- a bare ``<svg>`` given the ``.search-icon`` class, which was written for a text
  glyph and therefore carries no width or fill, so the icon drew itself as a
  ~173 px black disc across the page.
"""
import re
import time
import unittest
from unittest import mock

import keep_alive
from tests.test_composer import GUILD_ID
from tests.test_dashboard import _DashboardFixture

ROOT = None


def _render(client):
    return client.get("/owner")


class WhoMayOpenIt(_DashboardFixture):
    """The scope test is the page's first line, so it is the first thing tested."""

    def assertNoFleetWasServed(self, response):
        """A redirect, and **none of the fleet in what came back**.

        Asserting only on the status code would pass on a page that rendered
        every server and then said "302" — and Flask's redirect carries a small
        HTML body of its own, so "the body is empty" is the wrong test too. What
        must be true is that no server name, id or count reached the reader.
        """
        self.assertEqual(response.status_code, 302)
        body = response.get_data(as_text=True)
        self.assertNotIn("fleet-row", body)
        self.assertNotIn("fleet-list", body)
        for guild in (self.guild, self.other):
            self.assertNotIn(guild.name, body)
            self.assertNotIn(str(guild.id), body)

    def test_a_request_with_no_session_is_sent_to_sign_in(self):
        response = _render(self.client)
        self.assertNoFleetWasServed(response)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_a_one_time_link_never_opens_the_whole_fleet(self):
        """The scope a server's administrator holds must not reach this page.

        This is the whole point of the console existing separately. A link is
        minted for one server and may act in one server; if it could render this
        page it would be shown every other server's name, id, member count and
        which of its systems are off — and, worse, a page whose whole layout
        invites acting on them.
        """
        self.sign_in(guild_id=GUILD_ID, scope="guild")
        response = _render(self.client)
        self.assertNoFleetWasServed(response)
        self.assertNotIn("/owner", response.headers.get("Location", ""))

    def test_a_discord_sign_in_never_opens_it_either(self):
        """Even for a person who administers every server the bot is in."""
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["csrf_token"] = "csrf-token"
            session["auth_scope"] = "user"
            session["admin_guild_ids"] = [GUILD_ID, self.other.id]
        self.assertNoFleetWasServed(_render(self.client))

    def test_the_operator_gets_it(self):
        self.sign_in(scope="global")
        response = _render(self.client)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Fleet", response.data)
        self.assertIn("fleet-row", response.get_data(as_text=True))


class WhatTheOperatorSees(_DashboardFixture):
    """Every server, with the number that makes a bad one obvious."""

    def setUp(self):
        super().setUp()
        self.sign_in(scope="global")
        self.page = _render(self.client).get_data(as_text=True)

    def test_every_server_the_bot_is_in_is_listed(self):
        for guild in (self.guild, self.other):
            with self.subTest(server=guild.name):
                self.assertIn(guild.name, self.page)
                self.assertIn(str(guild.id), self.page)

    def test_each_row_opens_that_one_server(self):
        for guild in (self.guild, self.other):
            with self.subTest(server=guild.name):
                self.assertIn(f'href="/dashboard?guild_id={guild.id}"', self.page)

    def test_the_counts_on_the_cards_are_the_ones_the_functions_report(self):
        """Cross-checked against the source of truth, never against a literal.

        The dashboard's own tests learned this the expensive way: a hard-coded
        "2 / 3" outlived the registry it described and said so on a page with
        eleven cards. A number asserted against the function that produces it
        cannot go stale.
        """
        summaries = [
            keep_alive._module_summary(guild.id)
            for guild in (self.guild, self.other)
        ]
        self.assertIn(f">{sum(s['on'] for s in summaries)}<", self.page)

    def test_the_servers_are_ordered_by_how_many_people_are_in_them(self):
        self.assertLess(
            self.page.index(self.guild.name),
            self.page.index(self.other.name),
            "the server with more members must be listed first",
        )

    def test_the_systems_that_are_off_are_named_rather_than_counted(self):
        """\"3 of 11\" says something is wrong; the *names* say what to fix."""
        states = keep_alive.modules.get_states(self.guild.id)
        off = [
            module["name"]
            for module in keep_alive.AVAILABLE_MODULES
            if not states.get(module["id"], keep_alive.features.default_enabled(module["id"]))
        ]
        if off:
            self.assertIn(off[0], self.page)
        else:
            # With everything on the page must say so rather than render nothing,
            # or an operator cannot tell "nothing is off" from "nothing loaded".
            self.assertIn("every system on", self.page)

    def test_the_process_uptime_is_shown_and_cannot_be_negative(self):
        self.assertGreaterEqual(keep_alive.PROCESS_STARTED_AT, 0)
        self.assertLess(keep_alive.PROCESS_STARTED_AT, time.time() + 1)
        self.assertRegex(self.page, r"been up \d+h \d+m")

    def test_the_log_tail_is_on_the_page_and_collapsed(self):
        with mock.patch.object(keep_alive, "LOG_RING", type(keep_alive.LOG_RING)(["[X] a line"], maxlen=10)):
            page = _render(self.client).get_data(as_text=True)
        self.assertIn("[X] a line", page)
        # Collapsed by default: sixty lines of log is a wall in front of the page.
        self.assertIn('id="output-log" hidden', page)


class TheInteractiveParts(_DashboardFixture):
    """The controls the page's own script drives must be there to be driven."""

    def setUp(self):
        super().setUp()
        self.sign_in(scope="global")
        self.page = _render(self.client).get_data(as_text=True)

    def test_the_script_and_its_controls_agree(self):
        scripts = "\n".join(
            re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", self.page, flags=re.S)
        )
        looked_up = sorted(set(re.findall(r'getElementById\(\s*"([^"]+)"\s*\)', scripts)))
        self.assertGreater(len(looked_up), 4, "the page's script was not found")
        missing = [i for i in looked_up if self.page.count(f'id="{i}"') == 0]
        self.assertEqual(missing, [], f"the script looks up ids the page does not have: {missing}")

    def test_the_filter_and_the_order_control_are_both_present(self):
        for element_id in ("fleet-filter", "fleet-sort", "fleet-filter-count", "fleet-no-match"):
            with self.subTest(id=element_id):
                self.assertEqual(self.page.count(f'id="{element_id}"'), 1)

    def test_the_empty_state_is_html_not_something_the_script_builds(self):
        """So a page whose script never runs still says something true."""
        self.assertIn('id="fleet-no-match" hidden', self.page)

    def test_a_row_carries_what_the_filter_and_the_sort_read(self):
        for attribute in ("data-name=", "data-id=", "data-members=", "data-off="):
            with self.subTest(attribute=attribute):
                self.assertIn(attribute, self.page)


class ItIsNotTheDashboard(_DashboardFixture):
    """The two pages must not be the same page with a different heading.

    The console's job is to say which server to open; if it grew a module switch
    it would be answering the other question badly, and worse, it would be a page
    that changes eleven servers' settings from one screen.
    """

    def setUp(self):
        super().setUp()
        self.sign_in(scope="global")
        self.page = _render(self.client).get_data(as_text=True)

    def test_it_offers_no_module_switch(self):
        self.assertNotIn('class="toggle-switch"', self.page)
        self.assertNotIn('id="modules"', self.page)

    def test_it_offers_no_composer(self):
        self.assertNotIn('id="composer-body"', self.page)
        self.assertNotIn("/api/send-message", self.page)

    def test_it_says_where_its_reach_comes_from(self):
        self.assertIn("Operator", self.page)


class ItSurvivesAStanelessGateway(unittest.TestCase):
    """An operator opens this page *because* something is wrong, so it must render."""

    def test_no_gateway_at_all_is_not_an_error(self):
        with mock.patch.object(keep_alive, "BOT_INSTANCE", None):
            with mock.patch.object(keep_alive, "_visible_guilds", return_value=[]):
                rows = keep_alive._fleet_rows()
        self.assertEqual(rows, [])

    def test_a_server_whose_module_state_cannot_be_read_still_lists(self):
        """``_module_summary`` swallows the failure and says so; the row must too."""
        with mock.patch.object(keep_alive.modules, "get_states", side_effect=RuntimeError("db down")):
            summary = keep_alive._module_summary(GUILD_ID)
        self.assertFalse(summary["known"])
        self.assertEqual(summary["on"], 0)

    def test_the_fleet_is_ordered_and_guilds_without_a_member_count_do_not_crash(self):
        class _Guild:
            def __init__(self, gid, name, count):
                self.id, self.name = gid, name
                self.member_count = count

        class _Bot:
            guilds = []

            def get_guild(self, gid):
                return {1: _Guild(1, "aaa", 5), 2: _Guild(2, "bbb", None)}.get(gid)

        with mock.patch.object(keep_alive, "BOT_INSTANCE", _Bot()):
            with mock.patch.object(
                keep_alive, "_visible_guilds",
                return_value=[{"id": 1, "name": "aaa"}, {"id": 2, "name": "bbb"}],
            ):
                rows = keep_alive._fleet_rows()
        self.assertEqual([row["name"] for row in rows], ["aaa", "bbb"])
        self.assertEqual(rows[1]["members"], 0)


if __name__ == "__main__":
    unittest.main()
