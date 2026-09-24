"""What Discord holds, versus what a server's switches asked for.

The dashboard's command-scope panel is named for Discord's picker and says in its
own copy that a command whose system is off is "not in this server's picker at
all". Until this existed it computed both figures from the local module cache —
which is what the server *asked for* — so it read the same ninety whether the
republish had landed, was still behind its settle, or had been refused outright
by a spent daily write budget. The only trace of a refusal was a line in the ring
buffer on another page, and the page's own gesture at all of this was a flat
four-second timer that re-read the same local numbers.

Two halves have to hold, and they fail differently:

* **The record must be exact or absent.** ``held`` is a count, and a count that is
  ambiguous — a pass refused part-way through its writes — must be ``None``.
  A number rendered under a heading is read as a measurement, so an approximate
  one is worse than no number at all.
* **The sentence must have one author.** The line is composed in
  ``utils/command_publish.describe`` and handed to the page whole, because the
  page is no longer its only reader: the JSON endpoint answers the same scope, and
  a console or a script that spelled its own sentence would disagree with the page
  about the same reading. (The dashboard's own refresh script, which was the second
  renderer this module was written against, is gone with the rest of its
  JavaScript — see :class:`TheScopePanel`.)
"""

import re
import unittest
from pathlib import Path

from tests.test_composer import GUILD_ID
from tests.test_dashboard import _DashboardFixture, page_href
from utils import command_publish

ROOT = Path(__file__).resolve().parents[1]
_DASHBOARD = ROOT / "templates" / "dashboard.html"
# Rebuilt and back on disk; the guard stays because the read used to raise
# FileNotFoundError at import, which aborts collection for the *whole* suite —
# every other file's results hidden behind one absence. What depends on the
# template is skipped rather than the run being lost with it.
TEMPLATE = _DASHBOARD.read_text(encoding="utf-8") if _DASHBOARD.exists() else ""


class _Recorded(unittest.TestCase):
    """The record itself: a dict of small facts, and no cleverness."""

    def setUp(self):
        command_publish.forget()

    def tearDown(self):
        command_publish.forget()

    def test_a_scope_nobody_has_published_says_so(self):
        line = command_publish.describe(7)
        self.assertEqual(line["tone"], "never")
        self.assertIsNone(line["at"], "there is no reading, so there is no timestamp")
        self.assertIsNone(line["held"])
        self.assertIn(
            "not been read since this process started", line["text"],
            "the panel must not present a server's own switches as a reading of Discord",
        )

    def test_a_finished_pass_reads_as_a_match(self):
        command_publish.record(7, verified=True, pending=0, held=90)
        line = command_publish.describe(7, expected=90)
        self.assertEqual(line["tone"], "ok")
        self.assertTrue(line["matches"])
        self.assertEqual(line["held"], 90)
        self.assertIn("90 commands", line["text"])

    def test_a_guild_and_its_id_are_one_record(self):
        """``bot.py`` holds a guild; the web layer holds the id from a cookie.

        Filed under two keys, every server would read as never published — which
        is exactly what happened the first time this module was tested, with the
        publisher's records under ``"<Guild id=… name=…>"``.
        """
        command_publish.record(7, verified=True, pending=0, held=3)
        command_publish.record(type("G", (), {"id": 7})(), verified=True, pending=0, held=4)

        self.assertEqual(command_publish.read(7)["held"], 4)
        self.assertEqual(command_publish.describe(7)["tone"], "ok")
        self.assertIsNone(command_publish.read("<Guild id=7>"), "the object is not a key")

    def test_an_unfinished_pass_names_the_count_and_the_delay(self):
        command_publish.record(
            7, verified=True, pending=3, held=None, detail="3 refused (a, b, c)",
            retry_after=45.0,
        )
        line = command_publish.describe(7)
        self.assertEqual(line["tone"], "pending")
        self.assertIn("still needs 3 commands", line["text"])
        self.assertIn("3 refused (a, b, c)", line["text"])
        self.assertIn("next pass is in 45 seconds", line["text"])

    def test_an_unread_scope_is_unverified_not_empty(self):
        """A pass that could not read Discord publishes the whole tree — correct
        behaviour, and no evidence at all that the scope is right."""
        command_publish.record(
            7, verified=False, pending=0, held=None,
            detail="could not reach Discord",
        )
        line = command_publish.describe(7)
        self.assertEqual(line["tone"], "unverified")
        self.assertIn("could not be confirmed", line["text"])
        self.assertIn("could not reach Discord", line["text"])
        self.assertIsNone(line["held"])
        self.assertNotIn(
            "Discord holds ", line["text"],
            "an unread scope must never be described as holding a number",
        )

    def test_the_line_does_not_invent_a_cause_the_record_never_had(self):
        """A pass that died half-way has a reading and no conclusion.

        The boot publish and the drain both record one of these for every raise
        (``bot.py``), with their own words in ``detail``. The line may not name
        the read as what failed: a raise from the retirement step implies the
        read had already succeeded, so the sentence sent an administrator to
        look at Discord's API for a spent write budget.
        """
        command_publish.record(
            7, verified=False, pending=0, held=None,
            detail="the last republish failed: 500 Internal Server Error",
        )
        line = command_publish.describe(7)
        self.assertEqual(line["tone"], "unverified")
        self.assertNotIn("could not be read", line["text"])
        self.assertIn("the last republish failed: 500", line["text"])
        self.assertIsNone(line["held"])

    def test_a_disagreement_that_writes_cannot_explain_is_named(self):
        """The case a bulk overwrite once produced: complete, and still wrong."""
        command_publish.record(7, verified=True, pending=0, held=87)
        line = command_publish.describe(7, expected=90)
        self.assertFalse(line["matches"])
        self.assertIn("3 fewer", line["text"])
        self.assertIn("Discord holds 87", line["text"])

    def test_the_sentence_never_claims_agreement_and_disagreement_at_once(self):
        """Appended, the disagreement read "…matching what this server's switches
        say. Discord holds 87 — 3 fewer than this server's switches say it
        should" — so the one line this function gained ``expected`` to print
        contradicted itself in its own first half."""
        for held, expected in ((87, 90), (93, 90)):
            command_publish.record(7, verified=True, pending=0, held=held)
            text = command_publish.describe(7, expected=expected)["text"]
            self.assertNotIn(
                "matching what this server's switches say", text,
                f"held={held}, expected={expected}: that clause is false here",
            )
            self.assertIn("Discord holds", text)
            self.assertEqual(text.count("Discord holds"), 1, "said twice")

        # And with nothing to disagree with, it still says the one thing it can.
        command_publish.record(7, verified=True, pending=0, held=90)
        agreed = command_publish.describe(7, expected=90)["text"]
        self.assertIn("matching what this server's switches say", agreed)
        self.assertNotIn("fewer", agreed)
        self.assertNotIn("more", agreed)

    def test_a_count_that_is_not_knowable_is_not_printed(self):
        command_publish.record(7, verified=True, pending=2, held=None)
        line = command_publish.describe(7)
        self.assertIsNone(line["held"])
        self.assertNotIn(
            "Discord holds", line["text"],
            "a half-finished pass cannot count what Discord has, so it says nothing",
        )

    def test_the_age_is_computed_when_it_is_read(self):
        """An ``at`` carried to the page and rendered there is how a panel says
        "read 1789848173 seconds ago"."""
        command_publish.record(7, verified=True, pending=0, held=1)
        recorded = command_publish.read(7)["at"]
        line = command_publish.describe(7, now=recorded + 120)
        self.assertEqual(line["age_seconds"], 120)
        self.assertIn("2 minutes ago", line["text"])

    def test_one_of_anything_is_not_spelled_with_a_plural(self):
        command_publish.record(7, verified=True, pending=1, held=None, retry_after=1.0)
        line = command_publish.describe(7, now=command_publish.read(7)["at"] + 1)
        self.assertIn("needs 1 command ", line["text"])
        self.assertIn("next pass is in 1 second", line["text"])
        self.assertIn("1 second ago", line["text"])
        for wrong in ("1 seconds", "1 commands", "1 minutes"):
            self.assertNotIn(wrong, line["text"], f"{wrong!r} reads like a bug")

    def test_the_reader_cannot_reach_into_the_record(self):
        command_publish.record(7, verified=True, pending=0, held=3)
        first = command_publish.read(7)
        first["held"] = 999
        self.assertEqual(command_publish.read(7)["held"], 3)

    def test_the_global_lane_is_a_scope_of_its_own(self):
        command_publish.record(None, verified=True, pending=0, held=2)
        self.assertEqual(command_publish.read(None)["held"], 2)
        self.assertIsNone(command_publish.read(7), "the shared lane is not a server")


@unittest.skipIf(not TEMPLATE, "templates/dashboard.html is archived, not rebuilt yet")
class TheScopePanel(unittest.TestCase):
    """The panel has to carry the line, and the page has to follow the publish.

    Read from the template, because what is worth pinning here is a property of
    the *page* rather than of any one value it renders.
    """

    def test_the_line_is_rendered_by_the_server_not_only_by_the_script(self):
        """A reader with no JavaScript, or one who has not clicked, still learns
        whether Discord was read — so it cannot be a script-built element."""
        panel = TEMPLATE.split('id="scope-publish"', 1)
        self.assertEqual(len(panel), 2, "the panel has no publish line at all")
        markup = panel[1].split("</div>", 1)[0]
        self.assertIn('id="scope-publish-text"', markup)
        self.assertIn("command_scope_publish.text", markup)

    # Two per-test retirements stand here rather than as tests, and both are the
    # same fact: **the page has no refresh script any more.**
    #
    # The dashboard used to be one document whose panel was repainted in place by
    # a script — the reason half of this module existed was that two renderers of
    # one sentence could disagree, and that the panel slept four seconds and then
    # re-read figures the click had already saved. The rebuilt dashboard is eleven
    # pages of forms, and the host's CSP carries no `unsafe-inline`, so there is no
    # script to refresh anything: a switch POSTs, the endpoint answers, and the
    # page is rendered again by the server with the new reading already in it. That
    # is strictly better than waiting — there is nothing left to wait *for* — and
    # it is pinned where the page is pinned, in
    # ``tests/test_dashboard_pages.py::ThePublishLineIsServerRendered``.
    #
    # What went: the tone→dot vocabulary (it was CSS drawn by the script), the
    # `publish.at > since` wait (the API still answers `publish_at` for anything
    # that wants to watch a republish land), `scheduleScopeRefresh(data.publish_at)`
    # on both switches, and the "two clocks are never compared" test whose positive
    # half was that same comparison. The negative half — never subtracting the
    # browser's clock from the server's stamp — is now held by the absence of the
    # script rather than by a line in a template.

    def test_the_template_does_not_spell_the_sentence_itself(self):
        """One author for the words, and it is ``utils/command_publish``.

        Kept rather than retired with the rest of this class: the sentence is the
        part that would *spread* if somebody hand-assembled it here, and it is one
        line to assert on the page as it ships.
        """
        self.assertEqual(
            TEMPLATE.count('id="scope-publish-text">{{ command_scope_publish.text }}'), 1,
            "the sentence must be handed over whole, not assembled here",
        )
        # The figures may be printed — they are labelled readings of their own —
        # but the *sentence* may not be spelled here. So what is checked absent is
        # its wording, not the fields it was composed from.
        for fragment in (
            "matching what this server's switches say",
            "has not been read since this process started",
            "could not be confirmed",
        ):
            self.assertNotIn(
                fragment, TEMPLATE,
                "the page is spelling the sentence; the wording belongs to "
                "utils/command_publish, which every reader of this scope shares",
            )


@unittest.skipIf(not TEMPLATE, "templates/dashboard.html is archived, not rebuilt yet")
class ScopeApiAnswer(_DashboardFixture):
    """Driven against the real app, because the endpoint and the page must agree.

    The JSON endpoint and the page render the same scope through different seams —
    a query string against a resolved session — and a test that only rendered the
    template would pass while the endpoint answered a different scope, or while
    the *page* did. The scoping itself is the interesting part, not the wording:
    ``describe`` answers correctly for both scopes, which is exactly why the bug
    this class was written for could only be seen through the route.
    """

    def setUp(self):
        super().setUp()
        command_publish.forget()

    def tearDown(self):
        command_publish.forget()
        super().tearDown()

    def test_the_endpoint_answers_what_discord_holds(self):
        command_publish.record(GUILD_ID, verified=True, pending=0, held=90)
        self.sign_in()

        payload = self.api("GET", "/api/command-scope").get_json()

        self.assertTrue(payload["success"])
        self.assertEqual(payload["publish"]["tone"], "ok")
        self.assertEqual(payload["publish"]["held"], 90)
        self.assertEqual(
            payload["publish"]["at"], command_publish.read(GUILD_ID)["at"],
            "the stamp is what the page compares to know Discord was reread",
        )
        # The figures this endpoint has always answered with are still there.
        self.assertIn("available", payload)
        self.assertIn("hidden", payload)

    def test_an_unpublished_scope_is_not_reported_as_agreement(self):
        self.sign_in()
        payload = self.api("GET", "/api/command-scope").get_json()
        self.assertEqual(payload["publish"]["tone"], "never")
        self.assertIsNone(payload["publish"]["at"])

    def test_the_page_carries_the_line_before_any_script_runs(self):
        command_publish.record(GUILD_ID, verified=True, pending=0, held=90)
        self.sign_in()

        body = self.client.get(page_href("commands")).data.decode()

        self.assertIn("Discord holds 90 commands", body)
        self.assertIn('data-tone="ok"', body)
        self.assertIn('id="scope-publish-text"', body)

    def test_an_unscoped_page_does_not_borrow_the_shared_lane(self):
        """No server selected is not Discord's global scope.

        The route answers a session with no server with the id ``0`` and renders
        the default switches, so mapping that sentinel onto ``None`` — Discord's
        *shared* lane, which holds the DM-safe few — put that lane's reading under
        a heading whose figures were the defaults: "Discord holds 2 — 2 more than
        this server's switches say it should", about a server the page is not
        showing. Driving the real route is the only way to see it; ``describe``
        answers correctly for both scopes.
        """
        command_publish.record(None, verified=True, pending=0, held=2)
        self.sign_in(guild_id=None)

        body = self.client.get(page_href("commands")).data.decode()
        line = re.search(
            r'id="scope-publish-text">([^<]*)<', body,
        ).group(1)

        self.assertNotIn("Discord holds 2", line)
        self.assertNotIn("more than this server's switches say", line)
        self.assertIn("not been read since this process started", line)

    def test_the_page_says_so_when_discord_has_not_been_read(self):
        """A page opened before the bot has published must not imply it has."""
        self.sign_in()
        body = self.client.get(page_href("commands")).data.decode()
        self.assertIn('data-tone="never"', body)
        self.assertIn("not been read since this process started", body)
        self.assertNotIn(
            'data-tone="ok"', body,
            "a process that has not published yet must not report a match",
        )

    def test_a_switch_hands_over_the_stamp_it_supersedes(self):
        """``/api/toggle`` answers with the publish *before* it queues a republish.

        The page waits until the panel reports a newer stamp, which is a fact —
        Discord has been read since this click — rather than four seconds of
        hoping. It compares two readings of the server's clock, never the
        browser's against the server's, so a host in another region cannot make
        it wait forever or give up early.
        """
        command_publish.record(GUILD_ID, verified=True, pending=0, held=90)
        stamp = command_publish.read(GUILD_ID)["at"]
        self.sign_in()

        payload = self.api(
            "POST", "/api/toggle",
            {"guild_id": GUILD_ID, "module": "fun", "enabled": False},
        ).get_json()

        self.assertTrue(payload["success"], payload.get("error"))
        self.assertEqual(payload["publish_at"], stamp)
        self.assertEqual(
            command_publish.read(GUILD_ID)["at"], stamp,
            "logging a click is not a publish, and must not look like one",
        )

    def test_the_bulk_switch_hands_over_a_stamp_too(self):
        command_publish.record(GUILD_ID, verified=True, pending=0, held=90)
        stamp = command_publish.read(GUILD_ID)["at"]
        self.sign_in()

        payload = self.api(
            "POST", "/api/toggle-all", {"guild_id": GUILD_ID, "enabled": False}
        ).get_json()

        self.assertTrue(payload["success"], payload.get("error"))
        self.assertEqual(payload["publish_at"], stamp,)
        self.assertGreater(
            command_publish.read(GUILD_ID)["at"], stamp - 1,
            "the switch must not clear the record it hands over",
        )

    def test_a_scope_no_other_server_can_read_through(self):
        """The panel is about one server. A record for another must not leak in."""
        command_publish.record(999002, verified=True, pending=0, held=1)
        self.sign_in()

        payload = self.api("GET", "/api/command-scope").get_json()

        self.assertEqual(payload["publish"]["tone"], "never")

    def test_the_page_compares_no_two_clocks_of_its_own(self):
        """Retired down to its negative half, which is the half that survives.

        The positive half was the page waiting for a newer stamp than the click
        handed it — a real fix for a real defect, and now unnecessary: the page has
        no script, so a toggle is answered by a fresh render rather than by a
        comparison. The negative half still matters, because it is the mistake
        somebody would make while *re-adding* a script, and it costs one line to
        keep: the browser's clock and the server's are minutes apart on a host in
        another region, so nothing here may subtract one from the other.
        """
        self.assertNotIn(
            "Date.now() - publish.at", TEMPLATE,
            "browser clock versus server clock breaks on a host in another region",
        )
        self.assertNotIn("new Date(publish.at", TEMPLATE)


if __name__ == "__main__":
    unittest.main()
