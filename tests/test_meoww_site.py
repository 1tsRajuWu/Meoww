"""The rebuilt public site, held to the laws it was designed against.

The design this replaced was pinned by `tests/test_design_laws.py`, 2,300 lines
of assertions about the `aurora`, `top__`, `foot__` and `mock-switch` vocabulary.
Those laws were correct and they are the reason the old front end could not be
*edited* into this one: every class they named is a class this site no longer
writes. So they are superseded here rather than deleted quietly — the laws below
are the same *intent* (a page that can be read, reached and understood) written
against the system that exists now.

What is checked here is deliberately not taste. Nobody can assert that coral is
a good accent. What can be asserted is everything that actually breaks:

* **Decisions that have a number.** Hick's law is a count of destinations, Fitts's
  is a minimum height, Miller's is a group size, Serial Position is DOM order.
  Each is read off the rendered page or off the sheet, so a page that quietly
  grew a fourth nav item fails here instead of being noticed in a screenshot.
* **One author for one fact.** The accent is `utils/brand.py`'s, the switch is
  `templates/meoww/_switch.html`'s, the left edge is one token, and the two
  readings (coral = do this, green = this is on) never trade places.
* **The states this page is *designed in*.** No script, reduced motion, and a
  screen reader are not fallbacks here; each has a law because each is a way the
  page is read.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import keep_alive
from utils import brand, permissions

ROOT = Path(__file__).resolve().parents[1]
SHEET = ROOT / "static" / "meoww.css"
SCRIPT = ROOT / "static" / "meoww.js"
SPINE = ROOT / "static" / "spine.js"
SWITCH = ROOT / "templates" / "meoww" / "_switch.html"
FRAME = ROOT / "templates" / "meoww" / "base.html"
TEMPLATES = ROOT / "templates"

#: The public pages, and the names `scripts/build_site.py` publishes them under.
#: Imported from the builder rather than typed again: a route that is served but
#: not published (or the reverse) is the defect this list cannot have.
try:  # pragma: no cover - the builder is imported for its own tuple, not its main
    from scripts.build_site import PAGES  # type: ignore
except Exception:  # pragma: no cover
    PAGES = (
        ("/", "index.html"),
        ("/systems", "systems.html"),
        ("/start", "start.html"),
        ("/pricing", "pricing.html"),
        ("/privacy", "privacy.html"),
        ("/terms", "terms.html"),
        ("/ai", "ai.html"),
    )

#: The front door answers a monitor's user agent with one line and a person's
#: with the page, so every render below says which one it is. Without this the
#: whole file would inspect a 34-byte string and pass on the strength of it —
#: which is the trap `tests/test_selling_surface.py` already has written down.
BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

_RENDERED: dict[str, str] = {}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def without_prose(text: str, *, css: bool = False, script: bool = False) -> str:
    """The file with its own documentation removed.

    Every law in this file that reads *code* has to do this first, because the
    prose in this project names the very things the laws forbid — a comment
    explaining that a script registers no `IntersectionObserver` is a file that
    contains the word, and a law that cannot tell a sentence from a statement
    fails on the explanation rather than on the code.
    """
    if script:
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = re.sub(r"(?m)^\s*//.*$", "", text)
    elif css:
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    else:
        text = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return text


def classes_of(body: str) -> set[str]:
    """Every class token a rendered page carries."""
    found: set[str] = set()
    for attribute in re.findall(r'class="([^"]*)"', body):
        found.update(attribute.split())
    return found


def page(route: str) -> str:
    """One public route, rendered by the real app, once per run."""
    if route not in _RENDERED:
        response = keep_alive.app.test_client().get(route, headers={"User-Agent": BROWSER})
        assert response.status_code == 200, f"{route} answered {response.status_code}"
        _RENDERED[route] = response.get_data(as_text=True)
    return _RENDERED[route]


def sheet_tokens(css: str, selector: str) -> dict[str, str]:
    """The `--token: value` declarations inside one rule block."""
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, re.DOTALL)
    assert match is not None, f"{selector} declares no block in static/meoww.css"
    return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", match.group(1)))


def rule_for(css: str, selector: str) -> str:
    """The declaration block that belongs to one selector, comments stripped."""
    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{(.*?)\n\}", css, re.DOTALL)
    assert match is not None, f"{selector} is not declared in static/meoww.css"
    return match.group(1)


def rules_for(css: str, selector: str) -> str:
    """Every declaration block for one selector, joined.

    `rule_for` reads the first, which is what almost every law here means — but a
    selector can legitimately appear twice: the spine's rail and its line share a
    positioning rule (so the pair is one line down the gutter) and the line then
    has a rule of its own about how far it reaches. A law asking *where does this
    element sit* wants the first; one asking *what does it declare* wants them
    all, and reading only the first is how a law above passed for the wrong
    block.
    """
    found = re.findall(r"(?m)^" + re.escape(selector) + r"\s*\{(.*?)\n\}", css, re.DOTALL)
    assert found, f"{selector} is not declared in static/meoww.css"
    return "".join(found)


def block_of(html: str, marker: str) -> str:
    """The one element whose opening tag contains `marker`, by counting `<div>`s.

    Enough for the one law below, and deliberately not a parser: what it has to
    answer is *what is inside this box*, and a box in a rendered page is a
    `<div>` with a closing tag.
    """
    start = html.index(marker)
    opened = html.rindex("<div", 0, start)
    depth = 0
    for tag in re.finditer(r"<(/?)div\b", html[opened:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return html[opened:opened + tag.end()]
    raise AssertionError(f"the element around {marker!r} is not closed")


def rail_of(body: str) -> str:
    """The spine's own markup, from its `<nav>` to its close.

    Split rather than parsed, like `block_of`, and for the opposite reason:
    `block_of` counts `<div>`s because the thing it reads is a `<div>`, and this
    one is a `<nav>` — a *navigation about the page* rather than part of it.
    """
    return body.split('<nav class="mw-spine"', 1)[1].split("</nav>", 1)[0]


def luminance(colour: str) -> float:
    """Relative luminance of `#rrggbb`, as WCAG defines it."""

    def channel(value: float) -> float:
        value /= 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    hexed = colour.strip().lstrip("#")
    assert len(hexed) == 6, f"{colour} is not a six-digit hex colour"
    red, green, blue = (int(hexed[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def contrast(one: str, two: str) -> float:
    first, second = sorted((luminance(one), luminance(two)), reverse=True)
    return (first + 0.05) / (second + 0.05)


class ThePageIsOneDocument(unittest.TestCase):
    """Every public page is drawn by one frame, and it renders at all."""

    def test_every_published_page_renders_and_wears_the_frame(self):
        for route, _name in PAGES:
            with self.subTest(route=route):
                body = page(route)
                self.assertIn('class="mw-shell"', body)
                self.assertIn('href="/static/meoww.css', body)
                # The theme is in the markup before any script runs.
                self.assertIn('data-theme="dark"', body)
                self.assertIn("/static/favicon.svg", body)

    def test_the_frame_loads_the_theme_before_the_first_paint(self):
        """`theme.js` not deferred, and before any stylesheet that paints."""
        head = read(FRAME).split("</head>", 1)[0]
        theme = re.search(r'<script src="[^"]*theme\.js[^"]*"[^>]*>', head)
        self.assertIsNotNone(theme, "the frame does not load theme.js in its head")
        self.assertNotIn("defer", theme.group(0), "theme.js is deferred, so the theme flashes")
        self.assertLess(head.index(theme.group(0)), head.index('href="/static/meoww.css'))

    def test_the_old_shell_is_gone_from_the_pages_it_covered(self):
        """No retired class survives on a rebuilt page.

        Compared as *class tokens* rather than as substrings: the new frame's own
        `mw-foot__list` contains `foot__list`, and a law that cannot tell a
        retired class from its replacement fails on the replacement.

        `btn`, `btn--ghost` and `btn--quiet` are **not** in this set, and the
        reason is a decision rather than an oversight: they belong to the theme
        control, which is the project's own partial — `theme.js` looks its two
        ids up, so its markup is shared with every page rather than copied into
        this one. What the law does require is that the new sheet draws them:
        `TheOneAuthorRules` holds `.mw-shell .btn` to a declared block, because a
        shared control that arrives unstyled is the defect this check found.
        """
        retired = {
            "aurora", "aurora__grid", "aurora__ribbon", "shell", "top", "top__inner",
            "brand", "brand__mark", "brand__name", "brand__dot", "top__menu", "nav",
            "nav__link", "foot", "foot__grid", "foot__title", "foot__list", "foot__base",
            "wrap", "band", "mock-switch", "showcase", "showcase__item", "preview",
            "step", "faq", "closing", "page-head", "tiles", "tile", "btn--primary",
            "drawer", "input", "select", "chip",
        }
        # `card` is in the same position as `btn` and for the same reason: the
        # theme partial's panel is a `card`, it is shared, and this sheet draws it
        # (see `test_the_shared_theme_control_is_drawn_by_this_sheet`).
        for route, _name in PAGES:
            with self.subTest(route=route):
                found = sorted(classes_of(page(route)) & retired)
                self.assertEqual(found, [], f"{route} still wears the old shell: {found}")

    def test_no_page_loads_a_retired_stylesheet(self):
        for route, _name in PAGES:
            with self.subTest(route=route):
                body = page(route)
                for gone in ("landing.css", "pages.css"):
                    self.assertNotIn(gone, body, f"{route} still loads {gone}")


class HickTheNavigationIsThreeDestinations(unittest.TestCase):
    """Hick's law: the time to choose grows with the number of choices."""

    def test_no_page_offers_more_than_three_destinations(self):
        for route, _name in PAGES:
            with self.subTest(route=route):
                links = re.findall(r'class="mw-nav__link"', page(route))
                self.assertLessEqual(len(links), 3, "the nav grew past three destinations")

    def test_the_cap_does_not_orphan_a_page(self):
        """Tesler's law: the complexity the cap removes is the footer's, not the reader's.

        One exception, and it is a deliberate one: `/pricing` is not linked while
        there is nothing for sale, which is a law of its own
        (`tests/test_selling_surface.py`). A page that must not be advertised is
        not an orphan.
        """
        for route, _name in PAGES:
            with self.subTest(route=route):
                body = page(route)
                for published, _file in PAGES:
                    if published == "/pricing" and f'href="{published}"' not in body:
                        continue
                    with self.subTest(target=published):
                        self.assertIn(f'href="{published}"', body, f"{published} is not reachable from {route}")


class TheSpineIsATableOfContents(unittest.TestCase):
    """The rail earns the space it takes, or it is decoration.

    Two laws, and together they are what makes the spine one author for two
    facts. The marks have to point at sections that exist — a table of contents
    whose third line scrolls nowhere is worse than no table of contents — and
    the number a band prints beside itself has to be the number the rail draws
    beside it, because those were two hand-typed copies until this partial.
    """

    def test_every_mark_points_at_a_section_that_exists(self):
        for route, _name in PAGES:
            body = page(route)
            if 'class="mw-spine"' not in body:
                continue
            marks = re.findall(r'href="#([a-z0-9-]+)"', rail_of(body))
            with self.subTest(route=route):
                self.assertTrue(marks, f"{route} draws a rail with no marks on it")
                for mark in marks:
                    self.assertIn(f'id="{mark}"', body, f"{route}'s rail points at #{mark}, which is not on the page")

    def test_the_rail_and_the_band_print_the_same_number(self):
        """One fact, one author: the list, and never the band's own micro line."""
        for route, _name in PAGES:
            body = page(route)
            if 'class="mw-spine"' not in body:
                continue
            rail = rail_of(body)
            drawn = re.findall(r'mw-spine__num" aria-hidden="true">(\d\d)</span>', rail)
            labels = re.findall(r'mw-spine__label">([^<]+)</span>', rail)
            with self.subTest(route=route):
                self.assertEqual(len(drawn), len(labels), f"{route}'s rail lost a label or a number")
                self.assertEqual(drawn, [f"{n:02d}" for n in range(1, len(drawn) + 1)], drawn)
                for number, label in zip(drawn, labels):
                    self.assertIn(
                        f"{number} — {label}",
                        body,
                        f"{route}'s rail says {number} is {label!r} and no band prints that line",
                    )

    def test_the_rail_is_never_drawn_without_its_position_read(self):
        """A rail that cannot say where you are is a decoration with numbers on it.

        Two halves of one defect. The frame loads the script that reads the
        position (`static/spine.js`), and the sheet's line claims nothing without
        it: the fill's height is `var(--mw-at, 0)` — a measurement, or zero — and
        it declares no animation of its own. That second half is the one that was
        wrong before: the line used to be driven by page scroll, so on a page of
        five uneven sections it ended wherever the *page* was rather than at the
        dot it appeared to name.
        """
        css = read(SHEET)
        fill = rules_for(css, ".mw-spine__fill")
        self.assertIn("scaleY(var(--mw-at, 0))", fill, "the line no longer waits to be told where to stop")
        self.assertNotIn("animation", fill, "the line animates itself again instead of being measured")
        self.assertIn("/static/spine.js", read(FRAME), "the frame draws a rail and loads no position read")
        for route, _name in PAGES:
            if 'class="mw-spine"' not in page(route):
                continue
            with self.subTest(route=route):
                self.assertIn(
                    "spine.js",
                    page(route),
                    f"{route} draws a rail and never loads the script that reads it",
                )

    def test_the_script_names_no_section_and_owns_one_property(self):
        """The page's own list stays the only author of its section ids.

        This is the whole reason the position is read by a script instead of by a
        scroll timeline: CSS could only answer *which section am I in* by holding
        one timeline name per section, which is a second copy of a list
        `templates/meoww/_spine.html` already declares — the defect the partial
        exists to prevent. So the law is that the script keeps learning nothing.
        It follows the marks' own `href`s, and the whole of its contract with the
        sheet is one class and one property, because a script with a second hook
        is a script the sheet cannot be read without.
        """
        source = without_prose(read(SPINE), script=True)
        self.assertIn('querySelectorAll(".mw-spine__mark")', source, "the rail's marks are no longer its source of sections")
        self.assertIn("getElementById", source, "the sections are no longer found by the ids the marks point at")
        self.assertIn('setProperty("--mw-at"', source, "the line's length is no longer set from the active waypoint")
        self.assertIn('classList.add("is-here")', source)
        self.assertEqual(source.count("setProperty("), 1, "the rail's script writes a second property")
        self.assertNotIn("createElement", source, "the rail's script builds markup")
        self.assertNotIn("mw-in", source, "the rail's script drives the page's entrance")
        for route, _name in PAGES:
            body = page(route)
            if 'class="mw-spine"' not in body:
                continue
            for section_id in re.findall(r'href="#([a-z0-9-]+)"', rail_of(body)):
                with self.subTest(route=route, section=section_id):
                    # Quoted, so a sentence that merely contains the letters of an
                    # id is not the same thing as the id being written — the same
                    # distinction `tests/test_design_laws.py` draws about state
                    # names, and the one this law needed for `/systems`, whose
                    # third section is called `all` and whose letters are inside
                    # the script's own `querySelectorAll`.
                    self.assertNotIn(
                        f'"{section_id}"',
                        source,
                        f"the rail's script has learned the id {section_id!r}",
                    )

    def test_the_current_waypoint_is_the_one_the_reading_line_is_inside(self):
        """Which dot is lit, held as the *decision* rather than as a screenshot.

        Read off the script's source because the alternative is a browser. Three
        facts, and the first one used to be different: the line is a fraction of
        the window's height (`READ_AT`), the answer is the *last* section whose
        top the line has crossed, and the foot of the page answers with its last
        section — because a last section shorter than the window can never bring
        its top past the line at all.

        What this law is really holding is the defect that got here: the line was
        the middle of the window and the answer was the boundary *nearest* it, and
        with sections of uneven heights the nearest boundary from the middle of a
        tall window is regularly the next one down. Measured on the front door it
        said "01" while the reader was two sections into the page and "05" while
        they were reading the third — a rail that lies about where you are, which
        is worse than no rail. Absolute distance is therefore banned here rather
        than merely unused.
        """
        source = without_prose(read(SPINE), script=True)
        self.assertIn("READ_AT", source, "the reading line is no longer a named fraction of the window")
        self.assertIn("window.innerHeight * READ_AT", source, "the line is no longer drawn from the window's height")
        self.assertNotIn("window.innerHeight / 2", source, "the line is the middle of the window again")
        self.assertNotIn("Math.abs(", source, "the waypoint is chosen by distance again, which is how it drifted")
        self.assertIn(
            "getBoundingClientRect().top <= line",
            source,
            "the waypoint is no longer the last boundary the line has crossed",
        )
        self.assertIn("getBoundingClientRect", source, "the sections' positions are no longer measured")
        self.assertIn("scrollHeight", source, "the foot of the page no longer answers with its last section")

    def test_the_rail_is_drawn_in_the_document_order_of_its_sections(self):
        """A waypoint's number is its place on the page, not its place in a list.

        `spine(sections)` draws the rail and `mark(sections, key)` prints the
        number beside a heading, so the two cannot disagree about a *label* — but
        they could still disagree about *order*, and order is the whole of what a
        table of contents says: declare the list in one order, write the bands in
        another, and every dot points at the right section while claiming the
        wrong position in the page. The numbers would be right and the rail would
        still lie about which way is forward.

        So this reads the rendered page: the id each mark points at has to appear
        in the document in the same order the marks do.
        """
        for route, _name in PAGES:
            body = page(route)
            if 'class="mw-spine"' not in body:
                continue
            marks = re.findall(r'href="#([a-z0-9-]+)"', rail_of(body))
            offsets = []
            for mark in marks:
                self.assertIn(f'id="{mark}"', body, f"{route}'s rail points at #{mark}, which is not on the page")
                offsets.append(body.index(f'id="{mark}"'))
            with self.subTest(route=route):
                self.assertEqual(offsets, sorted(offsets), f"{route}'s rail is not in the page's own order: {marks}")

    def test_the_current_waypoint_is_filled_larger_and_haloed(self):
        """Not merely present: the difference has to survive a glance.

        A row of identical grey rings with one slightly different border is a
        decoration that knows where you are — which is how this rail was read when
        every mark was the same 40px circle and the current one only changed hue.
        So the two states are now *sizes*: a small dim station at rest (24px, a
        16% tint) and a 52px solid-accent disc with a soft glow for the one the
        reader is at, its numeral a size up and written in the fill's own ink
        rather than white.

        The line is one accent at two intensities and two widths — a dim 20% track
        the reader has *filled* with a rounded accent bar twice as wide, glowing
        faintly. Width is the half that matters: a fill the same width as its
        track is a segment of a rod, however bright, and this law holds the
        inequality rather than the numbers so that thickening both together still
        fails.

        What has not changed is the 40px *slot*: the rails's rhythm is what makes
        the marks sit still while the reader moves, and the circle is drawn inside
        it by a pseudo-element so the current one can be half again as large
        without re-laying the four below it.
        """
        css = read(SHEET)
        here = rule_for(css, ".mw-spine__mark.is-here::before")
        resting = rule_for(css, ".mw-spine__mark::before")
        self.assertIn("background: var(--mw-accent)", here, "the current waypoint is no longer filled")
        self.assertIn("inset: -6px", here, "the current waypoint is no longer larger than its neighbours")
        self.assertIn("0 0 26px 6px rgb(var(--mw-accent-rgb) / 40%)", here, "the glow went with the glow")
        self.assertIn("0 0 0 4px rgb(var(--mw-accent-rgb) / 18%)", here, "the edge ring is built from something else")
        self.assertIn("var(--mw-accent-ink)", rule_for(css, ".mw-spine__mark.is-here"), "white ink on a coral fill")
        self.assertIn(
            "font-size: var(--mw-text-small)",
            rule_for(css, ".mw-spine__mark.is-here"),
            "the current numeral is the same size as the ones it is meant to lead",
        )
        self.assertIn(
            "rgb(var(--mw-accent-rgb) / 20%)",
            rules_for(css, ".mw-spine__rail"),
            "the track is no longer a dim tint of the line it becomes",
        )
        self.assertIn("inset: 8px", resting, "the resting dots grew back into buttons")
        self.assertIn(
            "rgb(var(--mw-accent-rgb) / 16%)",
            resting,
            "the resting dots are back to a neutral hairline in the margin",
        )
        # The resting ring has to be *smaller* than the current one, read as the
        # insets they are rather than as two magic numbers side by side.
        rest_inset = int(re.search(r"inset: (-?\d+)px", resting).group(1))
        here_inset = int(re.search(r"inset: (-?\d+)px", here).group(1))
        self.assertGreater(rest_inset, here_inset, "the resting ring is no longer the smaller of the two")
        fill = rules_for(css, ".mw-spine__fill")
        self.assertIn("var(--mw-accent)", fill, "the read line is no longer the accent")
        self.assertIn("border-radius: 999px", fill, "the filled line lost its rounded end")
        dots = rule_for(css, ".mw-spine__mark")
        self.assertIn("width: 40px", dots, "the slot shrank, which is what makes the rail jump as you read")
        # The rail and its line share one positioning rule (the pair is one line
        # down the gutter), so the track's width is read off that block rather
        # than off either selector's own rule.
        shared = re.search(r"\.mw-spine__rail,\s*\.mw-spine__fill\s*\{(.*?)\n\}", css, re.DOTALL)
        self.assertIsNotNone(shared, "the rail and its line no longer share a positioning rule")
        self.assertIn("width: 3px", shared.group(1), "the line is a hairline again, which is what made it a smudge")
        track_width = int(re.search(r"width: (\d+)px", shared.group(1)).group(1))
        # The fill's own declaration is the last one in its match: the pair share a
        # positioning rule, so the track's width arrives first.
        fill_width = int(re.findall(r"width: (\d+)px", fill)[-1])
        self.assertGreater(
            fill_width,
            track_width,
            "the read line is no thicker than the track it is meant to have filled",
        )

    def test_the_rail_is_clear_of_the_column_it_indexes(self):
        """The dots and a label are drawn in the gutter, never on the text.

        A measurement, and it was wrong once: measured at 1440 the 40px slot ran
        from the wrap's own edge out to 44px, and the text's left edge *is* the
        56px gutter — so a label arriving 10px past the slot began **2px inside
        the text column**, which is the one thing the opaque panel behind a label
        exists to prevent. The rail was reported as too close, and it was.

        Read as arithmetic off the sheet's own numbers rather than as a position
        nobody can check: the wrap's widest gutters, the floor the rail's own
        media query names, the offset from the wrap's edge, and the 40px slot
        with a label 10px past it. Two claims follow, and the second is the one
        the defect would have failed.
        """
        css = read(SHEET)
        spine = rule_for(css, ".mw-spine")
        offset = re.search(
            r"left: calc\(50% - \(var\(--mw-wrap-max\) / 2\)\s*([-+]\s*\d+)px\)", spine
        )
        self.assertIsNotNone(
            offset,
            "the rail's left edge is no longer an offset from the wrap's own edge",
        )
        shift = int(re.sub(r"\s+", "", offset.group(1)))
        wrap_max = int(re.search(r"--mw-wrap-max:\s*(\d+)px", css).group(1))
        # `--mw-gutter` is a clamp, and both ends of it matter here: the widest it
        # ever gets is its own maximum, and the tightest it is at the width this
        # rail starts being drawn at is `4vw` there.
        gutter_max = int(re.findall(r"--mw-gutter: clamp\(\d+px, \d+vw, (\d+)px\)", css)[0])
        floor = re.search(r"@media \(min-width: (\d+)px\) \{ \.mw-spine \{ display: block; \} \}", css)
        self.assertIsNotNone(floor, "the rail is no longer drawn from a stated width")
        drawn_from = int(floor.group(1))
        gutter = min(gutter_max, drawn_from * 0.04)

        # The positioning rule assumes the wrap is already at its widest — below
        # that, `50% - (max / 2)` is left of the wrap's own edge and every number
        # above moves with the window instead of being fixed.
        self.assertGreaterEqual(
            drawn_from,
            wrap_max + 2 * gutter,
            "the rail is drawn at a width where the wrap is not yet at its widest",
        )
        # The whole rail — slot and the label that follows it — ends inside the
        # gutter, which is what keeps it off the first column of the page.
        self.assertLess(
            shift + 40 + 10,
            gutter,
            "a label on the rail starts inside the text column rather than in the gutter",
        )

    def test_the_pages_with_sections_carry_a_rail(self):
        """Named rather than counted: the rail is a decision per page.

        The three documents are read straight through — a table of contents for
        a policy is a table of contents with one entry named "the policy" — and
        the dashboard has a rail of its own in `pages.css` rather than this one
        in the gutter. So this is the list, and a page that quietly lost its
        rail is what the law is for.

        `/pricing` is deliberately not on it. It declares one section while this
        deployment sells nothing (`tiers` arrives with a payment link), and the
        partial refuses a rail for fewer than two — see the law below, which is
        the half of this decision that keeps the rail from shipping as a single
        dot that can never be the wrong one and can never be right either.
        """
        for route in ("/", "/systems", "/start"):
            with self.subTest(route=route):
                self.assertIn('class="mw-spine"', page(route), f"{route} declares sections and draws no rail")
        for route in ("/privacy", "/terms", "/ai"):
            with self.subTest(route=route):
                self.assertNotIn('class="mw-spine"', page(route), f"{route} is read straight through")

    def test_one_waypoint_is_not_a_table_of_contents(self):
        """A rail has to be able to be *somewhere*, not merely to exist.

        A single mark is a position indicator that is always at its own position,
        with a line above it and below it that are the same line — decoration
        wearing a number, which is what this class of law exists to refuse. So
        the partial draws nothing below two sections, and this holds both halves:
        the guard is in the partial (where the count is known), and no rendered
        page in the suite draws a rail of one.
        """
        source = read(ROOT / "templates" / "meoww" / "_spine.html")
        self.assertIn(
            "{% if sections | length > 1 %}",
            source,
            "the partial draws a rail for any number of sections, including one",
        )
        for route, _name in PAGES:
            body = page(route)
            if 'class="mw-spine"' not in body:
                continue
            marks = re.findall(r'href="#([a-z0-9-]+)"', rail_of(body))
            with self.subTest(route=route):
                self.assertGreaterEqual(
                    len(marks),
                    2,
                    f"{route} draws a rail of {len(marks)} waypoint(s), which cannot say where the reader is",
                )


class TheInteriorsWearTheFrontDoorsPalette(unittest.TestCase):
    """Walking from the site into the dashboard must not change the brand.

    This is the comparison that was missing, and it is worth saying what it is
    *not*: the interiors are not restyled here, and their `own-*`/`dash-*`
    vocabulary is not touched. They are already the same product by
    construction — `static/pages.css` draws the dashboard *and* the operator
    console and takes every colour it uses out of `meow.css` (`var(--accent…)`
    eleven times, `:root` colours none), so one palette reaches all of them. But
    nothing in the suite said so, and the arrangement only holds while it holds:
    one pasted literal in that sheet and the two rooms behind the front door are
    on a palette of their own, with every test in this repository still green.

    The one place a colour *is* typed by hand is the market chart's fallback
    set, which paints when the sheet has not resolved — and its accent fallback
    was the retired teal until this pass, in a file no colour law had ever read.
    """

    def test_the_interiors_sheet_declares_no_colour_of_its_own(self):
        css = without_prose(read(ROOT / "static" / "pages.css"), css=True)
        literals = sorted(set(re.findall(r"#[0-9a-fA-F]{3,8}\b", css)))
        self.assertEqual(literals, [], f"static/pages.css paints its own colours: {literals}")
        self.assertIn("var(--accent", css, "the interiors stopped reading the shared palette")

    def test_the_one_typed_accent_is_the_brand(self):
        source = without_prose(read(ROOT / "static" / "market.js"), script=True)
        fallback = re.search(r'read\("--accent",\s*"(#[0-9a-fA-F]{6})"\)', source)
        self.assertIsNotNone(fallback, "the market chart stopped naming a fallback accent")
        self.assertEqual(
            fallback.group(1).lower(),
            brand.ACCENT_HEX,
            "the market chart falls back to a colour this product does not wear",
        )

    def test_no_live_file_still_paints_the_accent_this_product_retired(self):
        """`#2dd4bf` is the teal, in the same spirit as the retired *name*.

        A value rather than a derivation, because that is what it is: the accent
        this product wore before the coral. A live stylesheet or script that
        still carries it is a surface a visitor can still see in the old colour,
        and this file's landscape-glob is deliberate — the check is the whole
        visible tree, not the four sheets somebody remembered.
        """
        retired = "#2dd4bf"
        live = sorted(
            path
            for pattern in ("static/*.css", "static/*.js", "static/*.svg", "hub/static/*.css", "hub/static/*.js")
            for path in ROOT.glob(pattern)
        )
        guilty = [p.relative_to(ROOT).as_posix() for p in live if retired in read(p).lower()]
        self.assertEqual(guilty, [], f"these still paint the retired accent: {guilty}")


class SerialPositionTheOrderIsTheMeaning(unittest.TestCase):
    """First and last are the two positions a reader remembers."""

    def test_systems_is_first_and_the_action_is_last(self):
        body = page("/")
        nav = body.split('<nav class="mw-nav"', 1)[1].split("</nav>", 1)[0]
        self.assertRegex(nav, r'href="[^"]*systems[^"]*">Systems<')
        # The action is in the nav's action row, after every destination.
        self.assertLess(nav.index("mw-nav__list"), nav.index("mw-nav__actions"))
        self.assertIn("mw-btn--primary", nav)

    def test_the_dashboard_is_a_destination_in_the_chrome(self):
        """The door out of the marketing site, in the marketing site's nav.

        There was none: the only way into the dashboard was the one-time link
        `/help` mints inside Discord, so a visitor reading this site in a
        browser had to go back to a chat client to find the product they were
        being sold. It is a *destination* rather than the call to action, which
        stays `Add to Discord` and stays last — and it is a link rather than a
        button because it navigates, which is what this sheet's own laws say
        about the two.
        """
        for route in ("/", "/systems", "/start"):
            nav = page(route).split('<nav class="mw-nav"', 1)[1].split("</nav>", 1)[0]
            self.assertRegex(
                nav,
                r'class="mw-nav__link"[^>]*href="[^"]*/dashboard"',
                f"{route} offers no way into the dashboard",
            )


class MillerAGroupIsNotAWall(unittest.TestCase):
    """Seven things, or a heading that says what the eighth is doing there."""

    def test_no_unheaded_group_runs_past_seven_rows(self):
        for route, _name in PAGES:
            body = page(route)
            for match in re.finditer(r'class="mw-list"[^>]*>(.*?)(?=<section|</main>)', body, re.DOTALL):
                block = match.group(1)
                rows = len(re.findall(r"<li\b|<details\b|<div class=\"mw-live__row\"", block))
                if rows <= 7:
                    continue
                before = body[: match.start()]
                heading = re.search(r"<h[23][^>]*>[^<]{4,}</h[23]>.*?$", before, re.DOTALL)
                with self.subTest(route=route, rows=rows):
                    self.assertIsNotNone(
                        heading,
                        "a group of more than seven rows is drawn with no heading above it",
                    )


class VonRestorffOneAccentPerBand(unittest.TestCase):
    """One thing that differs is remembered; six that differ are noise."""

    def test_each_band_offers_exactly_one_accent_action(self):
        for route, _name in PAGES:
            body = page(route)
            for index, band in enumerate(re.finditer(r"<section\b.*?</section>", body, re.DOTALL)):
                with self.subTest(route=route, band=index):
                    self.assertLessEqual(
                        band.group(0).count("mw-btn--primary"),
                        1,
                        "two accent-filled actions in one band",
                    )

    def test_the_on_state_never_fills_an_action(self):
        """The two readings cannot trade places, in the sheet or in the markup."""
        css = read(SHEET)
        for selector in (".mw-btn", ".mw-btn--primary"):
            self.assertNotIn("--mw-on", rule_for(css, selector))

    def test_a_switch_is_never_the_brand_accent(self):
        css = read(SHEET)
        block = rule_for(css, '.mw-switch[aria-checked="true"]::before')
        self.assertIn("--mw-on", block)
        self.assertNotIn("--mw-accent", block)


class FittsEverythingIsBigEnoughToHit(unittest.TestCase):
    """A target is a finger's problem, not a cursor's."""

    def test_the_tap_minimum_is_one_number_and_the_controls_use_it(self):
        css = read(SHEET)
        tokens = sheet_tokens(css, ":root")
        self.assertEqual(tokens.get("--mw-tap"), "44px")
        for selector in (".mw-btn", ".mw-nav__link", ".mw-faq__q", ".mw-switch"):
            with self.subTest(selector=selector):
                self.assertIn("var(--mw-tap)", rule_for(css, selector))

    def test_the_phone_gets_the_action_in_the_thumb_zone(self):
        self.assertIn("mw-sticky", page("/"))
        mobile = read(SHEET).split("@media (max-width: 900px)", 1)[1].split("@media", 1)[0]
        self.assertIn(".mw-sticky", mobile)
        self.assertIn("position: fixed", mobile)


class JakobTheFamiliarIsInTheFamiliarPlace(unittest.TestCase):
    """A visitor should never have to learn where things are."""

    def test_the_theme_control_and_the_action_sit_top_right(self):
        body = page("/")
        header = body.split("<header", 1)[1].split("</header>", 1)[0]
        self.assertIn("data-theme-toggle", header)
        self.assertIn("mw-nav__actions", header)
        self.assertLess(header.index("mw-nav__list"), header.index("mw-nav__actions"))

    def test_the_footer_uses_the_conventional_groups(self):
        body = page("/")
        footer = body.split("<footer", 1)[1].split("</footer>", 1)[0]
        for title in ("The product", "Reading", "Elsewhere"):
            with self.subTest(title=title):
                self.assertIn(title, footer)


class SimilarityALinkIsALink(unittest.TestCase):
    """One colour, one underline, decided once."""

    def test_there_is_one_underline_behaviour(self):
        css = read(SHEET)
        self.assertEqual(css.count("text-decoration: underline"), 1)
        self.assertEqual(css.count("text-underline-offset"), 1)

    def test_a_link_is_the_accent_and_the_underline_is_the_only_difference(self):
        css = read(SHEET)
        self.assertIn("color: var(--mw-accent)", rule_for(css, "a"))
        self.assertIn("text-decoration: none", rule_for(css, "a"))
        # The only rule that adds an underline is the hover, which is what makes
        # "underlined" mean *this is a link* rather than *this is emphasised*.
        self.assertIn("text-decoration: underline", rule_for(css, "a:hover"))


class CommonRegionAGroupHasAnEdge(unittest.TestCase):
    """Three or more things together are a set, and a set is drawn as one."""

    def test_the_group_containers_declare_a_border(self):
        css = read(SHEET)
        for selector in (".mw-card", ".mw-list"):
            with self.subTest(selector=selector):
                self.assertIn("border: 1px solid var(--mw-line)", rule_for(css, selector))

    def test_a_group_of_rows_is_drawn_inside_the_region_that_edges_it(self):
        """A disclosure is a row *of* a region, not a region of its own."""
        body = page("/systems")
        block = body.split('class="mw-list"', 1)[1].split("</div>", 1)[0]
        self.assertIn('class="mw-faq"', block)


class ClosureAndContinuityTheRulesStopWhereTheSetDoes(unittest.TestCase):
    """A rule that runs to the edge says the set continues; it does not."""

    def test_the_last_row_drops_its_divider(self):
        css = read(SHEET)
        for selector in (".mw-list > li:last-child", ".mw-live__row:last-child", ".mw-faq:last-child"):
            with self.subTest(selector=selector):
                self.assertIn("border-bottom: 0", rule_for(css, selector))

    def test_there_is_one_left_edge_on_every_band(self):
        css = read(SHEET)
        self.assertEqual(css.count("--mw-gutter:"), 1, "the gutter is declared more than once")
        self.assertIn("padding-inline: var(--mw-gutter)", rule_for(css, ".mw-wrap"))
        self.assertNotIn("padding-left", css)
        self.assertNotIn("padding-right", css)


class FigureGroundAnOverlayFloats(unittest.TestCase):
    """Anything drawn over the page needs an edge and a shadow."""

    def test_the_panel_over_the_page_carries_the_shadow(self):
        css = read(SHEET)
        self.assertIn("box-shadow: var(--mw-shadow)", rule_for(css, ".mw-shell .theme-pop"))
        self.assertIn("box-shadow: var(--mw-shadow)", rule_for(css, ".mw-shot__window"))
        self.assertGreater(len(sheet_tokens(css, ":root")["--mw-shadow"]), 4)


class PeakEndThePageFinishes(unittest.TestCase):
    """The last thing read is the thing remembered."""

    def test_the_start_page_ends_on_a_real_next_state(self):
        body = page("/start")
        self.assertIn("What happens next", body)
        self.assertIn("mw-closing__title", body)
        self.assertIn("mw-btn--primary", body.split("<section", 3)[-1])

    def test_every_page_ends_with_one_action_except_the_policies(self):
        """A policy that ends by selling you something is not a policy.

        The three documents are the deliberate exception: their last band names
        the other two documents, because a policy you cannot compare against its
        siblings is a policy you have to take on faith.
        """
        for route, _name in PAGES:
            if route in ("/privacy", "/terms", "/ai"):
                continue
            with self.subTest(route=route):
                self.assertIn("mw-closing", page(route))


class AestheticUsabilityItIsLegible(unittest.TestCase):
    """Contrast read from the sheet's own tokens, in every palette it declares."""

    def _palette(self, selector: str) -> dict[str, str]:
        return {name: value.strip() for name, value in sheet_tokens(read(SHEET), selector).items()}

    def test_every_palette_clears_the_text_floor(self):
        for selector in (":root", '[data-theme="light"]', '[data-theme="contrast"]'):
            palette = self._palette(selector)
            with self.subTest(palette=selector):
                for text, ground in (
                    ("--mw-text", "--mw-bg"),
                    ("--mw-text", "--mw-panel"),
                    ("--mw-muted", "--mw-bg"),
                    ("--mw-muted", "--mw-panel"),
                    ("--mw-accent", "--mw-bg"),
                ):
                    with self.subTest(text=text, on=ground):
                        ratio = contrast(palette[text], palette[ground])
                        self.assertGreaterEqual(
                            round(ratio, 2), 4.5,
                            f"{text} on {ground} in {selector} measures {ratio:.2f}:1",
                        )

    def test_the_ink_on_an_accent_fill_clears_the_floor(self):
        for selector in (":root", '[data-theme="light"]', '[data-theme="contrast"]'):
            palette = self._palette(selector)
            with self.subTest(palette=selector):
                ratio = contrast(palette["--mw-accent-ink"], palette["--mw-accent"])
                self.assertGreaterEqual(round(ratio, 2), 4.5, f"{selector} paints {ratio:.2f}:1 on its own accent")


class PostelThePageWorksWithNoScript(unittest.TestCase):
    """Be conservative in what you send: a page is a page without its file."""

    def test_no_page_asks_the_shared_script_for_an_animation(self):
        """The reveal hooks are how a *second* entrance would arrive."""
        for route, _name in PAGES:
            with self.subTest(route=route):
                body = page(route)
                self.assertNotIn("data-reveal", body)
                self.assertNotIn("data-count", body)

    def test_a_control_that_needs_the_script_is_not_shown_without_it(self):
        body = page("/")
        self.assertIn("data-mw-live hidden", body)

    def test_the_switches_state_their_state_in_the_markup(self):
        body = page("/")
        self.assertEqual(body.count('role="switch"'), body.count("aria-checked="))

    def test_the_drawing_takes_no_tab_stop(self):
        """A picture of a control is not a control.

        The hero's window is `aria-hidden`, and `aria-hidden` means "nothing in
        here is a thing" — so a `<button>` inside it that is still focusable is
        the one contradiction the attribute cannot survive: a keyboard user is
        walked through five controls a screen reader has been told do not
        exist. `pointer-events: none` does not fix that, because the tab key
        does not use a pointer. The partial draws them `inert` instead, and the
        live grid below has to stay tabbable or the law would pass by making the
        real control unreachable.
        """
        body = page("/")
        drawing = block_of(body, 'class="mw-shot__rows"')
        self.assertIn('aria-hidden="true"', drawing)
        buttons = re.findall(r"<button[^>]*>", drawing)
        self.assertTrue(buttons, "the window draws no switches; this law is vacuous")
        for button in buttons:
            with self.subTest(button=button[:60]):
                self.assertIn('tabindex="-1"', button)
        live = block_of(body, "data-mw-live")
        self.assertNotIn('tabindex="-1"', live, "the real grid stopped being reachable")

    def test_the_script_looks_up_no_ids_at_all(self):
        """`getElementById` is the hook that fails silently; there are none."""
        source = without_prose(read(SCRIPT), script=True)
        self.assertNotIn("getElementById", source)
        self.assertIn("querySelector", source)


class MotionExactlyOneEntrance(unittest.TestCase):
    """One thing animates on load, all of it is opt-in, and nothing else moves."""

    def test_one_element_carries_the_entrance(self):
        for route, _name in PAGES:
            with self.subTest(route=route):
                self.assertLessEqual(page(route).count("data-mw-enter"), 1)

    def test_all_motion_is_inside_the_opt_in_block(self):
        """Reduced motion needs no second declaration, because it never loads these."""
        css = without_prose(read(SHEET), css=True)
        marker = "@media (prefers-reduced-motion: no-preference)"
        self.assertIn(marker, css, "the sheet has no reduced-motion opt-in block")
        before, after = css.split(marker, 1)
        # Everything after the opt-in block is the print sheet and the trailing
        # rules, which are checked with it: `transition` in a print block is dead
        # code whether or not it animates.
        outside = before + after.split("@media print", 1)[-1]
        for declaration in ("animation:", "animation-name:", "transition:", "transition-property:", "@keyframes"):
            with self.subTest(declaration=declaration):
                self.assertNotIn(declaration, outside, f"{declaration} escaped the opt-in block")

    def test_every_animation_belongs_to_the_one_entrance(self):
        """A second entrance is a rule that animates something outside the window.

        Read as rules rather than as lines, because the selector is what decides:
        every declaration of `animation` in this sheet has to sit inside a block
        whose selector names the one element allowed to arrive.

        **There was an exception here and there is not one now**, which is worth
        recording: the spine's reading line used to be animated straight off the
        reader's scroll position (`animation-timeline: scroll(root block)`), and
        this law admitted it because that motion has no duration to spend and is
        driven by somebody else's hand. It was a good exception and the line was
        still wrong — a page-proportional fill ends wherever the *page's* scroll
        lands, which on a page of five uneven sections is nowhere near the
        waypoint it appears to be naming. The line is now told where to stop (see
        `static/spine.js` and the law beside it), so nothing outside the entrance
        animates at all, and the exception went with the defect that justified it.
        """
        css = read(SHEET)
        offenders = []
        for selector, body in re.findall(r"(?m)^([^\n{}@]+)\{([^}]*)\}", css, re.DOTALL):
            if "animation:" not in body and "animation-name:" not in body:
                continue
            if "data-mw-enter" in selector:
                continue
            offenders.append(selector.strip())
        self.assertEqual(
            offenders,
            [],
            "these selectors animate outside the page's one entrance: " + ", ".join(offenders),
        )

    def test_nothing_registers_an_observer_or_a_scroll_listener(self):
        """A scroll reveal is how a second entrance gets added."""
        source = without_prose(read(SCRIPT), script=True)
        for forbidden in ("IntersectionObserver", "scroll", "requestAnimationFrame", "load"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class TheFrontDoorShowsOneThingAtATime(unittest.TestCase):
    """A band that shows everything at once shows nothing — and what it *does*
    show has to be true.

    Two bands are held here for one reason: the tour of the dashboard, cut twice
    until one page is described in full and the other ten are single rows to
    scan, and the command demo that replaced a row of switches with nothing
    attached. Both make claims about the product on a page a visitor has no way
    to check for themselves, so what is asserted of each is completeness and
    honesty rather than taste: that nothing the registry declares falls off the
    page, and that the demo's every sentence comes from the bot's own tables.
    """

    def test_the_tour_is_one_card_and_a_list_of_links(self):
        band = page("/").split('id="dashboard"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(
            len(re.findall(r'class="mw-card"', band)),
            1,
            "the tour grew back into a grid of panels",
        )
        self.assertIn('class="mw-list mw-pages"', band, "the other pages are named nowhere")
        rows = re.findall(r'class="mw-pages__row"', band)
        self.assertEqual(
            len(rows),
            len(keep_alive.DASHBOARD_PAGES) - 1,
            "one page is not described in full and one row is not the rest of them",
        )
        # The scan list is a list of links and notes, not a second card grid: a
        # bullet under a row is the bullets this cut exists to remove.
        self.assertNotIn("mw-prose", band.split('class="mw-list mw-pages"', 1)[1], "the list grew bullets")

    def test_every_page_carries_a_note_and_a_note_is_a_label(self):
        """Four to six words, and not a sentence — the fallback is the tell.

        A row is ten seconds of scanning, so what it prints is a label: a note of
        nine words with a full stop on it is a paragraph squeezed into a row, and
        the reader who was promised a fast band gets the slow one back. The
        fallback a page with no entry falls back *to* ends in a full stop on
        purpose, so this fails on the day a page is added without a note rather
        than passing on a sentence that happens to be short.
        """
        for row in keep_alive._landing_tour():
            with self.subTest(page=row["id"]):
                note = row["note"]
                self.assertFalse(note.endswith("."), f"{row['id']}'s note is a sentence, not a label")
                words = len(note.split())
                self.assertGreaterEqual(words, 4, f"{row['id']}'s note says too little to be a label")
                self.assertLessEqual(words, 6, f"{row['id']}'s note is a paragraph, not a label")

    def test_every_page_the_registry_declares_is_named_on_the_front_door(self):
        """Read from the rail the tour is built from, not from the page."""
        band = page("/").split('id="dashboard"', 1)[1].split("</section>", 1)[0]
        self.assertGreater(len(keep_alive.DASHBOARD_PAGES), 4, "this law assumes there are more pages than cards")
        for page_id, label, path, _anchor in keep_alive.DASHBOARD_PAGES:
            with self.subTest(page=page_id):
                self.assertTrue(
                    label in band or path in band,
                    f"the dashboard's {label!r} page is on neither a card nor the line",
                )

    def test_the_command_demo_asks_nothing_of_any_server(self):
        """The two tables are in the page, and the script has no way out.

        This is the claim the band makes in its own closing line — "the answers are
        worked out in your browser" — and it is the one that would be quietest if
        it stopped being true: a demo that started asking the deployment would look
        exactly the same to a reader, and would be a request from every visitor to
        a marketing page. So the JSON is asserted to be *in* the markup, and the
        script is asserted to reach for none of the four ways out of a browser.
        """
        body = page("/")
        self.assertIn('type="application/json" data-mw-sim', body, "the demo's tables are not in the page")
        block = body.split("data-mw-sim>", 1)[1].split("</script>", 1)[0]
        for key in ('"gates"', '"always"', '"off"', '"names"'):
            with self.subTest(key=key):
                self.assertIn(key, block, "the demo's payload lost a table it needs")
        source = without_prose(read(SCRIPT), script=True)
        for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "navigator"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source, "the demo reaches out of the page")

    def test_the_demo_knows_every_real_command_and_only_real_commands(self):
        """So the box cannot be used to fish for commands that are not there.

        Two registry tables, and each is already held to the truth elsewhere: the
        audience table is the one whose coverage of the real command tree is
        enforced, and the gate map is the one ``bot`` refuses commands with. Their
        union has to be exactly the product's commands — a name the bot has and
        the demo answers "not a command" is the failure that matters, because it
        teaches a visitor the wrong thing about the product.
        """
        payload = keep_alive._sim_payload()
        known = set(payload["gates"]) | set(payload["always"])
        real = {str(name).split(" ")[0] for name in permissions.COMMAND_ACCESS}
        self.assertGreater(len(real), 50, "the command list was not found; this law is vacuous")
        self.assertEqual(
            known,
            real,
            "the demo recognises a command the bot does not have, or misses one it does",
        )

    def test_every_command_the_demo_suggests_is_one_it_recognises(self):
        """The box must not suggest a command by a name it would itself refuse.

        The placeholder is the page's own instruction, and it read ``level rank``
        for as long as the leveling system's command has been ``rank``: a visitor
        typed exactly what the box suggested, and the box answered "is not a
        command this bot has". A suggestion is a claim about the product, so
        every name the demo offers — the placeholder, the label a screen reader
        reads, the three ``<code>``s in the hint, and the empty-state line the
        script builds — is held to the same two tables the demo answers from.
        """
        payload = keep_alive._sim_payload()
        known = set(payload["gates"]) | set(payload["always"])
        body = page("/")
        sim = body.split('class="mw-sim"', 1)[1].split("data-mw-sim>", 1)[0]
        offered = re.findall(r'placeholder="([^"]+)"', sim)
        offered += re.findall(r'aria-label="A command to try, for example ([^"]+)"', sim)
        offered += re.findall(r"<code>/?([a-z0-9][a-z0-9 -]*)</code>", sim)
        offered += re.findall(r'element\("code", "([^"]+)"\)', read(SCRIPT))
        self.assertGreaterEqual(len(offered), 4, "the demo stopped suggesting anything")
        for name in offered:
            with self.subTest(name=name):
                self.assertIn(
                    name.strip().lstrip("/").split()[0],
                    known,
                    "the demo suggests a command it would answer 'not a command' to",
                )

    def test_every_system_the_demo_names_is_one_the_registry_has(self):
        payload = keep_alive._sim_payload()
        for command, ids in payload["gates"].items():
            for feature_id in ids:
                with self.subTest(command=command, system=feature_id):
                    self.assertIn(
                        feature_id,
                        payload["names"],
                        "a gate names a system the registry does not have, so a refusal would name it wrong",
                    )

    def test_the_demo_refuses_in_the_bots_own_words(self):
        """The sentence is `bot.py`'s, compared against `bot.py`.

        A paraphrased refusal is worse than no demo: it teaches a member what the
        bot does *not* say. The bot cannot be imported here (importing it starts a
        gateway), so the sentence lives in `keep_alive` and this reads the source
        it was copied from, with the markdown emphasis stripped — the demo shows
        the name in bold rather than printing asterisks at a reader.
        """
        sentence = keep_alive.SIM_OFF_SENTENCE
        self.assertIn("{name}", sentence, "the refusal no longer takes the system's name")
        source = re.sub(r"\*\*", "", read(ROOT / "bot.py"))
        for fragment in sentence.split("{name}"):
            self.assertIn(fragment, source, f"the demo invents a refusal: {fragment!r}")
        self.assertIn(
            keep_alive.SIM_OFF_HINT.split(" the Web Dashboard")[0],
            source,
            "the demo's second line is not the bot's second line",
        )

    def test_the_demo_fits_a_phone(self):
        """The one-column rule keeps the pair, and dropping it was a whole defect.

        Measured at 320x740 on the front door: the command box holds an
        `<input>`, whose min-content width is its own default twenty characters —
        about 200px, whatever `min-width: 0` and `flex: 1` say about it. A bare
        `1fr` track keeps its min-content **floor**, so that floor widened the
        single column to 321px inside a 280px one, the document measured 341px
        in a 320px window, and `body { overflow-x: hidden }` meant the 21px
        hanging off the edge could not be scrolled to: the field's Send button
        and the answer region's right rule were simply cut off, with nothing on
        the page saying so. `minmax(0, 1fr)` is a floor of zero, which is what
        lets the box shrink to its column instead of the column growing to the
        box.

        Asserted on the declaration rather than in a browser, because it *is* one:
        the two-pane rules above already use the pair, and what is checked here is
        that the phone's own rule does too — and that no track in that block went
        back to a bare `1fr`.
        """
        css = without_prose(read(SHEET), css=True)
        phone = css.split("@media (max-width: 900px)")[1]
        self.assertRegex(
            phone,
            r"\.mw-sim,\s*\n\s*\.mw-split \{ grid-template-columns: minmax\(0, 1fr\); \}",
            "the two-pane bands stack into a track with a min-content floor again, "
            "which is a page wider than the phone it is read on",
        )
        bare = re.findall(r"grid-template-columns:\s*1fr\b", phone)
        self.assertEqual(
            bare,
            [],
            "a column in the phone block kept its min-content floor: " + ", ".join(bare),
        )

    def test_the_questions_band_states_its_own_heading(self):
        """The band that ends the page gets room the other bands' intros do not.

        At the shared spacing the questions' intro read as the caption of the
        first row — a band-gap above a list whose first line is another sentence
        at the same weight — which is a heading that looks like a row. The room is
        the whole of the difference: no fourth type size is invented for it.

        It was one step of room and it is now two, because one step was not
        enough to make the difference visible: the accordion rows are the only
        place on the page where every line is identically shaped, so the intro
        above them has to be separated by more than a band's own rhythm, or the
        reader arrives at "Before you add it" already among the questions. Both
        numbers are written as a sum of the scale's own steps rather than as a
        literal, so they stay in the system they are drawn from.
        """
        body = page("/")
        self.assertIn('class="mw-band mw-band--statement" id="questions"', body)
        css = read(SHEET)
        band = rule_for(css, ".mw-band--statement")
        self.assertIn("var(--mw-space-8)", band)
        self.assertIn("var(--mw-space-6)", band, "the questions lost the extra room above their intro")
        head = rule_for(css, ".mw-band--statement .mw-head")
        self.assertIn("var(--mw-space-7)", head)
        self.assertIn("var(--mw-space-3)", head, "the intro sits on its first row again")


class TheOneAuthorRules(unittest.TestCase):
    """The facts that were two facts in this repository before."""

    def test_the_sheet_paints_the_brands_own_colours(self):
        tokens = sheet_tokens(read(SHEET), ":root")
        expected = {
            "--mw-bg": brand.BG_HEX,
            "--mw-panel": brand.PALETTE["surface-card"],
            "--mw-panel-2": brand.PALETTE["surface-hover"],
            "--mw-text": brand.PALETTE["text-primary"],
            "--mw-muted": brand.PALETTE["text-muted"],
            "--mw-accent": brand.ACCENT_HEX,
            "--mw-on": brand.ON_HEX,
            "--mw-on-rgb": brand.ON_RGB,
        }
        wrong = {name: (tokens.get(name), value) for name, value in expected.items() if tokens.get(name) != value}
        self.assertEqual(wrong, {}, "utils/brand.py and static/meoww.css disagree; the module is the author")

    def test_there_is_exactly_one_switch_in_the_tree(self):
        """One control, one file: the dashboard's form and the front door's grid."""
        owners = [
            path.relative_to(ROOT).as_posix()
            for path in sorted(TEMPLATES.rglob("*.html"))
            if 'role="switch"' in without_prose(read(path))
        ]
        self.assertEqual(owners, ["templates/meoww/_switch.html"], f"two implementations of one control: {owners}")

    def test_the_shared_theme_control_is_drawn_by_this_sheet(self):
        """A partial the pages load and do not style renders as plain text."""
        css = read(SHEET)
        self.assertIn("min-height: var(--mw-tap)", rule_for(css, ".mw-shell .btn"))
        self.assertIn("border", rule_for(css, ".mw-shell .theme-pop .card"))
        body = page("/")
        self.assertIn('class="btn btn--ghost"', body)

    def test_the_frames_own_decoy_is_kept_off_the_page(self):
        """`_trap.html` says `class="trap"` *because* a sheet is what hides it.

        The frame includes the trap and does not load the sheet that used to
        carry this rule, so it has to be declared here — without it the two
        paths the crawler directive names render as two coral links in the
        footer, which is what the first build of this frame did. Off-screen
        rather than `display: none`, for the reason the partial writes down: a
        node a collector may skip is not a decoy.
        """
        self.assertIn('{% include "_trap.html" %}', read(FRAME))
        self.assertIn("Entity index", page("/"), "the decoy left the markup; this law would pass vacuously")
        block = rule_for(read(SHEET), ".trap")
        self.assertIn("left: -9999px", block)
        self.assertNotIn("display: none", block)

    def test_both_callers_include_the_one_switch(self):
        for name in ("templates/dashboard.html", "templates/home.html"):
            with self.subTest(template=name):
                self.assertIn('from "meoww/_switch.html" import switch', read(ROOT / name))

    def test_the_dashboard_kept_its_own_write_and_its_form(self):
        """The partial owns the markup; the page owns the endpoint and the guard.

        The endpoint is asserted where it is actually decided — the view passes
        `toggle_url`, and the template cannot spell it — so this reads both files
        rather than looking for a literal the template never had.
        """
        source = read(ROOT / "templates" / "dashboard.html")
        # The wrapper's own call, in pieces rather than as one literal line: it
        # passes the two extra states a server's administrator can meet here and
        # nowhere else (a fleet lock, and the sentence for it), and an assertion
        # spelled as the whole argument list is a test that fails every time the
        # page learns one more thing about the switch it is drawing.
        self.assertIn("mode=\"write\", action=toggle_url", source)
        self.assertIn("hidden=guard(", source)
        self.assertIn('toggle_url="/api/toggle"', read(ROOT / "keep_alive.py"))
        self.assertNotIn('class="dash-switch"', source, "the dashboard draws its own switch again")


if __name__ == "__main__":
    unittest.main()
