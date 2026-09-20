"""The landing page — the front door, and the four things it must keep.

The composition (see `body.landing` in `static/meow.css`) is a hero with a
clip-masked headline, a paper band listing every system, and a band of live
figures, with `static/motion.js` doing the reveals. Motion is the part that can
quietly become essential, so these tests are about what has to be true when the
script does *not* run, when the fonts arrive late, and when the page is served
from a CDN with a one-year cache.
"""
import os
import re
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

#: The pages this landing system owns. `/pricing` and the policies share the
#: sheet but not this layout, so they are not listed here.
LANDING = ROOT / "templates" / "home.html"
MOTION = ROOT / "static" / "motion.js"
SHEET = ROOT / "static" / "meow.css"


class TheContentDoesNotNeedTheScript(unittest.TestCase):
    """A reveal that hides content until JavaScript runs is an outage.

    Every gated element is moved *out* of position by the script rather than into
    it: the markup carries the final state, so a visitor whose browser blocks
    `/static/motion.js` — or whose connection drops it — reads the whole page,
    and so does a crawler.
    """

    def setUp(self):
        self.markup = LANDING.read_text(encoding="utf-8")
        self.script = MOTION.read_text(encoding="utf-8")
        self.sheet = SHEET.read_text(encoding="utf-8")

    def test_the_headline_is_real_text_in_the_markup(self):
        """The words are in the template, not assembled by the script.

        The reveal *splits* them into clip masks at runtime; if it were the thing
        that created them, a blocked script would leave an empty `h1`.
        """
        self.assertIn("Own your server", self.markup)
        self.assertRegex(
            self.markup,
            r'<h1[^>]*data-text-reveal="words"[^>]*>\s*Own your server\s*</h1>',
            "the headline must be the sentence itself, in one element",
        )

    def test_the_page_carries_its_own_status_and_not_a_promise_of_one(self):
        """No script is involved in the state line, and a file is not a process."""
        self.assertIn("{{ status.detail }}", self.markup)
        self.assertNotIn("sleeping free-tier host", self.markup)

    def test_reduced_motion_is_honoured_in_both_places(self):
        """The script *and* the sheet, because either can act first.

        The curtain is dismissed by a CSS animation as well as by the script —
        that is what stops a blocked script stranding a visitor behind it — so
        both have to respect the preference.
        """
        self.assertIn("prefers-reduced-motion", self.script)
        self.assertIn("prefers-reduced-motion", self.sheet)
        self.assertIn("lp-curtain-away", self.sheet)

    def test_nothing_here_loads_a_third_party(self):
        """The CSP allows `'self'` for scripts; the market page is the exception.

        Asserted on the template rather than on a response because this is about
        what the page *asks for*, and a page that asks is broken everywhere it is
        served, not only where the header is set.
        """
        for match in re.findall(r'(?:src|href)="([^"]+)"', self.markup):
            self.assertFalse(
                match.startswith(("http://", "https://", "//")),
                f"the landing page loads {match} from somewhere else",
            )


class TheFiguresAreShownOnce(unittest.TestCase):
    """The bug this pins: a live figure rendered as "systems 11 systems".

    The figures are a `<dt>`/`<dd>` pair, and the first version hid the term with
    a `.sr-only` class that this page never had — so the label showed up twice
    on screen for everybody, and the accessible name was doubled for everybody
    using a screen reader. The fix is markup order plus one CSS rule, and the
    thing that makes it invisible is that the page still *looks* plausible.
    """

    def setUp(self):
        self.markup = LANDING.read_text(encoding="utf-8")
        self.sheet = SHEET.read_text(encoding="utf-8")

    def test_each_figure_is_a_term_and_a_definition_with_no_repetition(self):
        self.assertIn("<dt>{{ stat.label }}</dt>", self.markup)
        self.assertIn("<dd>{{ stat.value }}</dd>", self.markup)
        self.assertNotIn("sr-only", self.markup, "the label is hidden again")

    def test_the_figure_is_drawn_above_its_label_by_the_stylesheet(self):
        """`<dt>` must precede its `<dd>`, so the visual order comes from CSS."""
        stat = self.sheet.split(".lp-stat {", 1)[1]
        self.assertIn("display: flex", stat)
        self.assertIn("order: -1", stat)

    def test_the_accessibility_utility_exists_on_the_shared_sheet(self):
        """.sr-only was defined inside `dashboard.html` and nowhere else.

        Which is exactly how the page that needed it next did not have it.
        """
        self.assertIn(".sr-only {", self.sheet)


class AssetsAreCacheSafe(unittest.TestCase):
    """`/static/*` is served `immutable` for a year by the static host.

    That is only safe while the URL changes with the file, and hand-maintained
    `?v=` strings had already drifted: the stylesheet was linked bare, so a CSS
    edit would have been invisible to returning visitors for up to a year while
    looking perfectly correct to whoever deployed it.
    """

    def test_every_page_versions_the_stylesheet(self):
        import keep_alive

        pages = sorted((ROOT / "templates").glob("*.html"))
        self.assertTrue(pages)
        for page in pages:
            text = page.read_text(encoding="utf-8")
            if "/static/meow.css" not in text:
                continue
            self.assertIn(
                "asset_version('meow.css')", text,
                f"{page.name} links the sheet without a token that changes with it",
            )
        self.assertEqual(
            keep_alive.asset_version("meow.css"), keep_alive.asset_version("meow.css"),
            "the token is not stable between renders, so every request misses the cache",
        )

    def test_the_token_changes_when_the_file_does(self):
        import keep_alive

        meow = keep_alive.asset_version("meow.css")
        motion = keep_alive.asset_version("motion.js")
        self.assertNotEqual(meow, motion, "two different files share a token")
        with mock.patch.object(keep_alive.os, "stat") as stat:
            stat.return_value = os.stat_result((0, 0, 0, 0, 0, 0, 999999, 0, 0, 0))
            changed = keep_alive.asset_version("meow.css")
        self.assertNotEqual(changed, meow, "a changed file kept its old token")

    def test_a_missing_asset_is_not_an_exception(self):
        """A template must not 500 because a file was renamed."""
        import keep_alive

        self.assertEqual(keep_alive.asset_version("not-a-real-file.css"), "0")

    def test_the_fit_script_measures_the_content_box(self):
        """The headline is fitted by measurement, not by a `vw` guess.

        The first version fitted it to the *padding* box, so it filled its box and
        still crossed the band's own padding, where `overflow: hidden` cut the
        last letter off — "OWN YOUR SERV". A test rather than a comment because
        the failure looks like a font problem.
        """
        script = MOTION.read_text(encoding="utf-8")
        self.assertIn("paddingLeft", script)
        self.assertIn("paddingRight", script)
        self.assertIn("scrollWidth", script)


class TheTryPanelIsRealAndSaysSo(unittest.TestCase):
    """The one claim on this page a reader cannot check by reading.

    The page says a system switched off disappears from *one server's* command
    picker. That is the product's actual difference from a bot whose commands are
    global, so it ships as something the reader can operate — press a system and
    watch the list change — rather than as another paragraph asserting it.

    What is pinned here is the set of ways that could quietly stop being true:
    the panel is drawn from the registry rather than a hand-written copy, the
    names it prints are the names the bot registers, the payload is escaped so it
    can neither close its own `<script>` nor be mangled by Jinja, and the section
    says nothing before the script runs that the script then contradicts.
    """

    def setUp(self):
        self.markup = LANDING.read_text(encoding="utf-8")

    def test_a_command_is_printed_with_the_one_slash_it_already_has(self):
        """`/{{ cmd }}` rendered `//rank`, `//balance` and `//daily` on this page.

        Every name in the registry carries its own leading slash, and the
        dashboard's module cards have always rendered `{{ cmd }}` — so the two
        surfaces disagreed and only this one was wrong. It is the kind of defect
        that survives review because a double slash still *reads* as a command.
        """
        import keep_alive

        with mock.patch.object(keep_alive, "BOT_INSTANCE", None):
            commands = [
                command
                for module in keep_alive.AVAILABLE_MODULES
                for command in (module.get("commands") or [])
            ]
        self.assertTrue(commands, "no commands in the registry, so nothing is checked")
        without = [c for c in commands if not str(c).startswith("/")]
        self.assertEqual(without, [], "a name without a slash would now render as /name")

        # And the template must not add one of its own. Matched on the *loop
        # bodies*, because the same page prints a slash in prose elsewhere.
        for body in re.findall(r"\{%\s*for cmd in [^%]*%\}(.*?)\{%\s*endfor\s*%\}", self.markup, re.S):
            with self.subTest(body=body.strip()[:60]):
                self.assertNotIn(">/{{ cmd }}<", body)
                self.assertNotIn("textContent = \"/\"", body)

    def test_the_panel_is_drawn_from_the_registry_not_a_copy_of_it(self):
        self.assertIn("{% for feature in features %}", self.markup)
        self.assertIn('data-system="{{ feature.id }}"', self.markup)
        self.assertIn("try_payload", self.markup)

    def test_the_payload_cannot_close_its_own_script_element(self):
        """A `</script` inside the data would end the tag and spill into the page."""
        import keep_alive

        payload = keep_alive._try_payload()
        self.assertNotIn("<", payload)
        self.assertNotIn("</script", payload.lower())
        self.assertTrue(payload.startswith("["))

    def test_the_payload_is_json_that_the_page_can_actually_parse(self):
        """Autoescaping turned every quote into `&#34;`, which is valid HTML and
        invalid JSON — so the panel silently fell back to its static state on
        every page while looking perfectly correct in the markup."""
        import json

        import keep_alive
        from flask import render_template

        with keep_alive.app.test_request_context("/"):
            rendered = render_template("home.html", **{**self._minimal_home(), "try_payload": keep_alive._try_payload()})
        found = re.search(
            r'<script type="application/json" data-try-data>(.*?)</script>', rendered, re.S
        )
        self.assertIsNotNone(found, "the payload is not in the rendered page")
        parsed = json.loads(found.group(1))
        self.assertTrue(parsed, "the payload parsed to nothing")
        self.assertIn("id", parsed[0])
        self.assertIn("commands", parsed[0])

    def test_the_sections_static_state_is_the_truthful_one(self):
        """With no script at all it must claim everything is on, not nothing.

        A server nobody has configured runs every system, so that is what the
        markup says. A panel that started from "nothing is on" would be telling a
        reader something false about the product every time the script failed.
        """
        self.assertIn("aria-pressed=\"true\"", self.markup)
        self.assertIn("All {{ features|length }} systems on.", self.markup)

    def test_the_empty_state_is_in_the_markup_rather_than_built_by_script(self):
        self.assertIn('data-try-empty hidden', self.markup)

    def test_it_promises_that_nothing_is_sent(self):
        """A visitor who has not added the bot must not be made to wonder."""
        self.assertIn("Nothing is sent anywhere", self.markup)

    @staticmethod
    def _minimal_home():
        """The names `home.html` reaches for that this test does not care about."""
        return {
            "bot_name": "Meow Bot",
            "bot_initial": "M",
            "status": {"state": "up", "label": "Up"},
            "stats": [],
            # The two preview cards. Empty here because this test is about the
            # systems panel's payload; the cards have their own test in
            # `tests/test_figures.py`, which renders them with real ones.
            "control_tiles": [],
            "activity_tiles": [],
            "activity_periods": [],
            "system_bars": [],
            "figures_json": "{}",
            "figures_url": "/api/figures",
            "features": [],
            "invite_url": "",
            "invite_limited_url": "",
            "invite_here_url": "",
            "enter_url": "/login",
            "enter_label": "Sign in",
            "retention_days": 30,
            "oauth_enabled": False,
            "price_free": "Free",
            "price_pro": "",
            "price_enterprise": "",
            "selling": False,
            "source_repository": "https://example.test/repo",
            "meta_description": "a bot",
            "asset_version": lambda name: "0",
        }


class TheMenuIsUsableFromTheKeyboard(unittest.TestCase):
    """The bug this pins: an overlay that opens and forgets the caret.

    `.lp-menu` transitions `visibility` over 260ms so that a closing menu stays
    on screen while it fades, and a browser *silently ignores* `focus()` on an
    element that is still hidden. So "call focus once after a frame" did nothing
    at all — and because the panel's header link to the site root comes first in
    the DOM, the version that did work put the caret on a link that navigates
    away, so pressing Enter on a freshly opened menu left the page.

    Asserted on the script because this is timimg inside a browser, which the
    suite does not run; each check is one of the three decisions that were wrong,
    and each one fails *quietly* on screen.
    """

    def setUp(self):
        self.script = MOTION.read_text(encoding="utf-8")

    def test_the_focus_attempt_is_repeated_until_it_takes(self):
        """One frame is not enough while `visibility` is still transitioning."""
        self.assertIn("document.activeElement === first", self.script,
                      "nothing checks whether the focus actually landed")
        self.assertIn("requestAnimationFrame(function () { attempt(n + 1); })",
                      self.script, "the attempt is not retried")

    def test_the_menu_opens_onto_its_own_links_not_the_brand(self):
        """The first focusable element is a link home, which is not the point."""
        grab = self.script.split("function grabFocus()", 1)[1].split("function setMenu", 1)[0]
        self.assertIn('querySelector("nav a")', grab)
        self.assertLess(grab.index('querySelector("nav a")'), grab.index('querySelector("a, button")'),
                        "the brand link would be reached first again")

    def test_escape_only_closes_a_menu_that_is_open(self):
        """An unguarded close steals focus back on any Escape, anywhere."""
        self.assertIn('event.key === "Escape" && menu.classList.contains("is-open")',
                      self.script)


if __name__ == "__main__":
    unittest.main()
