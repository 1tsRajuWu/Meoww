"""Themes — four of them, and the three places that have to agree.

A theme is one idea written down in three files, and every way it can break is a
disagreement between them:

* the **stylesheet** decides what a theme *is* (a block that re-points the
  accent tokens),
* the **partial** decides what a visitor can *pick* (a button named for it),
* the **script** decides what is *remembered* (a name in `localStorage`, checked
  against a list).

Add a theme to one and not the others and nothing errors: a menu appears with a
dead option, a stored choice reverts silently, or a palette that was drawn for
one hue is spent on another. That is the same defect class this repository keeps
finding in pairs of renderers — two numbers for one word — and it is cheap to
close here because all three are text.

Two more properties are pinned below because they are the kind that rot without
a symptom: the theme script has to run **before the first paint**, which is a
property of where it sits in the markup rather than of what it does, and the
default theme has to stay `:root`, so a visitor who has chosen nothing — or whose
stored choice has since been retired — still gets a whole palette rather than an
attribute naming a block that no longer exists.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHEET = ROOT / "static" / "meow.css"
SCRIPT = ROOT / "static" / "theme.js"
PARTIAL = ROOT / "templates" / "theme_picker.html"

#: Every template that renders a page a visitor can land on. The theme has to be
#: on `<html>` before the paint whichever page is the first one opened, so each of
#: these reaches the script through the shared base below.
PAGES = (
    "home.html",
    "systems.html",
    "start.html",
    "pricing.html",
    "legal.html",
    "login.html",
    "servers.html",
    "dashboard.html",
    "console.html",
    "market_live.html",
)

#: The layout the three site pages share. The head lives here now, which is what
#: makes "before the paint" a property of one file rather than of eight.
BASE = ROOT / "templates" / "site_base.html"

#: The three that reach it. The consoles and the policies carry their own head,
#: so they are covered by the assertions on `BASE` and not by this one.
SHARED = ("home.html", "systems.html", "start.html")

#: The themes, and the one that is the stylesheet's default rather than a block.
THEMES = ("sakura", "forest", "ember", "sunset", "plasma", "aurora", "cosmic", "synthwave")
DEFAULT = "midnight"


def _blocks(css: str) -> dict[str, str]:
    """The theme blocks, as name → declaration text."""
    found = {}
    for match in re.finditer(r'\[data-theme="([a-z]+)"\]\s*\{(.*?)\}', css, re.DOTALL):
        found[match.group(1)] = match.group(2)
    return found


def _tokens(block: str) -> set[str]:
    return set(re.findall(r"(--[a-z-]+)\s*:", block))


class TheThreeCopiesAgree(unittest.TestCase):
    """The stylesheet, the partial and the script list the same themes."""

    def setUp(self):
        self.css = SHEET.read_text(encoding="utf-8")
        self.script = SCRIPT.read_text(encoding="utf-8")
        self.partial = PARTIAL.read_text(encoding="utf-8")
        self.blocks = _blocks(self.css)

    def test_the_stylesheet_defines_every_theme_and_nothing_else(self):
        self.assertEqual(sorted(self.blocks), sorted(THEMES))

    def test_every_theme_defines_the_same_tokens(self):
        """A block missing one token inherits the *default* for it.

        That is the failure worth pinning: a theme that forgets `--accent-rgb`
        keeps the default hue inside every glow, ring and gradient stop while its
        buttons change — which reads as a wrong colour rather than as a missing
        line, and is why the shared token set is asserted rather than each value.
        """
        sets = {name: _tokens(block) for name, block in self.blocks.items()}
        expected = _tokens(self.blocks[THEMES[0]])
        self.assertGreaterEqual(len(expected), 5, "a theme block looks empty")
        for name, tokens in sets.items():
            self.assertEqual(tokens, expected, f"{name} defines a different token set")
        self.assertIn("--accent-rgb", expected)
        self.assertIn("--accent-text-rgb", expected)

    def test_the_default_is_the_root_block_and_not_a_theme(self):
        """Otherwise a visitor who picks nothing gets a half-applied palette."""
        declared = re.search(r":root\s*\{(.*?)\n\}", self.css, re.DOTALL).group(1)
        self.assertIn("--accent:", declared)
        self.assertIn("--accent-rgb:", declared)
        self.assertNotIn(DEFAULT, self.blocks, "the default grew a block of its own")

    def test_the_picker_offers_exactly_the_themes_that_exist(self):
        offered = re.findall(r'data-theme-value="([a-z]+)"', self.partial)
        self.assertEqual(sorted(set(offered)), sorted({DEFAULT, *THEMES}))
        self.assertEqual(len(offered), len(set(offered)), "an option is listed twice")

    def test_the_script_knows_every_theme(self):
        listed = re.search(r"var THEMES = \[(.*?)\]", self.script, re.DOTALL).group(1)
        named = set(re.findall(r'"([a-z]+)"', listed))
        self.assertEqual(named, {DEFAULT, *THEMES})

    def test_the_script_validates_a_stored_choice_against_that_list(self):
        """A retired theme left in `localStorage` must not survive.

        Set on `<html>` unvalidated, it would be an attribute no stylesheet
        matches — so the page would render its default palette while the picker,
        reading the same value, said something else. The lookup is the guard, and
        this is the assertion that it is still there.
        """
        self.assertIn("THEMES.indexOf(value) === -1", self.script)
        self.assertRegex(self.script, r"return THEMES\.indexOf\(value\) === -1 \? DEFAULT : value")

    def test_the_swatch_colours_are_written_down_once_each(self):
        """The menu is a palette, and four identical dots are not one.

        Asserted as *distinct values* rather than as specific hexes: the point is
        that each option shows its own colour, and a designer changing Sakura
        should have to change one line, not two agreeing ones.
        """
        colours = re.findall(r'\.theme-option\[data-theme-value="[a-z]+"\] \.theme-swatch \{ background: (#[0-9a-f]{6}); \}', self.css)
        self.assertEqual(len(colours), len(THEMES) + 1, "every theme needs a swatch")
        self.assertEqual(len(set(colours)), len(colours), "two themes show one colour")


class ItHasToBeThereBeforeThePaint(unittest.TestCase):
    """`defer` would flash. That is a property of the markup, not the script.

    Asserted on the shared base rather than page by page, because the head moved
    there when the one long landing page became three. A per-page loop over the
    pages would now find three files with **no head at all** — `re.findall` on a
    missing `<script>` returns an empty list — so the two halves are pinned apart:
    the base carries it, and every page reaches the base.
    """

    def test_the_shared_base_loads_it_and_not_lazily(self):
        head = BASE.read_text(encoding="utf-8").split("</head>", 1)[0]
        tags = re.findall(r"<script[^>]*theme\.js[^>]*>", head)
        self.assertTrue(tags, "the shared base does not load theme.js in its head")
        for tag in tags:
            self.assertNotIn("defer", tag, "the base defers the theme script")
            self.assertNotIn("async", tag, "the base loads the theme script async")

    def test_every_shared_page_extends_that_base(self):
        """A page that does not extend it gets no theme, and no error either."""
        for name in SHARED:
            markup = (ROOT / "templates" / name).read_text(encoding="utf-8")
            self.assertRegex(
                markup,
                r'\{%\s*extends "site_base\.html"\s*%\}',
                f"{name} does not extend the shared base",
            )

    def test_the_pages_of_the_site_are_the_three_the_layout_knows(self):
        """So a fourth page cannot be added without a head that carries the theme.

        The list above is hand-kept, and a hand-kept list is how this whole
        project keeps finding a surface nobody updated. The check that closes it:
        any template that renders a `body class="landing"` is one of the three.
        """
        landing = set()
        for path in (ROOT / "templates").glob("*.html"):
            if 'class="landing"' in path.read_text(encoding="utf-8"):
                landing.add(path.name)
        # `site_base.html` *is* the layout, so it is the one file expected to
        # declare the body class; anything else with one is a page that has taken
        # the look without the head that carries the theme.
        self.assertEqual(
            landing - set(SHARED) - {BASE.name}, set(),
            "a page wears the site's body class without the site's layout",
        )
        self.assertIn(BASE.name, landing, "the layout stopped declaring the body class")

    def test_the_script_is_versioned_like_every_other_asset(self):
        """A year-long cache on a token that never changes is a stale script."""
        markup = BASE.read_text(encoding="utf-8")
        for src in re.findall(r'src="(/static/theme\.js[^"]*)"', markup):
            self.assertRegex(src, r"\?v=\{\{ asset_version\('theme\.js'\) \}\}$")

    def test_nothing_waits_for_the_dom_to_apply_a_theme(self):
        """The apply happens at the top level; only the wiring waits.

        The two must not be swapped: wiring on `DOMContentLoaded` *before*
        applying would leave the theme to be set after the first paint on every
        page, which is the flash the blocking load exists to avoid.
        """
        body = SCRIPT.read_text(encoding="utf-8")
        apply_at = body.index("\n    apply(stored());")
        wire_at = body.index('document.addEventListener("DOMContentLoaded", wire')
        self.assertLess(apply_at, wire_at)
        # ...and the bare call is above the `if (document.readyState ...)` guard
        self.assertLess(apply_at, body.index("if (document.readyState === \"loading\")"))


class TheScriptIsSafe(unittest.TestCase):
    """A new script on a public page, so the cheap checks are worth having."""

    def test_it_writes_no_markup_and_evaluates_nothing(self):
        body = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write"):
            self.assertNotIn(forbidden, body, f"theme.js uses {forbidden}")

    def test_every_hook_the_script_reads_is_in_the_partial(self):
        """A `data-` attribute is a contract between two files.

        The script queries `[data-theme-picker]`, `[data-theme-menu]`,
        `[data-theme-open]`, `[data-theme-name]` and `[data-theme-value]`; the
        partial is the only thing that provides them. Rename one and the picker
        stops working with no error anywhere — `querySelectorAll` on a selector
        nothing matches returns an empty list and the code simply does nothing.
        """
        script = SCRIPT.read_text(encoding="utf-8")
        partial = PARTIAL.read_text(encoding="utf-8")
        hooks = set(re.findall(r"\[(data-theme-[a-z-]+)\]", script))
        self.assertGreaterEqual(len(hooks), 5, "the hook list looks wrong")
        for hook in hooks:
            self.assertIn(hook, partial, f"{hook} is queried but never rendered")

    def test_it_leaves_the_browser_chrome_to_the_page(self):
        """`theme-color` belongs to the page, and the pages disagree on purpose.

        Three different values are declared across the eight templates, because
        the two consoles sit on a slightly bluer black than the landing page.
        A theme moves the *accent*, so nothing here has any business rewriting
        that meta — and a script that did would replace all three values with a
        fourth that matches no page's background.
        """
        self.assertNotIn('meta[name="theme-color"]', SCRIPT.read_text(encoding="utf-8"))
        self.assertNotIn('setAttribute("content"', SCRIPT.read_text(encoding="utf-8"))
        # The base carries the default inside a block and the document pages
        # override it, so the value is matched *inside* whatever the `content`
        # attribute holds rather than as the whole attribute.
        declared = set()
        for path in [*(ROOT / "templates" / name for name in PAGES), BASE]:
            markup = path.read_text(encoding="utf-8")
            declared.update(re.findall(r'name="theme-color" content="[^"]*?(#[0-9a-f]{6})', markup))
        self.assertGreaterEqual(len(declared), 2, "the pages no longer declare their own chrome")

    def test_a_refused_storage_does_not_throw(self):
        """Private mode, blocked cookies and a `file://` open all throw here."""
        body = SCRIPT.read_text(encoding="utf-8")
        self.assertGreaterEqual(body.count("catch (error)"), 2, "storage is not guarded")


if __name__ == "__main__":
    unittest.main()
