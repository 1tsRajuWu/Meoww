"""The dashboard's shell, after it stopped being a 1,660-line page.

Four properties, each one an edit away from being quietly undone, and each one
found by driving the rebuilt page in a real browser rather than by reading it:

* **The layout lives in a stylesheet, not in the template.** The inline `<style>`
  block had grown to the point where this template was the third-largest file in
  the repository and a change to one card was a change to a page. It now carries
  two rules that genuinely belong to the *document* (its own palette-open state
  and its own paper) and nothing else.
* **The rail is a map of the page.** Every anchor it offers has to land on a
  section that exists; a link to a section somebody deleted scrolls nowhere and
  looks like a broken button.
* **The command palette is markup the page carries and script drives.** It is
  the page's only way to reach a system without scrolling three thousand pixels,
  and it is invisible to a reader of the Python: a template edit that drops it
  leaves a file that still renders.
* **The custom cursor may never be the only pointer.** `cursor: none` is a rule
  that hides the pointer, and the layer that draws a replacement is script. The
  hiding is scoped to a class that script adds *after* the layer is in the
  document, and reduced motion gives the pointer back; a bare `cursor: none`
  anywhere in the sheet is a page with no cursor at all on a script that fails.
  Measured live, that ordering holds: the computed cursor is `none` while the
  layer is running, and the cat SVG otherwise.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _rules(sheet: str):
    """Yield ``(chain, declarations)`` for every leaf rule in a stylesheet.

    Brace-matched rather than split on ``}``, and that is not fastidiousness.
    The first version of this helper split on ``}``, which handed the first rule
    inside each ``@media`` a "selector" that was the whole file up to that point
    — so a *bare* ``cursor: none`` would have passed the check below by picking
    up the word ``has-meow-cursor`` from a rule hundreds of lines earlier. A
    guard that passes on exactly the edit it exists to catch is worse than no
    guard, so this walks the braces and carries the full selector chain
    (``@media ...`` included) down to every declaration block.

    Comments are removed first, because they are prose and prose contains
    braces: the sheet discusses ``cursor: none`` in a paragraph explaining why
    it is scoped, and a naive reader would find a rule in that sentence.
    """
    clean = re.sub(r"/\*.*?\*/", "", sheet, flags=re.S)
    return _leaves(clean)


def _leaves(text: str, chain: tuple = ()):
    cursor = 0
    while True:
        start = text.find("{", cursor)
        if start == -1:
            return
        selector = " ".join(text[cursor:start].split())
        depth, end = 1, start + 1
        while end < len(text) and depth:
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
            end += 1
        body = text[start + 1:end - 1]
        if "{" in body:
            # An at-rule, or a rule nested inside one: the selector is context
            # for everything inside it rather than a rule of its own.
            inside = chain + (selector,) if selector else chain
            yield from _leaves(body, inside)
        else:
            yield chain + (selector,), body
        cursor = end


class TheLayoutIsAStylesheet(unittest.TestCase):
    """A page is not where a design system lives."""

    def setUp(self):
        self.page = _read("templates/dashboard.html")
        self.sheet = _read("static/dashboard.css")

    def test_the_page_links_the_sheet(self):
        self.assertIn("/static/dashboard.css", self.page)

    def test_the_page_no_longer_carries_the_layout(self):
        """The card and control rules are the sheet's, not the template's."""
        for selector in (".module-card {", ".metric-card {", ".composer-panel {",
                         ".rail-nav {", ".palette {"):
            self.assertNotIn(selector, self.page, f"{selector} is layout, and it is back in the page")

    def test_the_sheet_is_where_the_shell_is_defined(self):
        self.assertIn(".app {", self.sheet)
        app = self.sheet.split(".app {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-template-columns", app, "the shell's own grid left the sheet")

    def test_the_document_level_rules_that_stayed_are_the_two_that_belong(self):
        style = self.page.split("<style>", 1)[1].split("</style>", 1)[0]
        body = "\n".join(
            line for line in style.splitlines() if not line.strip().startswith(("*", "/"))
        )
        self.assertIn("body.is-palette-open", body)
        self.assertIn("@media print", body)
        # Roughly: two rules and their braces. The number is a tripwire, not a
        # measurement — it moves when somebody adds a third *document* rule,
        # which is the moment to ask whether it belongs in the sheet instead.
        self.assertLess(len(body.splitlines()), 20, "the inline stylesheet is growing back")


class NoTwoElementsShareAnId(unittest.TestCase):
    """An id is unique by definition, and the page's script depends on it.

    This is not housekeeping. The composer's section heading was
    ``<h2 id="composer-title">`` and the embed-title *input* was
    ``id="composer-title"`` too, so ``getElementById("composer-title")`` handed
    the script a heading — an element with no ``.value`` — and every call to the
    composer's rulebook died on ``.value.trim()``. The button that sends a
    message was therefore disabled *forever*, the counter and the reason under
    the box disagreed with the ticks beside them, and the only trace anywhere was
    a `TypeError` in the browser console. A duplicate id is silent, plausible and
    fatal, which is exactly the kind of defect a template needs a guard for: it
    renders, the server is fine, and every test that reads the markup passes.
    """

    #: Every page this project renders from a template.
    PAGES = (
        "templates/dashboard.html",
        "templates/owner.html",
        "templates/home.html",
        "templates/login.html",
        "templates/servers.html",
        "templates/market_live.html",
        "templates/privacy.html",
        "templates/terms.html",
        "templates/pricing.html",
    )

    def test_no_template_declares_the_same_id_twice(self):
        checked = 0
        for rel in self.PAGES:
            path = ROOT / rel
            if not path.exists():
                continue
            checked += 1
            ids = re.findall(r'\bid="([^"]+)"', path.read_text(encoding="utf-8"))
            seen, dupes = set(), []
            for element_id in ids:
                if element_id in seen and element_id not in dupes:
                    dupes.append(element_id)
                seen.add(element_id)
            with self.subTest(template=rel):
                self.assertEqual(dupes, [], f"{rel} declares the same id twice: {dupes}")
        self.assertGreater(checked, 3, "the templates were not found, so nothing was checked")

    def test_every_id_the_page_script_looks_up_exists_in_the_page(self):
        """The rendered page, not the template: Jinja can drop an element.

        This is the general form of the defect above. A rebuild renames markup,
        and the script keeps asking for the old name — 65 ids here, and the one
        that broke was found by a browser console rather than by any test,
        because a missing element is not a template error and every assertion
        that reads the markup still passes.
        """
        from tests.test_composer import render_dashboard

        page = render_dashboard()
        scripts = "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page, flags=re.S))
        looked_up = sorted(set(re.findall(r'getElementById\(\s*"([^"]+)"\s*\)', scripts)))
        self.assertGreater(len(looked_up), 40, "the page's script was not found")

        missing = [i for i in looked_up if page.count(f'id="{i}"') == 0]
        self.assertEqual(missing, [], f"the script looks up ids the page does not have: {missing}")

        duplicated = [i for i in looked_up if page.count(f'id="{i}"') > 1]
        self.assertEqual(duplicated, [], f"the script looks up an ambiguous id: {duplicated}")

    def test_the_ids_the_composer_script_looks_up_are_not_headings(self):
        """The specific shape that broke: a looked-up id landing on a non-input.

        A weaker version of this test would just re-assert the duplicate check
        from the other direction. What is worth pinning directly is that the
        *first* element carrying each id the script reads has a ``value`` — a
        heading, a div or a label that shadows an input is the defect, however
        many ids there are.
        """
        page = _read("templates/dashboard.html")
        for element_id in ("composer-body", "composer-title", "composer-screen"):
            first = re.search(r'<([a-z0-9]+)[^>]*\bid="' + element_id + r'"', page)
            if first is None:
                continue
            self.assertIn(
                first.group(1), ("input", "textarea", "select"),
                f"`{element_id}` is read as a form control but the first element with "
                f"that id is a <{first.group(1)}>",
            )


class TheRailIsAMapOfThePage(unittest.TestCase):
    """Every anchor offered has to land somewhere."""

    def test_every_rail_anchor_lands_on_a_section(self):
        page = _read("templates/dashboard.html")
        anchors = set(re.findall(r'data-spy="([a-z-]+)"', page))
        sections = set(re.findall(r'<section id="([a-z-]+)"', page))
        self.assertGreater(len(anchors), 3, "the rail's anchors were not found")
        self.assertEqual(anchors, sections, "the rail offers a section the page does not have")


class ThePaletteIsServed(unittest.TestCase):
    """The page carries it; the script drives it."""

    def setUp(self):
        self.page = _read("templates/dashboard.html")

    def test_the_trigger_and_the_dialog_are_both_there(self):
        self.assertIn('id="palette-open"', self.page)
        self.assertIn('role="dialog"', self.page)
        self.assertIn('id="palette-input"', self.page)

    def test_the_empty_state_says_something_rather_than_showing_nothing(self):
        """A dialog that goes blank on a typo reads as a broken dialog."""
        self.assertIn("Nothing matches that", self.page)

    def test_it_is_reachable_by_keyboard_and_says_so(self):
        self.assertIn("Ctrl", self.page)
        self.assertIn('aria-haspopup="dialog"', self.page)


class TheCursorCannotBeTheOnlyPointer(unittest.TestCase):
    """Hiding the pointer and drawing a new one are two halves of one deal.

    The order matters and it is asserted here as an order rather than as two
    facts: a class added before the layer is appended would hide the native
    cursor for the frame or two before the replacement is in the document, and a
    bare `cursor: none` would hide it permanently if the script never ran.
    """

    def setUp(self):
        self.sheet = _read("static/meow.css")
        self.script = _read("static/motion.js")

    def test_both_cat_cursors_are_committed_assets(self):
        for name in ("cursor-default.svg", "cursor-pointer.svg"):
            self.assertTrue((ROOT / "static" / name).exists(), f"{name} is missing; the sheet points at it")

    def test_the_sheet_uses_them_as_the_base_layer(self):
        self.assertIn("/static/cursor-default.svg", self.sheet)
        self.assertIn("/static/cursor-pointer.svg", self.sheet)

    def test_hiding_the_pointer_is_scoped_to_the_class_the_script_adds(self):
        """No rule may ever hide the pointer on a page where nothing replaces it."""
        for chain, declarations in _rules(self.sheet):
            if "cursor: none" in declarations:
                selector = " ".join(chain)
                self.assertIn(
                    "has-meow-cursor", selector,
                    f"`cursor: none` applied by `{selector}`, which the script does not gate",
                )

    def test_the_class_is_added_after_the_layer_exists(self):
        appended = self.script.index("document.body.appendChild(host)")
        hidden = self.script.index('classList.add("has-meow-cursor")')
        self.assertLess(appended, hidden, "the pointer is hidden before the cat is drawn")

    def test_reduced_motion_gives_the_pointer_back(self):
        """Both halves, and in the *cursor's* block rather than the first match.

        The sheet has more than one reduced-motion block — the animations have
        their own — and the first version of this test read whichever came first
        in the file and then complained about CSS that was never about cursors.
        """
        reduced = [
            (chain, body) for chain, body in _rules(self.sheet)
            if any("prefers-reduced-motion" in part for part in chain)
        ]
        self.assertTrue(reduced, "the reduced-motion block is gone")
        self.assertTrue(
            any(".meow-cursor" in " ".join(chain) for chain, _ in reduced),
            "the layer still draws when motion is not wanted",
        )
        self.assertTrue(
            any("cursor: auto" in body for _, body in reduced),
            "a reduced-motion visitor would keep an invisible pointer",
        )

    def test_a_coarse_pointer_never_gets_the_layer(self):
        """Touch has no hover to enhance, and no cursor to replace."""
        self.assertIn("fine", self.script)
        self.assertIn("pointerLayer", self.script)


class BothSurfacesWearIt(unittest.TestCase):
    """One system, three pages — the complaint that started this."""

    def test_each_page_links_the_shared_system(self):
        for rel in ("templates/home.html", "templates/dashboard.html", "templates/owner.html"):
            page = _read(rel)
            with self.subTest(page=rel):
                self.assertIn("/static/meow.css", page)
                self.assertIn("/static/motion.js", page)

    def test_the_two_control_pages_share_one_shell_sheet(self):
        """The console reuses the dashboard's layout rather than inventing one.

        A second control room with its own rail, cards and palette is two
        products, and the whole point of the rebuild was that there is one.
        """
        for rel in ("templates/dashboard.html", "templates/owner.html"):
            with self.subTest(page=rel):
                self.assertIn("/static/dashboard.css", _read(rel))


class NoInlineSvgIsLeftToGuessItsSize(unittest.TestCase):
    """A bare inline `<svg>` is a 300×150 black shape.

    The ``.search-icon`` class was written for a **text** glyph (``🔍``) and so
    carries no width, height or fill — perfectly safe for a span, and a ~173 px
    solid black disc for an `<svg>`, which has neither a font size to inherit nor
    any reason to default to ``fill: none``. It was drawn straight across the
    owner console's filter box and found only by rendering the page.

    Two rules cover the two ways out: the class is sized and stroked for an SVG,
    and every ``<svg>`` in these templates is inside something that sizes it. The
    second is the general one, and it is checked against the real rule set rather
    than by pattern-matching the sheet.
    """

    #: Classes the shared sheets give an explicit width and height to.
    SIZED = (
        r"svg\.search-icon",
        r"\.rail-nav a \.rail-icon",
        r"\.rail-fleet \.rail-icon",
        r"\.fleet-open svg",
        r"\.btn svg",
    )

    def test_the_search_icon_is_sized_and_stroked_for_an_svg(self):
        rules = list(_rules(_read("static/dashboard.css")))
        found = {}
        for chain, body in rules:
            if "svg.search-icon" in chain:
                found[chain] = body
        self.assertTrue(found, "no rule sizes an SVG wearing .search-icon")
        body = "".join(found.values())
        for declaration in ("width:", "height:", "fill: none", "stroke:"):
            with self.subTest(declaration=declaration):
                self.assertIn(declaration, body)

    def test_the_sizing_rule_wins_over_the_class_alone(self):
        """``.search-icon`` sets no width, so a tie would leave the disc.

        Specificity, not order: ``svg.search-icon`` (0,1,1) beats
        ``.search-icon`` (0,1,0) wherever the two end up in the file.
        """
        self.assertIn("svg.search-icon", _read("static/dashboard.css"))

    def test_the_classes_that_size_svgs_are_still_in_the_sheet(self):
        sheet = _read("static/dashboard.css")
        for pattern in self.SIZED:
            with self.subTest(pattern=pattern):
                self.assertRegex(sheet, pattern)


if __name__ == "__main__":
    unittest.main()
