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
