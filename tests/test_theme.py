"""Themes — four palettes, and the three places that have to agree.

A theme is one idea written down in three files, and every way it can break is a
disagreement between them:

* the **stylesheet** decides what a palette *is* (a block that re-points the
  accent tokens),
* the **partial** decides what a visitor can *pick* (a button named for it),
* the **script** decides what is *remembered* (a name in `localStorage`, checked
  against a list).

Add a palette to one and not the others and nothing errors: a menu appears with
a dead option, a stored choice reverts silently, or a palette that was drawn for
one hue is spent on another. That is the same defect class this repository keeps
finding in pairs of renderers — two numbers for one word — and it is cheap to
close here because all three are text.

**This file was re-pointed, and the reason is worth keeping.** It used to
describe nine palettes — `midnight`, `sakura`, `forest`, `ember`, `sunset`,
`plasma`, `aurora`, `cosmic`, `synthwave` — named in `[data-theme="…"]` blocks
in the sheet, offered by `templates/theme_picker.html` and listed in a
`THEMES` array in the script. None of those three things exists now: the palette
set is four (`dark` on `:root` plus `light`, `contrast`, and `system` as a
*choice* rather than a palette), the control is `templates/_theme.html` — a
partial, so the frame and the market page cannot offer two copies of the same
menu — and the script's list is `CHOICES`. A law that reads a retired subject
does not fail loudly, it fails *permanently*: thirty-five assertions about files
nobody ships, in a file whose whole purpose is to catch exactly that kind of
drift. So the laws below are the same laws with today's names on them, and the
three-copies-agree shape is deliberately unchanged.

The two properties that are not about agreement are pinned for the same reason
they always were: the theme script has to run **before the first paint**, which
is a property of where it sits in the markup rather than of what it does, and the
default palette has to stay `:root`, so a visitor who has chosen nothing — or
whose stored choice has since been retired — still gets a whole palette rather
than an attribute naming a block that no longer exists.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHEET = ROOT / "static" / "meow.css"
SCRIPT = ROOT / "static" / "theme.js"
PARTIAL = ROOT / "templates" / "_theme.html"

#: The layout the public pages share, where the head lives. "Before the paint"
#: is a property of one file rather than of every page, which is the point of the
#: base — so the assertions below read it and then check the pages reach it.
BASE = ROOT / "templates" / "site_base.html"

#: Pages that extend the shared base. A page that wears the site's look without
#: the site's head gets no theme and no error either.
SHARED = ("home.html", "systems.html", "start.html", "pricing.html", "radio.html")

#: The palettes the sheet declares, and the one that is the stylesheet's default
#: rather than a block. `system` is a *choice* the visitor makes, not a palette:
#: it resolves to one of these two inside the script.
PALETTES = ("light", "contrast")
DEFAULT = "dark"
#: …and everything the picker offers, which is those plus the default and the
#: follow-the-operating-system setting.
CHOICES = ("system", "light", "dark", "contrast")


def _blocks(css: str) -> dict[str, str]:
    """The palette blocks, as name → declaration text."""
    return {
        match.group(1): match.group(2)
        for match in re.finditer(r'\[data-theme="([a-z]+)"\]\s*\{(.*?)\n\}', css, re.DOTALL)
    }


def _tokens(block: str) -> set[str]:
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))


def _values(block: str) -> dict[str, str]:
    """The palette's declarations as name → value, for the numbers that matter."""
    return {
        match.group(1): match.group(2).strip()
        for match in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)
    }


def _luminance(colour: str) -> float:
    """Relative luminance of a ``#rrggbb``, the way WCAG defines it."""
    text = colour.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    channels = [int(text[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [
        value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(one: str, other: str) -> float:
    high, low = sorted((_luminance(one), _luminance(other)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class TheAccentFillCarriesInkYouCanRead(unittest.TestCase):
    """Every palette's `--text-invert` clears 4.5:1 on that palette's `--accent`.

    The pair no other law looks at. The design laws measure the accent **as
    text** (`--accent-text` on a surface) and the accent as a **graphic** (3:1 for
    fills and rings), and `--text-invert` is neither: it is the ink painted *on*
    the fill, and it is what the brand mark's K and the primary button's label are
    drawn in.

    In the light palette it was white on `#0d9488` — **3.74:1**, in the palette a
    visitor gets by choosing Light, while the dark and high-contrast palettes
    measured 10.6:1 and 14.2:1. Nothing failed, because a colour that is merely
    hard to read is not an error anywhere a browser reports one: it is a button
    whose label a person has to look twice at. Both halves are asserted for the
    reason the token set is — a palette may move the fill or move the ink, and the
    only thing that cannot move is the ratio between them.
    """

    def setUp(self):
        css = SHEET.read_text(encoding="utf-8")
        root = re.search(r"^:root\s*\{(.*?)\n\}", css, re.DOTALL | re.MULTILINE)
        self.assertTrue(root, "the sheet declares no :root palette")
        self.palettes = {DEFAULT: _values(root.group(1))}
        for name, block in _blocks(css).items():
            self.palettes[name] = _values(block)

    def test_every_palette_paints_readable_ink_on_its_accent(self):
        self.assertIn(f"--text-invert", self.palettes[DEFAULT], "the default palette is unreadable")
        for name, values in self.palettes.items():
            with self.subTest(palette=name):
                self.assertIn("--accent", values, f"{name} declares no accent")
                self.assertIn("--text-invert", values, f"{name} declares no invert")
                ratio = _contrast(values["--text-invert"], values["--accent"])
                self.assertGreaterEqual(
                    round(ratio, 2),
                    4.5,
                    f"{name}: --text-invert {values['--text-invert']} on --accent "
                    f"{values['--accent']} is {ratio:.2f}:1, below the 4.5:1 body text "
                    "needs — the brand mark and the primary button are drawn in it",
                )

    def test_the_pair_is_actually_read_by_something(self):
        """A palette cannot satisfy the ratio by having the names but not using them."""
        css = SHEET.read_text(encoding="utf-8")
        self.assertIn("color: var(--text-invert)", css)
        self.assertIn("background: var(--accent)", css)


class TheThreeCopiesAgree(unittest.TestCase):
    """The sheet, the partial and the script list the same palettes."""

    def setUp(self):
        self.css = SHEET.read_text(encoding="utf-8")
        self.script = SCRIPT.read_text(encoding="utf-8")
        self.partial = PARTIAL.read_text(encoding="utf-8")
        self.blocks = _blocks(self.css)

    def test_the_stylesheet_defines_every_palette_and_nothing_else(self):
        self.assertEqual(sorted(self.blocks), sorted(PALETTES), self.blocks.keys())

    def test_every_palette_defines_the_same_tokens(self):
        """A block missing one token inherits the *default* for it.

        That is the failure worth pinning: a palette that forgets `--accent-rgb`
        keeps the house hue inside every glow, ring and gradient stop while its
        buttons change — which reads as a wrong *colour* rather than as a missing
        line, and is why the shared token set is asserted rather than each value.
        """
        sets = {name: _tokens(block) for name, block in self.blocks.items()}
        expected = _tokens(self.blocks[PALETTES[0]])
        self.assertGreaterEqual(len(expected), 10, "a palette block looks empty")
        for name, tokens in sets.items():
            self.assertEqual(tokens, expected, f"{name} defines a different token set")
        # Both halves of the accent, and both channel sets — a rebrand that moves
        # the fill and leaves the shade used for text is the defect these four
        # names exist to prevent.
        for name in ("accent", "accent-text", "accent-rgb", "accent-text-rgb"):
            self.assertIn(f"--{name}", expected, f"the palettes no longer carry --{name}")

    def test_the_default_is_the_root_block_and_not_a_theme(self):
        """Otherwise a visitor who picks nothing gets a half-applied palette."""
        declared = re.search(r":root\s*\{(.*?)\n\}", self.css, re.DOTALL).group(1)
        self.assertIn("--accent:", declared)
        self.assertIn("--accent-rgb:", declared)
        self.assertNotIn(
            DEFAULT,
            self.blocks,
            "the default grew a block of its own, so a visitor who has chosen "
            "nothing (or whose stored choice was retired) gets a partial palette",
        )

    def test_the_picker_offers_exactly_the_palettes_that_exist(self):
        offered = re.findall(r'data-theme-set="([a-z]+)"', self.partial)
        self.assertEqual(sorted(set(offered)), sorted(CHOICES))
        self.assertEqual(len(offered), len(set(offered)), "an option is listed twice")

    def test_the_script_knows_every_palette(self):
        listed = re.search(r"var CHOICES = \[(.*?)\]", self.script, re.DOTALL).group(1)
        named = set(re.findall(r'"([a-z]+)"', listed))
        self.assertEqual(named, set(CHOICES))

    def test_the_script_validates_a_stored_choice_against_that_list(self):
        """A retired palette left in `localStorage` must not survive.

        Set on `<html>` unvalidated, it would be an attribute no stylesheet
        matches — so the page would render its default palette while the picker,
        reading the same value, said something else. The lookup is the guard, and
        this is the assertion that it is still there.
        """
        self.assertIn("CHOICES.indexOf(value) === -1", self.script)
        self.assertRegex(
            self.script, r"CHOICES\.indexOf\(value\) === -1 \? DEFAULT : value"
        )

    def test_system_is_a_choice_rather_than_a_palette(self):
        """`system` must resolve, not be applied.

        If `system` were written straight onto `<html>` there would be no such
        palette and the page would render the default while the menu said
        "System" — the visitor's operating-system preference silently ignored.
        """
        self.assertIn('if (choice === "system")', self.script)
        self.assertRegex(self.script, r'return dark \? "dark" : "light"')


class ItHasToBeThereBeforeThePaint(unittest.TestCase):
    """`defer` would flash. That is a property of the markup, not the script."""

    def test_the_shared_base_loads_it_and_not_lazily(self):
        head = BASE.read_text(encoding="utf-8").split("</head>", 1)[0]
        tags = re.findall(r"<script[^>]*theme\.js[^>]*>", head)
        self.assertTrue(tags, "the shared base does not load theme.js in its head")
        for tag in tags:
            self.assertNotIn("defer", tag, "the base defers the theme script")
            self.assertNotIn("async", tag, "the base loads the theme script async")

    def test_the_script_is_versioned_like_every_other_asset(self):
        """A year-long cache on a token that never changes is a stale script."""
        markup = BASE.read_text(encoding="utf-8")
        for src in re.findall(r'src="(/static/theme\.js[^"]*)"', markup):
            self.assertRegex(src, r"\?v=\{\{ asset_version\('theme\.js'\) \}\}$")

    def test_nothing_waits_for_the_dom_to_apply_a_palette(self):
        """The apply happens at the top level; only the wiring waits.

        The two must not be swapped: wiring on `DOMContentLoaded` *before*
        applying would leave the palette to be set after the first paint on every
        page, which is the flash the blocking load exists to avoid.
        """
        body = SCRIPT.read_text(encoding="utf-8")
        apply_at = body.index("\n  paint(stored());")
        wire_at = body.index('document.addEventListener("DOMContentLoaded"')
        self.assertLess(apply_at, wire_at)

    def test_every_shared_page_extends_that_base(self):
        """A page that does not extend it gets no theme, and no error either."""
        for name in SHARED:
            markup = (ROOT / "templates" / name).read_text(encoding="utf-8")
            self.assertRegex(
                markup,
                r'\{%\s*extends "site_base\.html"\s*%\}',
                f"{name} does not extend the shared base",
            )


class TheScriptIsSafe(unittest.TestCase):
    """A script on every public page, so the cheap checks are worth having."""

    def test_it_writes_no_markup_and_evaluates_nothing(self):
        body = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function",
            "document.write",
        ):
            self.assertNotIn(forbidden, body, f"theme.js uses {forbidden}")

    def test_every_hook_the_script_reads_is_in_the_partial(self):
        """A `data-` attribute is a contract between two files.

        The script queries `[data-theme-toggle]`, `[data-theme-label]` and
        `[data-theme-set]`; the partial is the only thing that provides them.
        Rename one and the picker stops working with no error anywhere —
        `querySelectorAll` on a selector nothing matches returns an empty list and
        the code simply does nothing.
        """
        script = SCRIPT.read_text(encoding="utf-8")
        partial = PARTIAL.read_text(encoding="utf-8")
        hooks = set(re.findall(r"\[(data-theme-[a-z-]+)\]", script))
        self.assertGreaterEqual(len(hooks), 3, "the hook list looks wrong")
        for hook in hooks:
            self.assertIn(hook, partial, f"{hook} is queried but never rendered")

    def test_the_menu_the_script_opens_is_the_one_the_partial_draws(self):
        """`getElementById` is a second kind of hook, and just as silent."""
        script = SCRIPT.read_text(encoding="utf-8")
        partial = PARTIAL.read_text(encoding="utf-8")
        ids = set(re.findall(r'getElementById\("([a-z-]+)"\)', script))
        self.assertTrue(ids, "the script no longer looks up the menu by id")
        for menu_id in ids:
            self.assertIn(f'id="{menu_id}"', partial, f"#{menu_id} is looked up but never rendered")

    def test_it_leaves_the_browser_chrome_to_the_page(self):
        """`theme-color` belongs to the page, and the pages disagree on purpose.

        A palette moves the *accent*; the browser chrome follows the page's
        background, which is a per-page constant. This script used to overwrite
        all of them with one hardcoded value, so every page advertised a colour
        that matched no page's background — two writers, one attribute.

        Asserted on what the code *does* rather than on the word: the first
        version of this check forbade the string `theme-color` and failed on the
        comment explaining why the string is gone.
        """
        body = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('meta[name="theme-color"]', body)
        self.assertNotIn('setAttribute("content"', body)
        # The frame states its own, from the one author of the palette, and the
        # market page states a literal of its own — so the two disagree on
        # purpose and the script has no business reconciling them.
        base = BASE.read_text(encoding="utf-8")
        self.assertIn('name="theme-color" content="{{ brand.BG_HEX }}"', base)
        market = (ROOT / "templates" / "market.html").read_text(encoding="utf-8")
        self.assertRegex(market, r'name="theme-color" content="#[0-9a-f]{6}"')

    def test_a_refused_storage_does_not_throw(self):
        """Private mode, blocked cookies and a `file://` open all throw here."""
        body = SCRIPT.read_text(encoding="utf-8")
        self.assertGreaterEqual(body.count("catch (error)"), 2, "storage is not guarded")


if __name__ == "__main__":
    unittest.main()
