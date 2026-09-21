"""The web surfaces, held to the design laws rather than to anybody's taste.

There are five addresses in this project — the owner console, the public site,
the per-server dashboard, the hub and the pages Flask renders itself — and four
of them share one stylesheet. So the law has somewhere to live: **two** sheets
(``static/meow.css`` for everything public, ``console/src/styles.css`` for the
operator's) plus the markup that consumes them.

That is exactly why this file exists. A design system is not the sheet; it is the
sheet *plus something that fails when a rule stops being true*. Every check below
is one of five sentences a reviewer would otherwise have to remember:

* **Contrast.** Text is at least 4.5:1 against the surface it is drawn on, and a
  graphic (a border, a focus ring, a fill) at least 3:1. The tokens are what get
  measured, per theme, because that is where a colour is actually chosen.
* **A scale, not one-offs.** Body type is 16–18px — the size a sentence is read
  at — on every surface, and the console's component rules take their sizes from
  ``--type-*`` rather than from a literal somebody typed.
* **One accent.** ``--accent`` draws fills and rings; the token that draws *text*
  has to pass the text contrast, which is what ``--accent-text`` is for. A sheet
  that starts colouring a link with a fill-only token is the failure mode.
* **Nothing is unreachable.** Each sheet has a ``:focus-visible`` rule, and an
  ``outline: none`` is allowed only where the same rule draws something else.
* **Chunked, not endless.** The owner console has nine destinations, which is
  past the seven a person scans without counting — so its rail is grouped, and
  the arithmetic of that grouping is asserted here instead of trusted.

The point of writing these as tests rather than as a paragraph in a README is the
one this project keeps rediscovering: a rule nobody can check turns out to be a
rule nobody followed — the palette in ``utils/style.py`` had nine colours, five of
them unused, while the cogs wrote **251** hex literals of their own.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The two sheets that decide what anything in this project looks like. Every
#: other page (dashboard, hub, the Flask-rendered templates) links the first.
PUBLIC_SHEET = os.path.join(ROOT, "static", "meow.css")
#: The dashboard's own layout sheet, loaded right after the public one — it
#: inherits the public sheet's root size and never declares one of its own.
DASHBOARD_SHEET = os.path.join(ROOT, "static", "dashboard.css")
CONSOLE_SHEET = os.path.join(ROOT, "console", "src", "styles.css")
CONSOLE_SRC = os.path.join(ROOT, "console", "src")
TEMPLATES = os.path.join(ROOT, "templates")

#: The smallest type anybody may be asked to read. Fine print is included, and
#: that is not a small point: the laws name it because the fine print is exactly
#: where the smallest sizes collect — a version stamp, a hint under a field, an
#: eyebrow label — and it is still a sentence somebody has to read.
TYPE_FLOOR = 12.0

#: What a ``rem`` resolves against when a sheet declares no root size of its own.
#: ``meow.css`` sets ``html { font-size: var(--text-body) }``, ``--text-body`` is
#: 16px, and the dashboard inherits it.
ROOT_PX = 16.0

#: WCAG 2.1, the two numbers the law quotes. 4.5:1 is normal text; 3:1 is large
#: text and user-interface components — a border, a ring, a bar on a chart.
TEXT_CONTRAST = 4.5
GRAPHIC_CONTRAST = 3.0

#: What a body of text is set at. Between these two, or the page is either hard
#: to read or shouting.
BODY_MIN, BODY_MAX = 16.0, 18.0


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def blocks(css: str) -> list[tuple[str, str]]:
    """Every ``selector { body }`` in a stylesheet, comments stripped first.

    Deliberately not a CSS parser: this is a *guard*, and the shapes it has to
    understand are the ones this project writes. At-rules nest, so the returned
    selectors are whatever was immediately before the brace — which is what the
    checks below want, because a media query's own block is not a token block.
    """
    without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [
        (" ".join(match.group(1).split()), match.group(2))
        for match in re.finditer(r"([^{}]*)\{([^{}]*)\}", without_comments)
        if match.group(1).strip()
    ]


def token_blocks(css: str) -> list[tuple[str, dict[str, str]]]:
    """Every block that declares ``--colours``, as (selector, {name: value}).

    Only hex values are collected. A token built from ``color-mix`` or an ``rgb()``
    with a live variable in it cannot be resolved statically, and a check that
    guessed at one would be a check that lies.
    """
    out = []
    for selector, body in blocks(css):
        found = {
            match.group(1): match.group(2)
            for match in re.finditer(
                r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{3,8})\b", body
            )
        }
        if found:
            out.append((selector, found))
    return out


def hex_rgb(value: str) -> tuple[float, float, float]:
    raw = value.lstrip("#")
    if len(raw) == 3:
        raw = "".join(character * 2 for character in raw)
    if len(raw) == 4:  # #rgba — the alpha is dropped: contrast is about hue here
        raw = "".join(character * 2 for character in raw[:3])
    red, green, blue = (int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return red, green, blue


def luminance(value: str) -> float:
    """WCAG relative luminance."""

    def channel(component: float) -> float:
        return (
            component / 12.92
            if component <= 0.03928
            else ((component + 0.055) / 1.055) ** 2.4
        )

    red, green, blue = (channel(c) for c in hex_rgb(value))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(one: str, other: str) -> float:
    """The ratio between two colours, brighter over darker."""
    first, second = luminance(one), luminance(other)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def theme_tokens(path: str) -> list[tuple[str, dict[str, str]]]:
    """The named blocks of a sheet, which is where its palette lives."""
    named = []
    for selector, found in token_blocks(read(path)):
        if selector.startswith("[data-theme") or ":root" in selector:
            named.append((selector, found))
    return named


class Contrast(unittest.TestCase):
    """Text ≥ 4.5:1, graphics ≥ 3:1 — measured from the tokens, per theme."""

    #: ``(text token, ground token, why)`` for the public sheet. Every pair is a
    #: combination a page actually draws: a paragraph on a card, small print on a
    #: card (which is why ``--text-muted`` is here and not only the primary one —
    #: it was once ``#64748b``, which is 3.7:1 and under the floor), and the
    #: accent's own text shade on the page.
    PUBLIC_TEXT_PAIRS = (
        ("text-primary", "surface-card", "body text on a card"),
        ("text-secondary", "surface-card", "secondary text on a card"),
        ("text-muted", "surface-card", "the small print on a card"),
        ("text-primary", "bg", "body text on the page"),
        ("text-muted", "bg", "small print on the page"),
        ("accent-text", "bg", "a link or an emphasised value"),
        ("accent-text", "surface-card", "an emphasised value on a card"),
    )

    #: The console, whose four themes are four palettes. ``--accent`` is here as a
    #: *text* pair because this sheet legitimately writes ``code { color:
    #: var(--accent) }`` — inline code is text, so its colour has to pass the text
    #: floor. ``--accent-ink`` on ``--accent`` is a filled button's label.
    CONSOLE_TEXT_PAIRS = (
        ("text", "bg", "body text on the page"),
        ("text", "panel", "text on a card"),
        ("dim", "panel", "small print on a card"),
        ("dim", "bg", "small print on the page"),
        ("accent", "bg", "inline code and the focus ring's own colour"),
        ("accent", "panel", "inline code on a card"),
        ("accent-ink", "accent", "the label on a filled button"),
        ("good", "panel", "a success state's text"),
        ("warn", "panel", "an attention state's text"),
        ("danger", "panel", "a failure state's text"),
    )

    def _check(self, path: str, pairs, floors, *, allow_empty=False) -> int:
        themes = theme_tokens(path)
        if not themes and not allow_empty:
            self.fail(f"{os.path.basename(path)} declares no named palette block")
        checked = 0
        failures = []
        for selector, tokens in themes:
            for text_token, ground_token, why in pairs:
                if text_token not in tokens or ground_token not in tokens:
                    continue
                ratio = contrast(tokens[text_token], tokens[ground_token])
                checked += 1
                if ratio < floors[text_token]:
                    failures.append(
                        f"{selector or ':root'}: --{text_token} on --{ground_token} "
                        f"({why}) is {ratio:.2f}:1, below {floors[text_token]:.1f}:1"
                    )
        self.assertGreater(checked, 0, f"no pair in {path} could be measured")
        if failures:
            self.fail("\n".join(failures))
        return checked

    def test_the_public_sheet_keeps_its_text_readable(self):
        floors = {token: TEXT_CONTRAST for token, _ground, _why in self.PUBLIC_TEXT_PAIRS}
        measured = self._check(PUBLIC_SHEET, self.PUBLIC_TEXT_PAIRS, floors)
        self.assertGreaterEqual(measured, len(self.PUBLIC_TEXT_PAIRS))

    def test_the_console_keeps_its_text_readable_in_every_theme(self):
        floors = {token: TEXT_CONTRAST for token, _ground, _why in self.CONSOLE_TEXT_PAIRS}
        measured = self._check(CONSOLE_SHEET, self.CONSOLE_TEXT_PAIRS, floors)
        # Four themes, ten pairs each: a check that quietly stopped measuring
        # three of the themes would otherwise still pass.
        self.assertGreaterEqual(measured, 4 * len(self.CONSOLE_TEXT_PAIRS))

    def test_the_accent_is_visible_enough_to_be_a_graphic(self):
        """A fill, a border or a focus ring is not text and is not held to text.

        It is still held to *something*: 3:1, the component floor. An accent that
        cannot clear that is an accent nobody can see, which is what makes a
        focus ring decorative rather than useful.
        """
        for path, text_token, ground_token in (
            (PUBLIC_SHEET, "accent", "bg"),
            (CONSOLE_SHEET, "accent", "bg"),
        ):
            ceilings = []
            for selector, tokens in theme_tokens(path):
                if text_token in tokens and ground_token in tokens:
                    ceilings.append(
                        (
                            selector or ":root",
                            contrast(tokens[text_token], tokens[ground_token]),
                        )
                    )
            self.assertTrue(ceilings, f"{os.path.basename(path)} has no accent to measure")
            for selector, ratio in ceilings:
                self.assertGreaterEqual(
                    ratio,
                    GRAPHIC_CONTRAST,
                    f"{selector}: --{text_token} is {ratio:.2f}:1 against "
                    f"--{ground_token}, below the {GRAPHIC_CONTRAST:.1f}:1 floor for a "
                    "control that has to be seen",
                )

    def test_a_fill_only_token_is_never_used_as_text(self):
        """The rule that keeps a link readable when somebody changes a palette.

        ``--accent`` is a fill token on the public sheet: it is used for borders,
        backgrounds and the focus ring, and its contrast on the page (4.01:1) is
        under the text floor *by design* — ``--accent-text`` exists for the other
        job at 8.94:1. So a rule here that paints **text** with a token that
        cannot carry text is the failure this test is for, and the fix is always
        the same: use the token that was made for it.
        """
        text_floor = {
            "accent": contrast(
                dict(theme_tokens(PUBLIC_SHEET)[0][1])["accent"],
                dict(theme_tokens(PUBLIC_SHEET)[0][1])["bg"],
            )
        }
        if text_floor["accent"] >= TEXT_CONTRAST:
            self.skipTest(
                "--accent clears the text floor on this palette, so colouring text with it is fine"
            )

        offenders = []
        for selector, body in blocks(read(PUBLIC_SHEET)):
            for match in re.finditer(r"(?<![-a-z])color:\s*var\(--(accent|violet|pink)\)", body):
                offenders.append(f"{selector} {{ color: var(--{match.group(1)}) }}")
        self.assertEqual(
            offenders,
            [],
            "these rules paint text with a token that is under the text contrast "
            "floor — use --accent-text (or --accent-text-rgb for a tint):\n"
            + "\n".join(offenders),
        )


class TheTypeScale(unittest.TestCase):
    """Body text is 16–18px, and the console takes its sizes from the scale."""

    def _body_font_size(self, path: str) -> float:
        """What a paragraph on this sheet is set at, tokens resolved.

        ``var(--text-body)`` is followed to its declaration rather than treated as
        an unknown: the *point* of the token is that a page reads the same size as
        the token says, so a check that stopped at the reference would be checking
        the wrong thing.
        """
        source = read(path)
        tokens = {
            name: value
            for name, value in re.findall(
                r"--(text-body|type-body):\s*([\d.]+)px", source
            )
        }
        for selector, body in blocks(source):
            if selector not in ("html", "body", "html, body"):
                continue
            found = re.search(r"font-size:\s*(var\(--[a-z-]+\)|[\d.]+px)", body)
            if not found:
                continue
            value = found.group(1)
            if value.startswith("var(--"):
                name = value[len("var(--") : -1]
                self.assertIn(
                    name,
                    tokens,
                    f"{os.path.basename(path)} sets its body size from --{name}, which is "
                    "never given a pixel value in this file",
                )
                return float(tokens[name])
            return float(value.rstrip("px"))
        self.fail(f"{os.path.basename(path)} never sets a root font size")

    def test_the_public_sheet_reads_at_body_size(self):
        size = self._body_font_size(PUBLIC_SHEET)
        self.assertGreaterEqual(
            size,
            BODY_MIN,
            f"the public sheet sets {size}px body text; {BODY_MIN}px is the floor for "
            "a sentence somebody has to read",
        )
        self.assertLessEqual(size, BODY_MAX)

    def test_the_console_reads_at_body_size(self):
        size = self._body_font_size(CONSOLE_SHEET)
        self.assertGreaterEqual(size, BODY_MIN)
        self.assertLessEqual(size, BODY_MAX)

    def test_the_consoles_body_token_is_the_floor(self):
        """``--type-body`` is the size prose is set at, not a preference."""
        tokens = dict(theme_tokens(CONSOLE_SHEET)[0][1])
        sizes = {
            name: value
            for name, value in re.findall(r"--type-([a-z-]+):\s*([\d.]+)px", read(CONSOLE_SHEET))
        }
        self.assertIn("body", sizes)
        body = float(sizes["body"])
        self.assertGreaterEqual(body, BODY_MIN)
        self.assertLessEqual(body, BODY_MAX)

    def test_no_console_rule_invents_a_font_size(self):
        """Every size in a component is a token or relative to its context.

        This is the check that keeps a scale a scale. A literal here is how a
        heading ends up 15px on one page and 16px on the next — which is what this
        sheet looked like: 74 literals, in twelve different sizes, including a
        12.5px that existed on one element.
        """
        offenders = []
        for selector, body in blocks(read(CONSOLE_SHEET)):
            if selector.startswith("[data-theme") or ":root" in selector:
                continue  # the scale itself is declared there
            for match in re.finditer(r"font-size:\s*([^;]+);", body):
                value = match.group(1).strip()
                if value.startswith("var(--type-") or value.endswith("em"):
                    continue
                offenders.append(f"{selector} {{ font-size: {value} }}")
        self.assertEqual(
            offenders,
            [],
            "these rules set a font size by hand instead of from the scale:\n"
            + "\n".join(offenders),
        )


class OneHeadingPerPage(unittest.TestCase):
    """A page names itself once. Two ``h1``s is a page with two subjects."""

    def test_every_template_declares_at_most_one_heading(self):
        for entry in sorted(os.listdir(TEMPLATES)):
            if not entry.endswith(".html"):
                continue
            source = read(os.path.join(TEMPLATES, entry))
            found = len(re.findall(r"<h1[\s>]", source))
            self.assertLessEqual(
                found,
                1,
                f"templates/{entry} declares {found} h1 elements; a page gets one",
            )

    def test_every_console_page_declares_at_most_one_heading(self):
        pages = os.path.join(CONSOLE_SRC, "pages")
        seen = 0
        for entry in sorted(os.listdir(pages)):
            if not entry.endswith(".tsx"):
                continue
            source = read(os.path.join(pages, entry))
            found = len(re.findall(r"<h1[\s>]", source))
            seen += found
            self.assertLessEqual(
                found,
                1,
                f"console/src/pages/{entry} renders {found} h1 elements; a page gets one",
            )
        self.assertGreater(seen, 0, "no console page declares a heading at all")

    def test_the_shell_does_not_compete_with_the_page(self):
        """The frame around a page never draws a heading of its own.

        ``Shell.tsx`` renders the same fact as a crumb in the topbar — the
        location belongs there, and a heading there would be a second ``h1``
        above the page's own.
        """
        source = read(os.path.join(CONSOLE_SRC, "app", "Shell.tsx"))
        self.assertNotIn("<h1", source)
        self.assertIn("topbar__crumb", source)


class ImagesAreLabelled(unittest.TestCase):
    """Every picture says what it is, or says it says nothing."""

    def test_every_image_in_a_template_has_alt_text(self):
        offenders = []
        for entry in sorted(os.listdir(TEMPLATES)):
            if not entry.endswith(".html"):
                continue
            source = read(os.path.join(TEMPLATES, entry))
            for match in re.finditer(r"<img\b[^>]*>", source):
                tag = match.group(0)
                if "alt=" not in tag:
                    offenders.append(f"templates/{entry}: {tag.strip()[:80]}")
        self.assertEqual(
            offenders,
            [],
            "an image with no alt is announced as its file name — add alt=\"\" for "
            "a decorative one:\n" + "\n".join(offenders),
        )

    def test_every_image_in_the_console_has_alt_text(self):
        offenders = []
        for base, _dirs, files in os.walk(CONSOLE_SRC):
            for entry in files:
                if not entry.endswith(".tsx"):
                    continue
                path = os.path.join(base, entry)
                source = read(path)
                for match in re.finditer(r"<img\b[\s\S]*?/?>", source):
                    tag = match.group(0)
                    if "alt=" not in tag:
                        offenders.append(
                            f"{os.path.relpath(path, ROOT)}: {' '.join(tag.split())[:90]}"
                        )
        self.assertEqual(offenders, [], "\n".join(offenders))


class FocusIsVisible(unittest.TestCase):
    """Nothing is reachable only with a mouse."""

    def test_each_sheet_draws_a_focus_ring(self):
        for path in (PUBLIC_SHEET, CONSOLE_SHEET):
            source = read(path)
            self.assertIn(
                ":focus-visible",
                source,
                f"{os.path.basename(path)} has no :focus-visible rule, so a keyboard "
                "user cannot see where they are",
            )

    def test_an_outline_is_only_removed_where_something_replaces_it(self):
        """``outline: none`` is allowed — replaced, never merely deleted.

        A suppressed outline is the most common way a focus ring disappears: the
        element stops being findable by keyboard and nothing in the file says so.
        The rule here is that the *same* block has to draw something else —
        a shadow, a border, a background — so the state is still visible, just not
        as a browser outline.
        """
        for path in (PUBLIC_SHEET, CONSOLE_SHEET):
            offenders = []
            for selector, body in blocks(read(path)):
                if not re.search(r"outline:\s*(none|0)\b", body):
                    continue
                alternative = re.search(
                    r"(box-shadow|border[a-z-]*|background[a-z-]*|text-decoration)\s*:", body
                )
                if alternative:
                    continue
                offenders.append(f"{selector} {{ ...{body.strip()[:60]}... }}")
            self.assertEqual(
                offenders,
                [],
                f"{os.path.basename(path)}: these rules remove the outline and draw "
                "nothing instead:\n" + "\n".join(offenders),
            )


class TargetsAreBigEnough(unittest.TestCase):
    """Fitts's law, as a token: 44×44 for anything a finger is meant to hit."""

    MINIMUM = 44.0

    def test_the_console_states_its_minimum_target(self):
        found = re.search(r"--target-min:\s*([\d.]+)px", read(CONSOLE_SHEET))
        self.assertIsNotNone(found, "the console declares no --target-min")
        self.assertGreaterEqual(float(found.group(1)), self.MINIMUM)

    def test_the_consoles_controls_use_it(self):
        """The token is only a rule where a control actually reads it."""
        # A selector can appear more than once — the responsive blocks restyle
        # these controls — so the *anywhere in the sheet* reading is the right one:
        # the question is whether the control is ever given the floor, and the
        # answer has to be yes in the rule that draws it.
        rules: dict[str, list[str]] = {}
        for selector, body in blocks(read(CONSOLE_SHEET)):
            rules.setdefault(selector, []).append(body)
        for selector in (
            ".btn",
            ".input",
            ".rail__link",
            ".rail__search",
            ".palette__item",
            ".swatch",
            ".topbar__menu",
        ):
            bodies = rules.get(selector, [])
            self.assertTrue(bodies, f"{selector} is not styled at all")
            self.assertTrue(
                any(
                    "min-height" in body and "var(--target-min)" in body
                    for body in bodies
                ),
                f"{selector} never declares min-height: var(--target-min), so it can "
                "be drawn smaller than a finger can hit",
            )

    def test_the_public_sheet_has_the_same_floor(self):
        source = read(PUBLIC_SHEET)
        found = re.search(r"--tap-min:\s*([\d.]+)px", source)
        self.assertIsNotNone(found, "the public sheet declares no --tap-min")
        self.assertGreaterEqual(float(found.group(1)), self.MINIMUM)
        rules: dict[str, list[str]] = {}
        for selector, body in blocks(source):
            rules.setdefault(selector, []).append(body)
        self.assertTrue(
            any("var(--tap-min)" in body for body in rules.get(".btn", [])),
            "the public sheet's buttons do not read --tap-min",
        )


class TheNavIsChunked(unittest.TestCase):
    """Nine destinations is past what a person scans — so they are grouped."""

    def test_every_owner_page_is_placed_exactly_once(self):
        """Every page is in a group — except the home link, which sits above them.

        One page may be ungrouped, and it has to be the *first* declared page: the
        Overview is the place you return to rather than a destination beside Live
        and Servers, and a rail whose home link is inside a heading reads as though
        it belongs to that heading. A second ungrouped page is a page somebody
        forgot to place, and five entries in a list called "Owner" is how a rail
        starts meaning nothing.
        """
        import keep_alive

        declared = {page_id for page_id, *_rest in keep_alive.OWNER_PAGES}
        placed = [
            page_id
            for _label, page_ids in keep_alive.OWNER_GROUPS
            for page_id in page_ids
        ]
        self.assertEqual(len(placed), len(set(placed)), "a page is in two groups")

        home = keep_alive.OWNER_PAGES[0][0]
        missing = declared - set(placed)
        self.assertEqual(
            missing,
            {home},
            "these pages are in no group, so the rail would draw them ungrouped: "
            + ", ".join(sorted(missing)),
        )
        self.assertEqual(
            placed[: len(placed)],
            [page_id for page_id, *_rest in keep_alive.OWNER_PAGES if page_id != home],
            "the groups do not hold the pages in the order they were declared",
        )

    def test_no_group_is_longer_than_a_person_scans(self):
        import keep_alive

        for label, page_ids in keep_alive.OWNER_GROUPS:
            self.assertTrue(label.strip(), "a group with no label is not a group")
            self.assertLessEqual(
                len(page_ids),
                5,
                f"the {label} group has {len(page_ids)} entries; five is where a list "
                "stops being scannable",
            )
        self.assertLessEqual(
            len(keep_alive.OWNER_GROUPS),
            5,
            "chunking nine pages into more than five groups is not chunking",
        )

    def test_the_dashboard_stays_under_the_same_limit(self):
        import keep_alive

        self.assertLessEqual(len(keep_alive.DASHBOARD_PAGES), 7)

    def test_the_owners_rail_carries_the_groups(self):
        """The console reads the labels off the payload, not off a list of its own."""
        import keep_alive

        pages = keep_alive._page_nav(
            keep_alive.OWNER_PAGES, "overview", groups=keep_alive.OWNER_GROUPS
        )
        by_group: dict[str, list[str]] = {}
        for page in pages:
            by_group.setdefault(page["group"], []).append(page["label"])
        self.assertEqual(
            list(by_group),
            [""] + [label for label, _pages in keep_alive.OWNER_GROUPS],
            "the home link is not first, or the groups arrive in a different order "
            "than they were declared",
        )
        self.assertEqual(len(by_group[""]), 1, "more than one page is drawn ungrouped")
        # And the service the console asks for is the one this test just read.
        rendered = read(os.path.join(CONSOLE_SRC, "app", "Shell.tsx"))
        self.assertIn("grouped(boot.pages)", rendered)
        self.assertIn("page.group", rendered)


class OneAccentPerSurface(unittest.TestCase):
    """One hue is spent on what can be acted on; states are not decoration."""

    def test_the_console_keeps_its_palette_to_the_named_tokens(self):  # noqa: D401
        """No component rule invents a colour.

        The palette is ``--accent`` plus three *states* (``--good``, ``--warn``,
        ``--danger``). A hex literal in a component is how a sheet grows a second
        accent, and it is exactly what the Discord cards did: 251 literals across
        the cogs, not one of which consulted the table that was supposed to decide
        the colours. The theme swatches are the one exception, and they are named
        here rather than silently allowed: their whole job is to *preview* a
        palette, so they carry one swatch per theme.
        """
        offenders = []
        for selector, body in blocks(read(CONSOLE_SHEET)):
            if selector.startswith("[data-theme") or ":root" in selector:
                continue  # the palette itself
            if selector.startswith(".swatch--"):
                continue  # a preview of the palette, which is its purpose
            for match in re.finditer(
                r"#[0-9a-fA-F]{3,8}\b|(?<![\w-])rgba?\([^)]*\)", body
            ):
                offenders.append(f"{selector}: {match.group(0)}")
        self.assertEqual(
            offenders,
            [],
            "these rules write a colour by hand instead of using a token:\n"
            + "\n".join(offenders),
        )

    def test_each_theme_declares_one_accent_and_three_states(self):
        for selector, tokens in theme_tokens(CONSOLE_SHEET):
            names = set(tokens)
            self.assertIn("accent", names, f"{selector} declares no accent")
            self.assertTrue(
                {"good", "warn", "danger"} <= names,
                f"{selector} is missing one of the three state colours",
            )
            decorative = [
                name
                for name in names
                if name not in {"bg", "panel", "panel-2", "line", "text", "dim"}
                and name not in {"accent", "accent-ink", "good", "warn", "danger"}
            ]
            self.assertEqual(
                decorative,
                [],
                f"{selector} declares colours nothing can name — two accents is none:\n"
                + "\n".join(decorative),
            )


class MotionIsOptional(unittest.TestCase):
    """A person who asked their system for less motion gets less motion.

    Two halves, because the console animates in two languages: hand-written CSS
    transitions, which the media query in each sheet stops, and JS springs from
    the animation library, which a media query cannot reach at all — those need
    the setting handed to the library, or the page keeps moving for somebody who
    asked it not to.
    """

    def test_each_sheet_honours_the_setting(self):
        for path in (PUBLIC_SHEET, DASHBOARD_SHEET, CONSOLE_SHEET):
            self.assertIn(
                "prefers-reduced-motion",
                read(path),
                f"{os.path.basename(path)} animates with no way to turn it off",
            )

    def test_the_console_hands_the_setting_to_its_animation_library(self):
        app = read(os.path.join(CONSOLE_SRC, "App.tsx"))
        self.assertIn(
            'reducedMotion="user"',
            app,
            "the console's JS animations ignore the system setting unless the "
            "library is told about it",
        )


class NothingIsSetBelowTheFloor(unittest.TestCase):
    """No type anywhere under 12px — in the sheets and in the templates.

    ``font-size: 11.5px`` is not a mid-point between two tiers, it is a number
    somebody typed: the dashboard alone carried **43** declarations between 10px
    and 11.5px, including the hint under the identity fields and the label on
    every metric. The floor is 12px, so those are 12px, and this walks the
    sheets, the templates' inline styles and their ``<style>`` blocks to keep
    them there.

    ``em`` and ``%`` are deliberately not resolved: they are relative to a
    parent this file would have to *guess*, and a guard that guessed would be a
    guard that lies. ``rem`` **is** resolved, against the root size the sheets
    actually set (``--text-body: 16px``), because that one is knowable.
    """

    SIZE = re.compile(r"font-size:\s*([\d.]+)\s*(px|rem)\b")

    def _offenders(self, text: str, label: str) -> list[str]:
        found = []
        for value, unit in self.SIZE.findall(text):
            size = float(value) * (ROOT_PX if unit == "rem" else 1.0)
            if size < TYPE_FLOOR:
                found.append(f"{label}: font-size: {value}{unit} → {size:g}px")
        return found

    def _template_files(self) -> list[str]:
        return [
            os.path.join(folder, name)
            for folder, _dirs, files in os.walk(TEMPLATES)
            for name in files
            if name.endswith(".html")
        ]

    def test_no_sheet_sets_type_under_the_floor(self):
        offenders = []
        for path in (PUBLIC_SHEET, DASHBOARD_SHEET, CONSOLE_SHEET):
            offenders.extend(self._offenders(read(path), os.path.basename(path)))
        self.assertEqual(
            offenders,
            [],
            f"type below {TYPE_FLOOR:g}px: readable for the person who chose it, "
            "not for the person reading it:\n" + "\n".join(offenders),
        )

    def test_no_template_sets_type_under_the_floor(self):
        offenders = []
        for path in sorted(self._template_files()):
            offenders.extend(self._offenders(read(path), os.path.basename(path)))
        self.assertEqual(offenders, [], "\n".join(offenders))


class ProseHasAMeasure(unittest.TestCase):
    """45–75 characters a line, and both sheets say so on purpose.

    The measure is the one law a palette cannot rescue: the same paragraph that
    reads well at 60 characters a line is unreadable across a 27" display, and
    ``ch`` is the unit that survives somebody changing the font. Only the ceiling
    is checked — a measure *tighter* than 45 is a deliberate choice (a caption,
    a figure, a hint under a field), and the half that hurts is the long one.
    """

    MEASURE_MAX = 75

    def _measures(self, path: str) -> list[float]:
        """Every ``max-width: <n>ch`` in a sheet, comments stripped.

        Comments are stripped because this file's own lessons get written down in
        them — ``meow.css`` explains the 18ch caption measure in prose, and a
        guard that counted the explanation as a rule would fail on its own
        documentation.
        """
        source = re.sub(r"/\*.*?\*/", "", read(path), flags=re.S)
        return [float(value) for value in re.findall(r"max-width:\s*([\d.]+)ch\b", source)]

    def test_no_sheet_writes_a_line_longer_than_the_law_allows(self):
        for path in (PUBLIC_SHEET, CONSOLE_SHEET):
            for value in self._measures(path):
                self.assertLessEqual(
                    value,
                    self.MEASURE_MAX,
                    f"{os.path.basename(path)} sets a {value}ch measure; "
                    f"{self.MEASURE_MAX} characters a line is where the eye stops "
                    "finding its way back",
                )

    def test_both_sheets_declare_a_measure_at_all(self):
        """No measure is the failure the number alone cannot catch."""
        for path in (PUBLIC_SHEET, CONSOLE_SHEET):
            self.assertTrue(
                self._measures(path),
                f"{os.path.basename(path)} declares no character measure, so every "
                "paragraph in it runs the width of the window",
            )


class FieldsAreNamed(unittest.TestCase):
    """A field a person can see is a field a screen reader can name.

    Placeholder-only is the classic miss: the text vanishes the moment somebody
    types, and no assistive technology ever had it as a label (WCAG 3.3.2 and
    4.1.2). Named means one of: a ``<label for=id>``, an ``aria-label`` /
    ``aria-labelledby``, a ``<label>`` wrapped around it, or a spread of props
    from a primitive that carries one.
    """

    SKIP_TYPES = {"hidden", "submit", "button", "reset", "image"}
    FIELD = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.I | re.S)
    ATTR = re.compile(r"([\w-]+)\s*=\s*\"([^\"]*)\"", re.S)
    LABEL_FOR = re.compile(r"<label[^>]*\bfor\s*=\s*\"([^\"]*)\"", re.I)

    def _wrapped(self, text: str, position: int) -> bool:
        """Is the field inside a ``<label>`` that carries no ``for``?"""
        opens = len(re.findall(r"<label\b(?![^>]*\bfor=)", text[:position], re.I))
        closes = len(re.findall(r"</label>", text[:position], re.I))
        return opens > closes

    def _unlabelled(self, path: str) -> list[str]:
        text = read(path)
        named_by_for = set(self.LABEL_FOR.findall(text))
        found = []
        for match in self.FIELD.finditer(text):
            tag, raw = match.group(1).lower(), match.group(2)
            attrs = {key.lower(): value for key, value in self.ATTR.findall(raw)}
            if tag == "input" and attrs.get("type", "text").lower() in self.SKIP_TYPES:
                continue
            if any(key in attrs for key in ("aria-label", "aria-labelledby", "title")):
                continue
            if attrs.get("id") and attrs["id"] in named_by_for:
                continue
            if self._wrapped(text, match.start()):
                continue
            line = text[: match.start()].count("\n") + 1
            found.append(f"{os.path.basename(path)}:{line} <{tag} id={attrs.get('id', '—')}>")
        return found

    def _html_templates(self) -> list[str]:
        return [
            os.path.join(folder, name)
            for folder, _dirs, files in os.walk(TEMPLATES)
            for name in files
            if name.endswith(".html")
        ]

    def test_every_template_field_is_named(self):
        offenders = []
        for path in sorted(self._html_templates()):
            offenders.extend(self._unlabelled(path))
        self.assertEqual(
            offenders,
            [],
            "these fields are reachable and nameless — a placeholder is not a "
            "label, and it disappears the moment somebody types:\n" + "\n".join(offenders),
        )

    def test_every_console_field_is_named(self):
        """The same rule for JSX, where a spread of props also counts.

        The console's fields are hand-written ``<input>``s rather than one
        primitive, so each one states its own name; ``{...props}`` is accepted
        because that is a primitive forwarding whatever the caller passed,
        including the label it requires.
        """
        offenders = []
        for folder, _dirs, files in os.walk(CONSOLE_SRC):
            for name in sorted(files):
                if not name.endswith(".tsx"):
                    continue
                path = os.path.join(folder, name)
                text = read(path)
                for match in re.finditer(r"<(input|select|textarea)\b", text):
                    end = text.find("/>", match.start())
                    block = text[match.start() : end if end != -1 else match.start() + 400]
                    if any(
                        marker in block
                        for marker in ("aria-label", "aria-labelledby", "{...props}", "hidden")
                    ):
                        continue
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{name}:{line}")
        self.assertEqual(offenders, [], "unnamed console fields:\n" + "\n".join(offenders))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
