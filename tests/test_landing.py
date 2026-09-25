"""The site's pages and the two things about them that outlived the rebuild.

This file was written for the pre-rebuild landing site: three documents sharing
`templates/site_base.html` and a header partial, composed with `static/landing.css`
and `static/motion.js`, with the front door's figures in a band, a headline fitted
by measurement, and an interactive systems panel on `/systems` whose payload came
from `keep_alive._try_payload()`.

The rebuild replaced that composition — one frame (`templates/meoww/base.html`),
one sheet (`static/meoww.css`), a spine, and a front door built around the
dashboard rather than around a paragraph — and the laws for it were written where
they belong: `tests/test_meoww_site.py` holds the frame, the motion, the command
demo, the navigation cap, the contrast and the one-author rules;
`tests/test_pages_site.py` holds the built copy, the catalogue's row and the
pricing page. What is left *here* is what this file was uniquely responsible for
and what survived the rebuild:

* **A cached asset has to be a versioned one.** `/static/*` is served
  `immutable` for a year, so the token on a sheet's URL is the only thing that
  makes an edit visible.
* **The three pages are one document split three ways**, and the quiet failures
  of that split are a fragment that lands nowhere and an id declared twice.
* **A control the frame's pages draw is a control the frame's sheet rules** — a
  `<button>` wearing a class no sheet answers renders with the user agent's
  background and the system font.
* **The frame's own menu is a toggle**, and the two things a toggle owes: it is
  not offered to a reader whose browser cannot run it, and its `aria-expanded`
  tracks the panel rather than the click.

Retired, with where each claim lives now:

* ``TheContentDoesNotNeedTheScript`` — the headline is real text, the page states
  its own status rather than promising one, reduced motion is honoured, nothing
  loads a third party. Now
  `tests/test_meoww_site.py::PostelThePageWorksWithNoScript`,
  `tests/test_meoww_site.py::MotionExactlyOneEntrance` and
  `tests/test_site.py::TheSiteIsOneDesign::test_no_page_loads_a_third_party_except_the_chart_library`.
* ``TheFiguresAreShownOnce`` — the figures band was replaced by the `mw-stat`
  cards; the counts are asserted by
  `tests/test_pages_site.py::BuiltSiteCase::test_the_front_page_and_the_reference_agree_on_how_many_of_each`.
* ``TheTryPanelIsRealAndSaysSo`` — the `/systems` interactive panel is gone, and
  `keep_alive._try_payload()` went with it (the view was composing a payload no
  template rendered). The demo is on the front door now, and
  `tests/test_meoww_site.py::TheFrontDoorShowsOneThingAtATime` holds the claim
  that it knows every real command and asks nothing of any server.
* ``TheMenuIsUsableFromTheKeyboard``'s overlay — the panel it was written about
  (`static/motion.js` and `.lp-menu`) left with the rebuild; the frame's menu is a
  plain toggle in `static/meoww.js`, and the law at the bottom of this file is
  what that toggle owes instead.
* ``EveryButtonIsDressedAsOne``'s reset list — `.lx-*` and the comma-separated
  `background: none; font: inherit` block went with the landing sheet. The claim
  is re-pointed below at the frame's own vocabulary rather than at that list.
* ``TheSiteIsThreePagesAndTheyAllWork``'s per-page laws — where a section lives
  and what it carries. `tests/test_meoww_site.py::TheSpineIsATableOfContents`
  asserts that every mark points at a section that exists, which is the part of
  the fragment law a static list cannot drift out of.
"""
import os
import re
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

#: The three pages of the site, and the frame they are drawn inside.
FRONT = ROOT / "templates" / "home.html"
SYSTEMS = ROOT / "templates" / "systems.html"
START = ROOT / "templates" / "start.html"
SITE_PAGES = (FRONT, SYSTEMS, START)
FRAME = ROOT / "templates" / "meoww" / "base.html"
#: The sheet the frame links, and the sheet every rebuilt page's controls are
#: drawn from. A page that links a sheet of its own is skipped by the button law
#: below, because its controls may legitimately be ruled there instead.
FRAME_SHEET = ROOT / "static" / "meoww.css"


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


class TheSiteIsThreePagesAndTheyAllWork(unittest.TestCase):
    """The split itself — and the way it breaks is not a broken page.

    Splitting one document into three does not produce a 404 or a stack trace when
    it goes wrong. It produces a page that *works*, whose navigation points at a
    section that is no longer on it: a fragment cannot be redirected by a server,
    so `#features` on a page without one scrolls nowhere, with no error in any
    console and nothing an automated crawl would notice. Every assertion below is
    one of those quiet failures.
    """

    def setUp(self):
        self.pages = {path.name: path.read_text(encoding="utf-8") for path in SITE_PAGES}
        self.frame = FRAME.read_text(encoding="utf-8")

    def test_every_page_extends_the_frame_and_fills_its_own_blocks(self):
        """Re-pointed: the base is the rebuilt frame, and its blocks are named.

        It read `site_base.html` and `{% block main %}`. The frame is
        `meoww/base.html` and a page fills `{% block page %}` and names itself in
        `{% block title %}`; `tests/test_site.py` asserts that each of these pages
        reaches a shared frame at all, and this is the half that says *which*
        blocks, because a page that extends the frame and fills nothing renders a
        header, a footer and no content.
        """
        for name, markup in self.pages.items():
            with self.subTest(page=name):
                self.assertIn('{% extends "meoww/base.html" %}', markup)
                self.assertIn("{% block page %}", markup)
                self.assertIn("{% block title %}", markup)

    def test_each_page_that_gets_a_fragment_actually_has_it(self):
        """Every cross-page fragment must exist on the page it points at."""
        for name, markup in self.pages.items():
            for target in set(re.findall(r'href="(/[^"]*)#([a-z-]+)"', markup)):
                base, fragment = target
                other = "home.html" if base == "/" else base.strip("/") + ".html"
                with self.subTest(page=name, to=f"{base}#{fragment}"):
                    self.assertIn(other, self.pages, f"{base} is not a page of this site")
                    self.assertIn(f'id="{fragment}"', self.pages[other])

    def test_no_page_declares_the_same_id_twice(self):
        """The defect the dashboard's composer died of, one site over.

        Two elements with one id means `getElementById` hands back whichever came
        first, and every anchor to the second one scrolls to the first. Nothing
        about the markup looks wrong.
        """
        for name, markup in self.pages.items():
            ids = re.findall(r'\bid="([^"]+)"', markup)
            duplicates = {value for value in ids if ids.count(value) > 1}
            self.assertEqual(duplicates, set(), f"{name} declares {sorted(duplicates)} twice")


class EveryButtonIsDressedAsOne(unittest.TestCase):
    """The frame's own controls must not arrive wearing the browser's chrome.

    `meoww.css` styles `.mw-btn` and the rest of the frame's vocabulary and
    deliberately does not reset the `<button>` element, so a control a page gives
    only a colour and a size to renders with the *user agent's* background behind
    it — `buttonface` grey, the system font, a 2px black border. That is how the
    pre-rebuild tour rail and its six questions shipped as pale rows with
    near-white text on them: unreadable, and invisible in the stylesheet, because
    a rule that sets no `background` reads as a finished rule.

    The class list is therefore read from the *markup* rather than written down
    here: the way this breaks next is a control added wearing a new class, which a
    test holding its own copy of the four would not notice.
    """

    #: The frame's own vocabulary: a lowercase block, hyphens inside it, with
    #: `__element` and `--modifier` suffixes. The underscores matter — the first
    #: version of this pattern read `.mw-sim__send` as `.mw-sim`, which reported a
    #: ruled class as missing and would have passed a genuinely missing one.
    CLASS_NAME = re.compile(r"\.([a-z][a-z0-9_-]*)")

    @classmethod
    def _ruled_classes(cls, sheet: str) -> set[str]:
        """Every class name the sheet has a selector for."""
        without_comments = re.sub(r"/\*.*?\*/", "", sheet, flags=re.S)
        ruled: set[str] = set()
        for selector, _body in re.findall(r"([^{}]+)\{([^{}]*)\}", without_comments):
            ruled.update(cls.CLASS_NAME.findall(selector))
        return ruled

    def _pages_drawing_from_the_frame(self) -> list[tuple[str, str]]:
        """Templates that extend the frame and link no sheet of their own.

        A page with its own `{% block page_css %}` may rule a control there — the
        dashboard does, and its buttons are `shared/meow.css`'s `btn` — so it is
        not this law's subject. What is asserted is the frame's own pages, whose
        only sheet is the one the frame links.
        """
        found = []
        for path in sorted((ROOT / "templates").glob("*.html")):
            source = path.read_text(encoding="utf-8")
            if '{% extends "meoww/base.html" %}' not in source:
                continue
            if "{% block page_css %}" in source:
                continue
            found.append((path.name, source))
        return found

    def test_every_button_class_the_frames_pages_wear_is_ruled(self):
        pages = self._pages_drawing_from_the_frame()
        self.assertTrue(pages, "no page draws its controls from the frame's sheet")

        ruled = self._ruled_classes(FRAME_SHEET.read_text(encoding="utf-8"))
        self.assertTrue(ruled, "the frame's sheet was not parsed, so this checks nothing")

        seen = 0
        for name, markup in pages:
            for value in re.findall(r'<button[^>]*\bclass="([^"]*)"', markup, re.S):
                for token in value.split():
                    seen += 1
                    with self.subTest(page=name, button=token):
                        self.assertIn(
                            token,
                            ruled,
                            f"{name} draws a <button class=\"{token}\"> and no sheet "
                            "rules that class, so it renders with the browser's own "
                            "background and font rather than this site's",
                        )
        self.assertGreater(seen, 0, "no classed buttons were found, so nothing is checked")

    def test_the_check_would_notice_a_class_left_out(self):
        """A guard that cannot fail is decoration."""
        sheet = """.mw-a, .mw-b__inner { background: none; border: 0; }"""
        self.assertEqual(self._ruled_classes(sheet), {"mw-a", "mw-b__inner"})
        self.assertNotIn("mw-c", self._ruled_classes(sheet))


class TheMenuIsAButtonThatSaysWhatItIs(unittest.TestCase):
    """The frame's menu, and the two ways it can lie about its own state.

    It was an overlay whose `visibility` transitioned, so `focus()` was silently
    ignored until it finished, and its `aria-expanded` was set on the click rather
    than read off the panel. The rebuilt menu is a plain toggle — no focus trap, no
    overlay animation — which removes two of those failures and leaves the two that
    a toggle still owes: it must not be offered to a reader whose browser cannot
    run the script (there would be a button that does nothing), and its
    `aria-expanded` must follow the panel, because a screen reader is told the
    state and nothing else.
    """

    def setUp(self):
        self.script = (ROOT / "static" / "meoww.js").read_text(encoding="utf-8")

    def test_the_toggle_is_hidden_until_the_script_can_work_it(self):
        """The markup ships `hidden`; only a script that runs takes it off.

        The frame draws the toggle `hidden`, so a reader with JavaScript off never
        meets a control that does nothing — and the one thing that could break
        that is the script revealing it before it has wired it up.
        """
        frame = FRAME.read_text(encoding="utf-8")
        toggle = frame.split("data-mw-nav-toggle", 1)[1].split(">", 1)[0]
        self.assertIn("hidden", toggle, "the frame offers a Menu button without a script")
        self.assertIn("toggle.hidden = false;", self.script)

    def test_the_expanded_state_is_read_off_the_panel_not_the_click(self):
        """The class is the state; `aria-expanded` is a *reading* of it."""
        body = self.script.split('var toggle = document.querySelector("[data-mw-nav-toggle]")', 1)[1]
        self.assertIn('nav.classList.toggle("mw-nav--open")', body)
        self.assertIn(
            'toggle.setAttribute("aria-expanded", open ? "true" : "false")',
            body,
            "the toggle's expanded state is not the panel's own",
        )

    def test_following_a_link_closes_the_menu(self):
        """A menu that stays open behind the reader is one they close twice."""
        body = self.script.split("nav.addEventListener(\"click\"", 1)[1]
        self.assertIn('nav.classList.remove("mw-nav--open")', body)
        self.assertIn('toggle.setAttribute("aria-expanded", "false")', body)


if __name__ == "__main__":
    unittest.main()
