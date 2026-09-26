"""The operator console: eleven pages, one door, and a tier behind a second one.

The console is the only surface in this project that one person can open, and it
acts on *every* server rather than on one. That makes two classes of defect worth
a law each, and both of them are silent without one:

* **A page that cannot be opened by the person it is for.** The address served a
  bundled browser app until the app was archived with the rest of the pre-rebuild
  site, and then it served a template that no longer existed — a 500, for the one
  account that can reach it, in the one place nothing else notices. So the first
  group here opens every page in ``OWNER_PAGES`` and reads what came back.
* **A tier whose two doors disagree.** The four fleet-wide actions are reachable
  from the served page *and* from the JSON API, which is exactly the shape that
  drifts: one door refusing an action the other performs, or one requiring a
  reason the other does not. The page and the API now call the same functions, and
  the last group drives both with the same input to prove it.

Nothing here re-tests the operator gate end to end — ``tests/test_console.py``
owns that, and ``tests/test_dashboard.py`` owns the session's shape. What is here
is the console's own promises: every page draws, the words and the table agree,
and the only surface that writes says so before it does.
"""
from __future__ import annotations

import contextlib
import os
import re
import unittest
from unittest import mock

# The same escaper the template uses: an apostrophe leaves this process as
# `&#39;` and not `&#x27;`, so a test comparing against `html.escape` disagrees
# with the browser about exactly the pages that say *bot's*.
from markupsafe import escape

import keep_alive
from tests.test_dashboard import CSRF
from tests.test_god import PASSKEY, _GodFixture


class _ConsoleFixture(_GodFixture):
    """An operator session, the passkey set, and the console's own requests.

    The passkey door is *not* mocked away: unlocking in these tests goes through
    ``/owner/god`` with a form, which is the path the page itself offers.
    """

    def page(self, sub: str = "", query: str = ""):
        return self.client.get(f"/owner{sub}{query}", headers={"X-CSRF-Token": CSRF})

    def god_post(self, action: str, *, reason: str = "", passkey: str = "", csrf: str = CSRF):
        return self.client.post(
            "/owner/god",
            data={
                "csrf_token": csrf,
                "action": action,
                "reason": reason,
                "passkey": passkey,
            },
        )


class EveryPageDraws(_ConsoleFixture):
    """The address renders, and it renders the page that was asked for."""

    def test_every_page_in_the_table_answers(self):
        for page_id, label, path, anchor in keep_alive.OWNER_PAGES:
            with self.subTest(page=page_id):
                response = self.page("" if page_id == "overview" else f"/{page_id}")
                self.assertEqual(
                    response.status_code,
                    200,
                    f"{path} did not render — {response.get_data(as_text=True)[:200]}",
                )
                body = response.get_data(as_text=True)
                self.assertIn(anchor, body, f"{path} does not carry its own anchor")
                # The rail names every page on every page: the failure this
                # catches is a page that draws but cannot be left.
                for _id, other_label, _p, _a in keep_alive.OWNER_PAGES:
                    self.assertIn(other_label, body, f"{path} does not offer {other_label}")

    def test_the_heading_and_the_lede_are_the_servers_own_words(self):
        """A page's copy lives beside its page, and every page has some.

        Compared as the browser receives it rather than as Python holds it: an
        apostrophe leaves this process as ``&#39;``, so the test that skips the
        escaping is a test that fails on exactly the pages that say *bot's*.
        """
        for page_id, _label, _path, _anchor in keep_alive.OWNER_PAGES:
            with self.subTest(page=page_id):
                title, lede = keep_alive.OWNER_PAGE_COPY[page_id]
                body = self.page("" if page_id == "overview" else f"/{page_id}").get_data(as_text=True)
                self.assertIn(escape(title), body)
                self.assertIn(escape(lede), body)

    def test_the_copy_table_and_the_page_table_are_the_same_set(self):
        """Neither may grow without the other, which is how a page ends up blank."""
        self.assertEqual(
            {page_id for page_id, _l, _p, _a in keep_alive.OWNER_PAGES},
            set(keep_alive.OWNER_PAGE_COPY),
        )

    def test_an_address_that_names_no_page_is_a_404_rather_than_a_crash(self):
        response = self.page("/nonsense")
        self.assertEqual(response.status_code, 404)

    def test_the_console_is_operator_only(self):
        """A signed-in member who is not the operator is sent somewhere real.

        Two destinations, and they are different on purpose: a guild-scoped
        sign-in goes to the picker — nobody is dropped onto a server they did not
        choose — while somebody who *is* the owner, signed in through Discord and
        short of the passkey, is sent to the door that can raise their scope. The
        law here is the one that matters either way: not this page.
        """
        with self.client.session_transaction() as session:
            session["auth_scope"] = "guild"
        response = self.page()
        self.assertIn(response.status_code, (301, 302))
        self.assertIn(response.headers.get("Location", ""), ("/servers", "/owner/login"))


class NothingIsInline(_ConsoleFixture):
    """The console is drawn by the server, so it needs no inline script or style.

    The host's policy has no ``'unsafe-inline'``. This is asserted rather than
    assumed because a template that gained one would look perfectly fine in
    development and be a blocked request in production — on the surface whose
    actions are the most consequential in the project.
    """

    def test_no_page_carries_an_inline_script_a_style_tag_or_a_style_attribute(self):
        for page_id, _label, _path, _anchor in keep_alive.OWNER_PAGES:
            with self.subTest(page=page_id):
                body = self.page("" if page_id == "overview" else f"/{page_id}").get_data(as_text=True)
                self.assertNotIn("<style", body)
                self.assertNotIn(" style=", body)
                for tag in re.findall(r"<script[^>]*>", body):
                    self.assertIn("src=", tag, f"{page_id} carries an inline script")


class TheTierIsBehindItsOwnDoor(_ConsoleFixture):

    def test_the_god_page_draws_while_it_is_locked(self):
        body = self.page("/god").get_data(as_text=True)
        self.assertIn("Owner passkey", body)
        for action_id, label, blurb, _needs in keep_alive.GOD_ACTIONS:
            with self.subTest(action=action_id):
                self.assertIn(label, body)
                self.assertIn(escape(blurb), body)

    # The tier's state is read through a request, never straight off `session`:
    # `_god_unlocked()` reads Flask's per-request session, and calling it from a
    # test body is a `RuntimeError` rather than an answer.
    def locked(self) -> bool:
        return bool(self.get("/api/console/god").get_json()["locked"])

    def test_a_wrong_passkey_says_so_and_leaves_the_tier_locked(self):
        self.god_post("unlock", passkey="not-the-passkey")
        self.assertTrue(self.locked())
        body = self.page("/god").get_data(as_text=True)
        self.assertIn("That is not the owner passkey", body)

    def test_the_right_passkey_opens_it_and_the_page_offers_to_close_it(self):
        self.god_post("unlock", passkey=PASSKEY)
        self.assertFalse(self.locked())
        body = self.page("/god").get_data(as_text=True)
        self.assertIn("Close god level now", body)
        self.assertNotIn("That is not the owner passkey", body)

    def test_closing_it_closes_it(self):
        self.god_post("unlock", passkey=PASSKEY)
        self.god_post("lock")
        self.assertTrue(self.locked())

    def test_a_form_without_the_token_is_refused_before_anything_is_done(self):
        self.god_post("unlock", passkey=PASSKEY, csrf="")
        self.assertTrue(self.locked())
        body = self.page("/god").get_data(as_text=True)
        self.assertIn("That form had expired", body)

    def test_an_action_is_refused_while_the_tier_is_locked(self):
        before = keep_alive.pause.state(refresh=True)["paused"]
        self.god_post("pause", reason="should not happen")
        self.assertEqual(keep_alive.pause.state(refresh=True)["paused"], before)
        body = self.page("/god").get_data(as_text=True)
        self.assertIn("locked", body.lower())

    def test_an_action_while_open_does_the_thing_and_says_what_it_did(self):
        self.god_post("unlock", passkey=PASSKEY)
        self.god_post("pause", reason="checking the page's own door")
        self.assertTrue(keep_alive.pause.state(refresh=True)["paused"])
        body = self.page("/god").get_data(as_text=True)
        # The notice is the reader's own sentence about the state it just set,
        # and the page's banner is the same function read again — so the two
        # cannot disagree, and either one contains the word.
        self.assertIn("paus", body.lower())
        # And the door closes behind it without signing anybody out.
        self.god_post("resume")
        self.assertFalse(keep_alive.pause.state(refresh=True)["paused"])


class BothDoorsDoTheSameThing(_ConsoleFixture):
    """The page and the API are two faces of one implementation.

    This is the class of defect the extraction was for: the same action offered in
    two places, implemented twice, drifting one refusal at a time.
    """

    def test_a_reason_The_api_requires_is_required_by_the_page_too(self):
        self.god_post("unlock", passkey=PASSKEY)
        response = self.god_post("pause", reason="")
        api = self.client.post(
            "/api/console/god/act",
            json={"action": "pause", "reason": ""},
            headers={"X-CSRF-Token": CSRF},
        )
        self.assertEqual(api.status_code, 400)
        self.assertFalse(keep_alive.pause.state(refresh=True)["paused"])
        body = self.page("/god").get_data(as_text=True)
        self.assertIn("Give a reason", body)
        self.assertEqual(response.status_code, 302)

    def test_an_action_neither_of_them_knows_is_refused_by_both(self):
        self.god_post("unlock", passkey=PASSKEY)
        api = self.client.post(
            "/api/console/god/act",
            json={"action": "self_destruct", "reason": "no"},
            headers={"X-CSRF-Token": CSRF},
        )
        self.assertEqual(api.status_code, 400)
        self.god_post("self_destruct", reason="no")
        body = self.page("/god").get_data(as_text=True)
        self.assertIn("Unknown god action", body)

    def test_the_api_and_the_page_answer_the_same_lock_state(self):
        """One lock, read twice: the page's chip and the API's payload agree."""
        api = self.get("/api/console/god").get_json()
        body = self.page("/god").get_data(as_text=True)
        self.assertTrue(api["locked"])
        self.assertIn("locked", body.lower())
        self.god_post("unlock", passkey=PASSKEY)
        after = self.get("/api/console/god").get_json()
        self.assertFalse(after["locked"])
        self.assertIn("Close god level now", self.page("/god").get_data(as_text=True))


class TheReadingsAreHonest(_ConsoleFixture):
    """Where a reading could not be taken, the page says so instead of drawing a zero."""

    def test_a_page_with_no_server_in_hand_still_renders(self):
        """A process that can see no server is the first seconds of every restart.

        `_visible_guilds` is emptied rather than waited for: this is the state a
        fresh deployment is in for its first seconds and the state a fresh
        database is in for its whole life, and it is where a page that reported
        *0 members* would be lying about a server it cannot see.
        """
        with mock.patch.object(keep_alive, "_visible_guilds", return_value=[]):
            for page_id in ("overview", "servers", "live", "systems", "economy"):
                with self.subTest(page=page_id):
                    response = self.page(f"/{page_id}")
                    self.assertEqual(
                        response.status_code,
                        200,
                        response.get_data(as_text=True)[:200],
                    )
                    body = response.get_data(as_text=True)
                    self.assertNotIn("Traceback", body)
            self.assertIn(
                "not in a server this process can see",
                self.page().get_data(as_text=True),
            )
            self.assertIn(
                "No server to read",
                self.page("/live").get_data(as_text=True),
            )

    def test_the_access_page_reports_that_the_ledger_was_read(self):
        body = self.page("/access").get_data(as_text=True)
        self.assertIn("session", body.lower())
        self.assertNotIn("could not be read", body)


class TheCommandsPageStatesThePublishReading(_ConsoleFixture):
    """What Discord holds, in words and three figures — not in the reading's keys.

    This card handed `commands.publish` to the generic renderer, so the operator
    read `Tone never · Age Seconds — · At — · Held — · Pending 0 · Matches —`
    and then the sentence the reading had already composed, one line below the
    keys. The dashboard's Commands page draws the same reading as a sentence and
    chips, and two surfaces about one publish have to agree.
    """

    def test_the_card_says_what_discord_holds_rather_than_printing_its_keys(self):
        body = self.page("/commands").get_data(as_text=True)
        self.assertIn("What Discord holds", body)
        self.assertIn("Still to write", body)
        # Each of these is a key of `command_publish.describe`'s payload.
        for field in ("Tone", "Age Seconds", "Matches", "Pending"):
            self.assertNotIn(f"<dt>{field}</dt>", body)

    def test_the_sentence_is_the_one_the_reading_composed(self):
        """One author for the words: the page prints `describe()`'s own sentence.

        It is escaped by the template, so the comparison is against the escaped
        form — the console's readings say *bot's* and reach the browser as `&#39;`.
        """
        from utils import command_publish

        body = self.page("/commands").get_data(as_text=True)
        reading = command_publish.describe(0, expected=keep_alive.features.command_count())
        self.assertIn(escape(reading["text"]), body)


class TheSupportPage(_ConsoleFixture):
    """The bridge's own page: which room it is in, and what it says when it cannot.

    The one surface in this console that is a *conversation* rather than a
    reading, so what is asserted is that it cannot draw itself as a quiet room
    when it is a broken one — and that the two writes it offers are the same two
    the JSON door answers.
    """

    def test_the_page_names_the_setting_the_id_and_the_two_writes(self):
        body = self.page("/support").get_data(as_text=True)
        self.assertIn(keep_alive.support.CHANNEL_ENV, body)
        self.assertIn(str(keep_alive.support.channel_id()), body)
        self.assertIn("Pull the last", body)
        self.assertIn('action="/api/support"', body)
        # A write on this console is a form carrying its own token, and the
        # message field is capped by the module's own number rather than one
        # typed into the template.
        self.assertIn('name="csrf_token"', body)
        self.assertIn('name="body"', body)
        self.assertIn(f'maxlength="{keep_alive.support.MAX_BODY}"', body)

    def test_a_channel_nothing_answers_to_is_said_rather_than_drawn_as_quiet(self):
        """The failure that looks exactly like a conversation nobody has had."""
        body = self.page("/support").get_data(as_text=True)
        self.assertIn("not one this bot can see", body)

    def test_the_page_prints_the_address_this_deployment_is_actually_using(self):
        """The setting is a deployment fact, so the page reads the one in force."""
        with mock.patch.dict(os.environ, {"SUPPORT_CHANNEL_ID": "424242424242424242"}):
            body = self.page("/support").get_data(as_text=True)
        self.assertIn("424242424242424242", body)


class TheEconomyPageStatesTheLedger(_ConsoleFixture):
    """A page, not a mapping with its braces taken off.

    This page handed the raw ledger reading to the template's generic renderer,
    and that renderer walks a mapping's **field names** — so the operator read
    `Enabled True · Label 2% · Describe … · Cap 500 · Stored False`: their own
    reader's variable names, in monospace, above a table. A generic renderer
    earns its place on a mapping whose fields are unknown; the ledger's fields
    are the money, and every one of them has a name a person would use.
    """

    def test_the_figures_are_named_rather_than_printed_as_the_readers_fields(self):
        body = self.page("/economy").get_data(as_text=True)
        for label in ("In wallets", "In banks", "In circulation", "DaCat Bank", "Accounts"):
            self.assertIn(label, body)
        # Each of these is a key of `_progression_context`'s fee mapping. Printing
        # one is the reader talking to itself, and that is the defect this class
        # exists for — so the assertion is on the word, not on a styling.
        for field in ("Enabled", "Label", "Describe", "Cap", "Stored"):
            self.assertNotIn(f"<dt>{field}</dt>", body)

    def test_the_fee_is_the_banks_own_sentence_beside_three_labelled_facts(self):
        body = self.page("/economy").get_data(as_text=True)
        self.assertIn("The fee this server charges", body)
        for fact in ("rate", "state", "origin", "maximum"):
            self.assertIn(f"<dt>{fact}</dt>", body)
        self.assertIn("of a transfer goes to DaCat Bank", body)
        # The bank writes that sentence for Discord, where `**2%**` is bold. A
        # page is not, and `/owner/economy` shipped the literal marks to the one
        # person most likely to paste them somewhere.
        self.assertNotIn("**2%**", body)

    def test_a_wallet_table_prints_the_share_the_reader_computed(self):
        """A share is a division by a circulation that can be zero, so it is a
        **string made in Python** — the page prints `share_text`, and a template
        doing its own arithmetic on "no economy yet" divides by the zero."""
        display = keep_alive._economy_display(
            {
                "circulation": 0,
                "top": [{"user_id": 1, "net": 5, "wallet": 5, "bank": 0, "share": 0.0}],
            }
        )
        self.assertEqual(display["top"][0]["share_text"], "0.0%")
        self.assertEqual(display["top"][0]["net_text"], "5")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "templates", "owner.html"), encoding="utf-8") as handle:
            markup = handle.read()
        self.assertIn("holder.share_text", markup)
        self.assertNotIn("holder.share or", markup)

    def test_the_console_and_the_dashboard_state_one_ledger(self):
        """One author, asserted on the render calls rather than described in prose.

        A second reader is exactly how two surfaces about one server come apart:
        the console printed the mapping, the dashboard printed the ledger, and the
        fee's own sentence was stripped of its marks on one of them. The pair of
        calls below is what has to hold, so it is the pair that is checked.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "keep_alive.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("def _fee_display(", source)
        # The dashboard's render, and the console's context: both call it, and
        # neither builds the dict by hand.
        self.assertIn("economy_fee=_fee_display(fee)", source)
        self.assertIn('context["economy_fee"] = _fee_display(', source)
        console = source[source.index('if page == "economy" and selected:') :]
        self.assertIn('_economy_display(ledgers["economy"])', console)
        # And the copy that was hand-built is gone, not merely bypassed.
        self.assertNotIn('"stored": bool(fee.get("stored", False)),', source)


class TheOpsPageStatesTheDeployment(_ConsoleFixture):
    """Three readings as a sentence and labelled facts, not as their own keys.

    This page handed `facts()` the snapshot, the site guard's verdict and the
    configuration audit — and `facts()` walks a mapping's **field names**, so the
    operator read `Status · At · Stale · Ratio 30d · Age Seconds · Age Text ·
    Detail` on the one page whose entire job is to be read when something is
    wrong. The other half of that defect never even drew: `facts()` renders a
    mapping's scalars and skips a list, and `monitors` *is* a list — so the card
    said "2 monitors, 1 up" while the names and their 30-day ratios, the half a
    person can act on, were absent.
    """

    #: A reading with rows in it, because the rows are the point: two monitors,
    #: one of them down, and the sentence `uptime_status` composes for that.
    #: A guard verdict, because `health_json` returns counts and no sentence once
    #: there is one — so the words on the card come from `_site_note`.
    SITE = {
        "status": "exposed",
        "probes": 9,
        "exposed": 2,
        "cached": 0,
        "unanswered": 0,
        "site": "https://dacat.pages.dev",
        "checked_seconds_ago": 300,
    }

    READING = {
        "status": "down",
        "at": 1_700_000_000,
        "stale": True,
        "page": "https://stats.uptimerobot.com/example",
        "monitors": [
            {
                "id": "804081347",
                "name": "1tsrajowo-meow.hf.space/health",
                "url": "https://1tsrajowo-meow.hf.space/health",
                "status": 8,
                "label": "seems down",
                "paused": False,
                "ratios": {"30d": 99.4},
                "response_ms": 412,
            },
            {
                "id": "804081348",
                "name": "dacat.pages.dev",
                "url": "https://dacat.pages.dev",
                "status": 2,
                "label": "up",
                "paused": False,
                "ratios": {"30d": 100.0},
                "response_ms": 88,
            },
        ],
        "total": 2,
        "up": 1,
        "down": 1,
        "paused": 0,
        "waiting": 0,
        "ratio_30d": 99.4,
        "detail": "UptimeRobot is reporting 1tsrajowo-meow.hf.space/health (seems down).",
    }

    def answered(self):
        """The page as it draws once all three readings have something to say.

        This fixture's deployment has no UptimeRobot key and has not run the
        guard, so its cards take their "nothing yet" branches — which are the
        branches a fresh deploy shows, and have a law of their own below. The
        labels and the monitor rows are only on screen once a reading answers.
        """
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(keep_alive.uptime_status, "snapshot", return_value=dict(self.READING))
        )
        stack.enter_context(
            mock.patch.object(keep_alive.site_guard, "health_json", return_value=dict(self.SITE))
        )
        return stack

    def test_the_readings_are_named_rather_than_printed_as_the_readers_fields(self):
        with self.answered():
            body = self.page("/ops").get_data(as_text=True)
        for label in (
            "watching",
            "last read",
            "verdict",
            "paths asked for",
            "answered with a page",
            "costing a feature",
        ):
            self.assertIn(f"<dt>{label}</dt>", body)
        # Each of these is a key of one of the three readings, with the
        # underscores `facts()` replaced — printing one is the reader talking to
        # itself, and that is what this class exists for. `unset` is deliberately
        # not in this list: it is a word a person uses, and the card labels it.
        for field in (
            "status",
            "at",
            "stale",
            "ratio 30d",
            "age seconds",
            "age text",
            "detail",
            "probes",
            "exposed",
            "cached",
            "unanswered",
            "attention",
            "monitors",
            "checked seconds ago",
        ):
            self.assertNotIn(f"<dt>{field}</dt>", body)

    def test_a_deployment_that_has_read_nothing_says_so(self):
        """A fresh deploy's page, where a zero would be the lie.

        No key and no guard pass yet: the honest reading is *not connected* and
        *not checked yet*, and the one thing that must not appear is a figure for
        a check that never ran — `checked_seconds_ago: 0` is the shape that did,
        and it is why `/health` stopped publishing one.
        """
        body = self.page("/ops").get_data(as_text=True)
        self.assertIn("not connected", body)
        self.assertIn("not checked yet in this process", body)
        for field in ("status", "stale", "detail"):
            self.assertNotIn(f"<dt>{field}</dt>", body)

    def test_the_monitor_rows_draw_with_their_names_and_ratios(self):
        """The half of the reading that never reached the page at all."""
        with self.answered():
            body = self.page("/ops").get_data(as_text=True)
        self.assertIn("1tsrajowo-meow.hf.space/health", body)
        self.assertIn("dacat.pages.dev", body)
        self.assertIn("seems down", body)
        self.assertIn("99.4%", body)
        # The reading's own sentence, and the one line that says the figures on
        # screen are the older ones. A kept reading is a different claim.
        self.assertIn(escape(self.READING["detail"]), body)
        self.assertIn("Kept from the last pass that answered", body)

    def test_every_site_verdict_has_a_sentence_of_its_own(self):
        """`health_json` returns counts, so the words have to come from somewhere.

        It carries a sentence only while nothing has run in this process; once
        there is a verdict the sentence `site_guard` does compose is written for a
        host log to be grepped in, and it leads with `clean —`. So every verdict is
        stated here instead, and a verdict this build has not learned to describe
        is the one case that must not quietly say nothing.
        """
        verdicts = {
            "clean": "answer 404",
            "exposed": "still answer with a page",
            "cached": "answered from a cache",
            "fallback": "does not exist",
            "unreachable": "went unanswered",
        }
        for status, words in verdicts.items():
            with self.subTest(verdict=status):
                note = keep_alive._site_note(
                    {
                        "status": status,
                        "probes": 9,
                        "exposed": 2,
                        "cached": 1,
                        "unanswered": 3,
                        "site": "https://dacat.pages.dev",
                    }
                )
                self.assertIn(words, note)
                self.assertIn("https://dacat.pages.dev", note)
        self.assertEqual(keep_alive._site_note({"status": "something-new"}), "")

    def test_the_configuration_card_says_what_the_count_costs(self):
        """`health_json`'s own sentence is the literal string `/api/config`.

        That is a useful thing to publish on an unauthenticated endpoint and a
        useless thing to read on this page, which is already where the names are.
        """
        display = keep_alive._health_display(
            config={"status": "attention", "unset": 56, "attention": 24, "detail": "/api/config"}
        )
        self.assertIn("24 settings", display["config"]["note"])
        self.assertNotIn("/api/config", display["config"]["note"])
        # And a set deployment is not congratulated with a count of nothing.
        quiet = keep_alive._health_display(config={"status": "ok", "unset": 0, "attention": 0})
        self.assertIn("none of them is one", quiet["config"]["note"])

    def test_the_page_and_the_json_read_one_reading(self):
        """One author, asserted on the calls rather than described in prose.

        The console's JSON overview is the surface a client reads and the page is
        the surface a person reads; two readers of one snapshot is exactly how
        they come apart, so the page draws `_health_display`'s rendering of the
        same three calls the overview makes.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "keep_alive.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("def _health_display(", source)
        self.assertIn("context[\"health\"] = _health_display(", source)
        # The mapping it replaced, and the dead `row.detail` the list printed,
        # are gone rather than merely bypassed.
        self.assertNotIn('{{ facts(\'The monitor\', health.uptime) }}', source)
        with open(os.path.join(root, "templates", "owner.html"), encoding="utf-8") as handle:
            markup = handle.read()
        self.assertIn("health.uptime.monitors", markup)
        # The list printed `row.detail` for rows that have no such field, so
        # every line read as an environment variable with nothing after it.
        self.assertIn("row.cost", markup)
        self.assertNotIn("row.detail if row.detail is defined else ''", markup)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
