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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
