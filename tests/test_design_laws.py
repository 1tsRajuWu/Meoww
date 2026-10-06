"""The web surfaces, held to the design laws rather than to anybody's taste.

There are five addresses in this project — the owner console, the public site,
the per-server dashboard, the hub and the pages Flask renders itself — and four
of them share one stylesheet. So the law has somewhere to live: **two** sheets
(``static/meow.css`` for everything public, ``console/src/styles.css`` for the
operator's) plus the markup that consumes them.

That is exactly why this file exists. A design system is not the sheet; it is the
sheet *plus something that fails when a rule stops being true*. Every check below
is one of nine sentences a reviewer would otherwise have to remember:

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
* **Nothing reads a token that does not exist.** A ``var()`` with no definition
  and no fallback is valid CSS that draws *nothing*: the declaration is dropped,
  so a colour paints what it inherited, a font size changes nothing, and an SVG
  ``stroke`` is not painted at all. Five such names lived in the console and no
  test could see them — and the same law has a second half, for the sheets that
  are deliberately *not* self-contained: a page's token contract is the whole
  document, in load order, not one file.
* **A theme overrides; it never owns.** The palette lives on the base block and
  each theme redefines the values that make it a theme — never the other way
  round, where a name exists only while one theme is selected and every other
  theme silently drops it.
* **Markup and the sheet agree.** A class name the app writes is a treatment it
  expects, so either the sheet rules it or the name is not written — and a layout
  is the sheet's business rather than a `style={{ … }}` the sheet cannot see.
* **A label column keeps its room.** A row of two columns — a name and the
  sentence about it — that states no width for either is sized by its content,
  so the longer column takes the row and the shorter one collapses to its
  longest word. Fifty-five pixels of a 285px row is not a narrow column but a
  column nobody chose, and it split the station tag across two lines at the
  hyphen in "ad-free". The same defect has a *stated* form, which is worse
  because it looks deliberate: `minmax(0, 1fr)` is a floor of zero, so a column
  given it does not shrink to its longest word, it **disappears** — the
  dashboard's front page measured a name column of **0px** with its own heading
  painting one word per line across the description beside it, and the site's
  footer collapsed its brand column to 14px for the same reason. A zero-minimum
  column is a column nobody chose too.
* **The pointer is moved by one thing.** The drawn cursor is positioned every
  frame by script and shaped by the sheet, and the two only compose if they use
  different properties: an animation of `transform` does not add to an inline
  `transform`, it replaces it. Both scripts wrote the position as
  `style.transform = translate3d(…)` while the sheet animates `transform` for the
  twinkle, the bob and the slash, so the default swarm sat spinning in the
  top-left corner of the viewport — on sheets whose own rule is `cursor: none`,
  which left the visitor with no cursor on the page at all.

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
#: The interiors' own layout sheet, loaded right after the public one — it
#: inherits the public sheet's root size and never declares one of its own.
#:
#: This was `static/dashboard.css`, and the rebuild renamed and merged it: the
#: dashboard's interior, the owner console's and the radio page's rows are all
#: `static/pages.css` now, which is the same arrangement by the same split — it
#: declares **no** custom property of its own and reads thirty-two from the
#: palette the sheets before it load. Every law below asks about that contract
#: rather than about a filename, so the rename is a constant moving rather than a
#: law being rewritten.
DASHBOARD_SHEET = os.path.join(ROOT, "static", "pages.css")
#: The hub's sheet, which is *self-contained* like the console's rather than
#: sharing the public palette the way the dashboard's does: the built pages are
#: generated by ``scripts/build_hub.py`` and carry their own palette.
HUB_SHEET = os.path.join(ROOT, "hub", "static", "hub.css")
CONSOLE_SHEET = os.path.join(ROOT, "console", "src", "styles.css")
CONSOLE_SRC = os.path.join(ROOT, "console", "src")
TEMPLATES = os.path.join(ROOT, "templates")

#: The sheets that carry a themed palette — a base block, and themes that
#: override parts of it. The interiors' sheet is not one of them: `pages.css`
#: declares no custom property at all — it reads the palette the sheets before it
#: load — so it has no theme blocks to override.
THEMED_SHEETS = (PUBLIC_SHEET, HUB_SHEET, CONSOLE_SHEET)

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


def _without_comments(source: str) -> str:
    """A TypeScript source with ``//`` and ``/* */`` comments taken out.

    Quote-aware rather than pattern-based, because the two things both look like
    comments and are not: the ``//`` in ``https://…`` and the ``/*`` in a string.
    A template literal counts as a quote here, so a ``${…}`` inside one is left
    exactly as it was written.

    An apostrophe is not a quote: this is JSX, so prose about "the command's own
    name" and "Discord's copy" sits in the markup, and a scanner that read those
    as the start of a string would stop seeing comments at the first one. The
    codebase writes its strings with double quotes (and the one thing this could
    have hidden — a URL in single quotes — appears nowhere in it).
    """
    out, quote, index = [], "", 0
    while index < len(source):
        char = source[index]
        if quote:
            if char == "\\":
                out.append(source[index:index + 2])
                index += 2
                continue
            if char == quote:
                quote = ""
            out.append(char)
            index += 1
        elif char in "\"`":
            quote = char
            out.append(char)
            index += 1
        elif source.startswith("//", index):
            end = source.find("\n", index)
            index = len(source) if end == -1 else end
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            index = len(source) if end == -1 else end + 2
            out.append(" ")
        else:
            out.append(char)
            index += 1
    return "".join(out)


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


class EveryTokenIsDefined(unittest.TestCase):
    """A ``var()`` nobody defined renders *nothing* — and passes every other check.

    This is the quietest failure in the whole sheet, which is why it is a law.
    ``color: var(--muted)`` is valid CSS that paints the inherited colour;
    ``font-size: var(--type-small)`` is valid CSS that changes nothing at all;
    an undefined name in an SVG ``stroke`` resolves to ``none``, so the element
    is not painted. Nothing errors, nothing is logged, and the diff that added it
    looks exactly like the diff that works.

    Found by reading the served pages rather than the source: five names —
    ``--muted``, ``--radius-2``, ``--surface-2``, ``--type-small`` (Discord's
    palette, carried in with the markup this console replaced) and
    ``--pill-good-text`` — were read by rules the browser had been discarding for
    as long as they had existed, so the Access page's session rows had no
    background and body-sized metadata, and the Overview's Fleet Reach gauge was
    an empty ring with "40%" written in the middle of it.

    A name is satisfied two ways: declared in the sources the sheet is built from
    (its own token blocks, or a ``setProperty`` call from ``theme.ts``, which is
    where the cursor's four live), or given a fallback — ``var(--x, …)`` — which
    is exactly what the runtime-set ones do.

    Two sheets are swept here rather than one, because the app is not the only
    self-contained one: ``hub/static/hub.css`` is generated into pages by
    ``scripts/build_hub.py`` and carries its own palette, so it is answerable to
    the same sentence. The dashboard's sheet is deliberately *not* here — it
    shares the public palette, and ``EverySheetIsSatisfiedByItsPage`` is where a
    contract like that gets checked.
    """

    def _sources(self) -> list[tuple[str, str]]:
        """Each self-contained source as ``(label, text)``, named from the root."""
        sources = []
        for root, _dirs, files in os.walk(CONSOLE_SRC):
            for entry in sorted(files):
                if entry.endswith((".css", ".ts", ".tsx")):
                    path = os.path.join(root, entry)
                    sources.append((os.path.relpath(path, ROOT), read(path)))
        sources.append(("hub/static/hub.css", read(HUB_SHEET)))
        return sources

    def _declared(self) -> set[str]:
        declared: set[str] = set()
        for _label, source in self._sources():
            declared.update(re.findall(r"--([a-z0-9-]+)\s*:", source))
            declared.update(re.findall(r"setProperty\(\s*[\"']--([a-z0-9-]+)", source))
        return declared

    def _reads(self) -> list[tuple[str, str]]:
        """Every ``var(--name)`` with no fallback: (name, where) for each."""
        naked = []
        for label, source in self._sources():
            for match in re.finditer(r"var\(\s*--([a-z0-9-]+)\s*([,)])", source):
                if match.group(2) == ")":
                    naked.append((match.group(1), f"{label}: {match.group(0)}"))
        return naked

    def test_the_table_was_found(self):
        """A check that reads nothing proves nothing."""
        declared = self._declared()
        self.assertGreater(len(declared), 30, "no custom properties were parsed")
        self.assertGreater(len(self._reads()), 40, "no var() reads were parsed")

    def test_every_property_a_rule_reads_exists(self):
        declared = self._declared()
        missing = sorted(
            f"--{name} (read by {where})"
            for name, where in self._reads()
            if name not in declared
        )
        self.assertEqual(
            missing,
            [],
            "these rules read a custom property nothing defines, so the browser "
            "drops the declaration and draws nothing — declare it, or give the "
            "read a fallback:\n" + "\n".join(missing),
        )

    def test_the_five_names_this_law_was_written_for_are_gone(self):
        """Named rather than left to the sweep above, because these five were
        *invisible*: a reviewer reading either diff would have seen a colour and a
        radius where the browser saw neither."""
        sources = "".join(source for _label, source in self._sources())
        for name in ("muted", "radius-2", "surface-2", "type-small", "pill-good-text"):
            with self.subTest(name=name):
                self.assertNotIn(f"var(--{name})", sources)
                self.assertNotIn(f"var(--{name},", sources)


class EverySheetIsSatisfiedByItsPage(unittest.TestCase):
    """A sheet that shares its palette is a contract, not a file.

    ``static/pages.css`` — the interior sheet the rebuild grew out of
    ``static/dashboard.css`` — declares **no** custom property and *reads*
    thirty-two. The palette it is written against lives in the sheets the page
    loads before it (``static/meoww.css`` from the frame, then ``static/meow.css``
    for the palette), which is the split the files state in their own headers — **the palette is shared, the layout is the page's** —
    and the consequence is that neither file can be checked on its own. Nothing
    was checking them together: thirty-two names, each one held up by nothing
    more than the fact that nobody had renamed it yet, which is the same silence
    the console's five undefined tokens lived in until they were found by looking
    at a page.

    So the law is about the **page**. Walk a template in document order and every
    ``var()`` a sheet reads with no fallback has to be declared by something
    already loaded when that sheet arrives: an earlier sheet, an earlier inline
    block, or a script that sets it at runtime. It is ``EveryTokenIsDefined`` one
    level up, for the sheets that are not self-contained — and unlike that one it
    cannot be satisfied by a single file, which is exactly why it went unwritten.
    """

    def _events(self, html: str) -> list[tuple[int, str, str]]:
        """What a page loads or declares, in the order the browser gets it."""
        found = []
        for match in re.finditer(r'<link[^>]+href="(/static/[^"?]+\.css)', html):
            found.append((match.start(), "sheet", match.group(1)))
        for match in re.finditer(r"<style>(.*?)</style>", html, flags=re.S):
            found.append((match.start(), "inline-style", match.group(1)))
        # An inline script can set a property before anything is painted — it is
        # how the sign-in page's mesh follows the pointer — so it is part of the
        # page's declarations too, and it is read as *setters* rather than as
        # text: a `--name:` inside JavaScript is a string, not a declaration.
        for match in re.finditer(
            r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, flags=re.S
        ):
            found.append((match.start(), "inline-script", match.group(1)))
        for match in re.finditer(r'<script[^>]+src="(/static/[^"?]+\.js)', html):
            found.append((match.start(), "script", match.group(1)))
        found.sort()
        return found

    def _declared(self, source: str) -> set[str]:
        return set(re.findall(r"--([a-z0-9-]+)\s*:", source))

    def _set_at_runtime(self, source: str) -> set[str]:
        return set(re.findall(r'setProperty\(\s*["\']--([a-z0-9-]+)', source))

    def _read_without_a_fallback(self, source: str) -> set[str]:
        """``var(--name)`` only: ``var(--name, …)`` carries its own definition."""
        return {
            match.group(1)
            for match in re.finditer(r"var\(\s*--([a-z0-9-]+)\s*([,)])", source)
            if match.group(2) == ")"
        }

    def _runtime_names(self, path: str) -> set[str]:
        try:
            return self._set_at_runtime(read(os.path.join(ROOT, path.lstrip("/"))))
        except OSError:
            return set()

    def _lineage(self, template: str) -> list[str]:
        """A template and the bases it extends, base first.

        A page that extends a base loads the base's sheets **before** its own: the
        block a child fills sits inside the base's document, and the sheets the
        base links are in the head above that block. The walk reads template text
        rather than rendered HTML, so it has to follow ``extends`` itself — which
        is why `home.html`, whose palette arrives from `site_base.html`, was read
        as a page that declared nothing until this existed.
        """
        chain = [template]
        seen = {os.path.abspath(template)}
        while True:
            match = re.search(r'{%\s*extends\s+"([^"]+)"', read(chain[-1]))
            if not match:
                break
            parent = os.path.join(TEMPLATES, match.group(1))
            if os.path.abspath(parent) in seen or not os.path.exists(parent):
                break
            seen.add(os.path.abspath(parent))
            chain.append(parent)
        return list(reversed(chain))

    def _gaps(self, template: str) -> list[str]:
        """Every read on this page that arrives before its own declaration."""
        known: set[str] = set()
        gaps = []
        for source_path in self._lineage(template):
            for _at, kind, payload in self._events(read(source_path)):
                if kind == "script":
                    known |= self._runtime_names(payload)
                    continue
                if kind == "sheet":
                    path = os.path.join(ROOT, payload.lstrip("/"))
                    if not os.path.exists(path):
                        gaps.append(f"{template} links {payload}, which is not there")
                        continue
                    source, where = read(path), payload
                elif kind == "inline-script":
                    known |= self._set_at_runtime(payload)
                    continue
                else:
                    source, where = payload, "an inline <style> block"
                missing = sorted(
                    self._read_without_a_fallback(source) - known - self._declared(source)
                )
                if missing:
                    names = ", ".join(f"--{name}" for name in missing)
                    gaps.append(f"{where} reads {names} with nothing loaded that declares them")
                known |= self._declared(source)
        return gaps

    def test_a_page_that_extends_a_base_inherits_its_sheets(self):
        """`home.html` reads the palette `site_base.html` links, and the walk sees it.

        The failure this pins was a false one: the landing page was reported as
        reading thirty tokens nothing declared, because the walk stopped at the
        child template and never looked at the base whose head loads them. It
        would have passed just as falsely the other way — a child that read a
        token its base did not declare was reported against the child.
        """
        home = os.path.join(TEMPLATES, "home.html")
        lineage = [os.path.basename(path) for path in self._lineage(home)]
        # The front door inherits the rebuilt frame — and this law is precisely the
        # one that makes that checkable rather than assumed, because it walks the
        # lineage instead of naming a file. The old frame it used to name is still
        # the frame for the sign-in doors, the market and the owner console.
        self.assertEqual(lineage[0], "base.html", lineage)
        self.assertEqual(lineage[-1], "home.html", lineage)
        self.assertEqual(self._gaps(home), [])
        # And a child's own sheet is still checked after the base's, which is what
        # makes the pair legal rather than merely unreported.
        base = self._lineage(home)[0]
        base_events = [payload for _at, _kind, payload in self._events(read(base))]
        self.assertIn("/static/meoww.css", base_events)

    def test_the_walk_finds_the_pair_this_law_was_written_for(self):
        """A walk that looks at nothing passes for the wrong reason."""
        template = os.path.join(TEMPLATES, "dashboard.html")
        sheets = [
            payload
            for _at, kind, payload in self._events(read(template))
            if kind == "sheet"
        ]
        self.assertIn("/static/pages.css", sheets)
        self.assertIn("/static/meow.css", sheets)
        # …in that order, which is what makes the second file's reads legal.
        self.assertLess(sheets.index("/static/meow.css"), sheets.index("/static/pages.css"))
        # …and the dependency is real: the layout sheet genuinely does not carry
        # the palette it is written against, so a self-contained check could not
        # have covered it.
        shared = self._read_without_a_fallback(read(DASHBOARD_SHEET)) - self._declared(
            read(DASHBOARD_SHEET)
        )
        self.assertGreater(
            len(shared),
            20,
            "pages.css no longer reads the shared palette — the split was removed, "
            "and so is the reason for this law",
        )
        self.assertEqual(self._gaps(template), [])

    def test_every_page_declares_what_the_sheets_it_loads_read(self):
        """The walk covers every template, so a new page is a page under the law."""
        walked = 0
        gaps: list[str] = []
        for root, _dirs, files in os.walk(TEMPLATES):
            for entry in sorted(files):
                if not entry.endswith(".html"):
                    continue
                walked += 1
                gaps.extend(self._gaps(os.path.join(root, entry)))
        self.assertGreater(walked, 10, "no templates were walked")
        self.assertEqual(
            gaps,
            [],
            "these pages load a sheet that reads a custom property nothing before it "
            "declares, so the browser drops the declaration and draws nothing:\n"
            + "\n".join(gaps),
        )


class EveryThemeOverridesTheBase(unittest.TestCase):
    """A theme may *override* a name. It may never be the only home of one.

    Every themed sheet in this project works the same way, and deliberately:
    ``:root`` (or the block that carries it, ``:root, [data-theme="midnight"]``)
    declares the whole palette, and each theme redefines the handful of values
    that make it a theme — thirteen names in the console, six in the public sheet
    (its themes change the hue and nothing else), twenty-two in the hub. Asking a
    theme to re-declare the other fifty would be a second copy of the palette to
    keep in step, which is the thing this arrangement exists to avoid.

    The arrangement has one failure mode, and nothing else here can see it: a
    name declared *inside* a theme block and nowhere else exists only while that
    theme is selected. Every other theme drops the declaration and paints
    nothing — the silent drop ``EveryTokenIsDefined`` is about, except that the
    name *is* declared somewhere, so a sweep for undeclared names passes. A page
    that is perfect on one theme and broken on the other eight is exactly the
    shape of bug this file exists for.

    So the first law is one direction and stated as a subset: **(names a theme
    block declares) ⊆ (names the base block declares)** — nothing may exist only
    under one theme. The second is the other half, and it is the hub's existing
    check made general: **the themes declare the same names as each other**, so a
    theme cannot quietly drop an override and render that token in the base's
    colours. Equality rather than "at least what the others have", which is how
    this was written first and which could never fail — an intersection of the
    theme sets is by definition a subset of every one of them, so a theme that
    drops an override shrinks the intersection and passes it. The mutation is
    what caught that, and it is why the check reads this way now.

    What is deliberately out of scope: a component that declares its own
    properties where it reads them (``.btn { --btn-text: … }``, the landing
    page's ``--lp-*``, ``.app { --rail: … }``) and a theme-scoped descendant rule
    (``[data-theme="light"] .aurora``). Those are declarations next to their own
    reads rather than palette, and ``EveryTokenIsDefined`` is what covers them.
    """

    def _palette(self, path: str) -> tuple[set[str], dict[str, set[str]]]:
        """One sheet as ``(base names, {theme: names})``.

        A *theme* is a block whose selectors are all theme selectors and nothing
        else — which includes the duplicated media-query form the hub uses
        (``@media (prefers-color-scheme: light) { :root:not([data-theme="dark"])``
        ``{ … } }``), since that is the same theme written for a browser with no
        preference set. A rule *under* a theme selector has a space in it and is
        not one.
        """
        base: set[str] = set()
        themes: dict[str, set[str]] = {}
        for selector, body in blocks(read(path)):
            declared = set(re.findall(r"--([a-z0-9-]+)\s*:", body))
            if not declared:
                continue
            parts = [part.strip() for part in selector.split(",")]
            if any(part == ":root" for part in parts):
                base |= declared
                continue
            if parts and all(" " not in part and "[data-theme" in part for part in parts):
                for part in parts:
                    themes.setdefault(part, set()).update(declared)
        return base, themes

    def test_the_palettes_were_parsed(self):
        """A check that finds no themes cannot fail for the right reason."""
        for path in THEMED_SHEETS:
            with self.subTest(sheet=os.path.relpath(path, ROOT)):
                base, themes = self._palette(path)
                self.assertGreater(len(base), 30, f"{path}: no base token block found")
                self.assertGreaterEqual(len(themes), 1, f"{path}: no theme blocks found")
                for theme, declared in themes.items():
                    self.assertLess(
                        len(declared),
                        len(base),
                        f"{theme} re-declares the whole palette, so this sheet no longer "
                        "has the base-plus-overrides shape the law below is written for",
                    )

    def test_no_theme_block_declares_a_name_the_base_does_not(self):
        offenders = []
        for path in THEMED_SHEETS:
            base, themes = self._palette(path)
            for theme, declared in sorted(themes.items()):
                only_here = sorted(declared - base)
                if only_here:
                    names = ", ".join(f"--{name}" for name in only_here)
                    offenders.append(f"{os.path.relpath(path, ROOT)}: {theme} declares {names}")
        self.assertEqual(
            offenders,
            [],
            "these names are declared inside a theme block and nowhere else, so every "
            "other theme drops them and paints nothing — declare them on the base "
            "block, and let the theme override the value:\n" + "\n".join(offenders),
        )

    def test_every_theme_declares_the_same_overrides(self):
        """A theme that forgets one renders that token in the base's colours.

        Equality, not "at least the others'": a name read from the union catches
        both halves of a disagreement — the theme that dropped an override, and the
        theme that is the only one carrying one — because a name only one theme
        declares is missing from every other theme by definition, which the first
        law above has already refused.
        """
        offenders = []
        for path in THEMED_SHEETS:
            _base, themes = self._palette(path)
            if len(themes) < 2:
                continue  # one theme cannot disagree with itself
            everywhere = set().union(*themes.values())
            for theme, declared in sorted(themes.items()):
                missing = sorted(everywhere - declared)
                if missing:
                    names = ", ".join(f"--{name}" for name in missing)
                    offenders.append(f"{os.path.relpath(path, ROOT)}: {theme} is missing {names}")
        self.assertEqual(
            offenders,
            [],
            "every theme on a sheet has to override the same names, or the ones it "
            "skips render in the base's colours:\n" + "\n".join(offenders),
        )


class MarkupAndSheetAgree(unittest.TestCase):
    """A class name is a promise the sheet has to keep.

    The console's markup and its stylesheet are two halves of one contract, and
    nothing was holding either half to the other. Both directions of the break
    were shipped and neither raised a thing:

    * **A name with no rule.** ``card__foot`` was written three times on the
      Access page and ruled nowhere, so the sentences that qualify a card — "this
      host cannot write to the ledger", "the history these counts carry is no
      longer than uptime" — printed at 16px and full brightness, which is exactly
      the size and weight of the claim they are qualifying. ``toolbar--bare``,
      ``cmds__row is-off`` and ``question__where`` were the same defect with a
      quieter symptom: names asserting a treatment the sheet had never been told
      about (a modifier that strips chrome the base never had, a row that says
      "off" one line above cells that say on, a "where" set apart from a "when"
      by nothing at all).
    * **A rule losing to the markup.** The Overview's two dials sat one under the
      other in a card with half its width empty, because the row borrowed
      ``.telemetry-row`` — the class of a *column* of label/value/bar rows from a
      component nothing renders any more — and then tried to state its real
      layout in an inline style that forgot the one property it was fighting.
      ``flex-wrap: wrap`` was written in that style, which only means anything in
      a row; the sheet said ``column``, and the sheet won.
    * **A flag set for nobody.** The third shape is the same promise made by
      script instead of markup: ``Cursor.tsx`` and ``theme.ts`` put
      ``has-meow-cursor`` on ``<html>`` so a sheet could hide the native pointer,
      which is the rule the *dashboard's* sheet answers — and the console had
      decided the opposite (its ring is a companion to the pointer, pinned by a
      test in ``test_console.py`` that refuses ``cursor: none`` anywhere in the
      sheet). The honest fix was not to give the console a rule it had argued
      against, but to stop setting the flag.

    So the law is written as those two halves, plus the third that has to hold for
    either to mean something: **a layout is the sheet's business.** A static
    ``style={{ … }}`` move the sheet cannot see is how the dials ended up in a
    column the page never asked for, and how 12px of gap lived in a component.
    What stays legal is what only the runtime knows: a computed position, a
    colour chosen from the theme list, and the ``--custom-properties`` that
    *are* values handed to the sheet.

    What this cannot see, said plainly rather than left to be discovered: a class
    name that arrives through a prop (``className={className}``, how ``Link``
    forwards one) and the *modifiers* built inside an interpolation, whose strings
    the scan does read but whose computed halves it cannot. A name that is cut off
    where the interpolation began — ``pill--``, ``card--span-`` — is therefore
    checked as a **prefix**: some rule has to complete it. Both allowances are
    counted in the tests below, so growing them is a decision rather than a
    quiet widening.
    """

    #: The vocabulary both sides use: a lowercase block, hyphens inside it, with
    #: ``__element`` and ``--modifier`` suffixes. Also what stops the scan from
    #: reading JavaScript as a class name.
    CLASS_NAME = re.compile(r"^[a-z][a-z0-9-]*(?:__[a-z0-9-]+)*$")

    #: A ``style={{ … }}`` attribute, opened at the brace. Nested braces are
    #: matched by hand rather than by the pattern, because ``${…}`` inside a
    #: template — ``{{ "--x": `${width}%` }}`` — is a brace inside a brace.
    STYLE_ATTRIBUTE = re.compile(r"style=\{\{")

    def _sources(self) -> list[tuple[str, str]]:
        """Every TypeScript source under ``console/src``: (path, text).

        Comments are removed first, and not as a convenience: this codebase names
        the thing it removed — that is how the next reader learns why a name went
        away — so a law that read comments would fail on the explanation of its
        own fix. Only a quote ends a quote, so the ``//`` in a URL is a URL.
        """
        sources = []
        for root, _dirs, files in os.walk(CONSOLE_SRC):
            for entry in sorted(files):
                if entry.endswith((".ts", ".tsx")):
                    path = os.path.join(root, entry)
                    sources.append((os.path.relpath(path, ROOT), _without_comments(read(path))))
        return sources

    def _ruled(self) -> set[str]:
        """Every class name the console's sheet has a selector for."""
        ruled: set[str] = set()
        for selector, _body in blocks(read(CONSOLE_SHEET)):
            ruled.update(re.findall(r"\.(-?[A-Za-z_][A-Za-z0-9_-]*)", selector))
        return ruled

    def _written(self, source: str) -> set[str]:
        """Class names a file writes as literals — both spellings, plus DOM writes.

        A ``className=`` expression is read for its quoted strings (the modifiers
        of ``cond ? "a is-off" : "a"``) and for the text around each ``${…}``, so
        a template's head and tail are read while whatever completes them keeps
        its freedom. A body that is a bare identifier is skipped: the name is the
        caller's, and a scan that guessed at it would report a page for its
        neighbour's class.
        """
        written: set[str] = set()
        for match in re.finditer(r'className="([^"]*)"', source):
            written.update(match.group(1).split())
        for match in re.finditer(r"className=\{", source):
            body = self._balanced(source, match.end() - 1, "{", "}")
            if re.fullmatch(r"[A-Za-z_$][\w$.]*", body.strip()):
                continue  # a prop: `className={className}`
            head = re.sub(r"\$\{[^{}]*\}", " ", body)
            for chunk in (head, *re.findall(r'"([^"]*)"', body)):
                written.update(chunk.replace("`", " ").split())
        for match in re.finditer(r'documentElement\.classList\.add\("([^"]+)"\)', source):
            written.update(match.group(1).split())
        return {name for name in written if self.CLASS_NAME.match(name)}

    @staticmethod
    def _balanced(text: str, index: int, opening: str, closing: str) -> str:
        """What sits between the ``opening`` at ``index`` and its match."""
        depth = 0
        for cursor in range(index, len(text)):
            if text[cursor] == opening:
                depth += 1
            elif text[cursor] == closing:
                depth -= 1
                if depth == 0:
                    return text[index + 1:cursor]
        return text[index + 1:]

    def test_every_class_the_console_writes_is_ruled(self):
        ruled = self._ruled()
        missing, cut_short = [], []
        for path, source in self._sources():
            for name in sorted(self._written(source)):
                if name in ruled:
                    continue
                if name.endswith("-"):
                    # The rest of the name is chosen at runtime: `pill--` is
                    # written as `` `pill--${tone}` ``, so the sheet is asked to
                    # complete the prefix rather than to define it.
                    if any(selector.startswith(name) for selector in ruled):
                        cut_short.append(name)
                        continue
                missing.append(f"{path}: .{name}")
        self.assertEqual(
            missing,
            [],
            "these class names are written by the app and ruled nowhere, so they "
            "are silently the browser's defaults — give each one a rule, or stop "
            "naming a treatment that does not exist:\n" + "\n".join(missing),
        )
        # The allowance above is real — `card--` and `card--span-`, `pill--`,
        # `toast--`, `swatch--`, the five places a modifier is chosen at runtime
        # — and it is that wide, not a door left open. Growth means a name is
        # being composed further from its rule, which is worth a look rather
        # than a silent pass.
        self.assertLessEqual(
            len(set(cut_short)),
            5,
            f"more class names are cut short than the known five: {sorted(set(cut_short))}",
        )

    def test_the_names_this_law_was_written_for_are_gone(self):
        """Named, because every one of them was invisible in its own diff.

        A modifier that stripped chrome the base never had, a row state nothing
        drew one line above the cells that did draw it, and a "where" set apart
        from its "when" by no rule at all — each rendered exactly as the sheet
        intended, which is to say as nothing. So did the pointer flag the cursor
        script set on ``<html>``, which is the one of the four that a reader would
        most reasonably have taken for a working mechanism, and ``cursor--link``,
        which was written beside ``is-link`` as a second name for the same state:
        the rule it looked like was right there under the other spelling, which is
        why nothing ever read it. ``is-text`` was in that family too — set by the
        cursor script, answered by no sheet — and it is the one of the four the
        console's own sheet still states (the layer steps back over a field), so
        it is asserted present there rather than removed.

        ``card__foot`` is the one name of the set the law *keeps*: it is a real
        treatment — a caveat, held off by a hairline — whose rule was simply
        missing, so what is asserted here is that the Access page still names it
        and the sheet now answers.
        """
        sources = "".join(source for _path, source in self._sources())
        for name in ("toolbar--bare", "cmds__row", "question__where", "has-meow-cursor", "cursor--link"):
            with self.subTest(name=name):
                self.assertNotIn(name, sources)
        self.assertIn("card__foot", sources)
        self.assertIn("card__foot", self._ruled())
        # The two classes the cursor scripts write for their own states. Read off
        # the *sheets*, not the sources: a name written and never answered is the
        # defect above, and these are the pair that were in it.
        #
        # The four cursor states, and the sheet that answers them moved. They
        # were asserted against ``static/meow.css`` here because the *public*
        # page carried a pointer layer too — a glow on a spring, seven trailing
        # particles and four entity divs — and that layer was deleted by
        # decision, which `motion.js`'s own header records and
        # `NoSheetDrawsItsOwnPointer` below is the law for. The only surface that
        # still writes these names is the console, which draws its own layer from
        # `Cursor.tsx` and rules it in its own sheet. Asserting the public sheet
        # answered them was asserting a rule for a layer that no longer exists;
        # the promise the law is actually about — a name written is a name
        # answered — survives, scoped to the sheet that owns the names.
        client = read(os.path.join(CONSOLE_SRC, "app", "Cursor.tsx"))
        for name in ("is-link", "is-text", "is-down", "is-on"):
            with self.subTest(name=name):
                # The state as the script writes it, quoted, so a sentence about
                # the name is not the same thing as the name being written.
                self.assertIn('" ' + name + '"', client)
                self.assertIn(name, self._ruled())


    def test_no_page_carries_a_static_style(self):
        """A hand-written declaration is a layout the sheet cannot see.

        Only the runtime's own values are allowed through: an expression is a
        value this file could not have written down, which is the entire reason
        for the attribute. A literal — ``"12px"``, ``"1px solid var(--line)"`` —
        is a decision that belongs in the sheet, where the next person reading the
        component can see it and the theme can reach it.
        """
        offenders = []
        for path, source in self._sources():
            for match in self.STYLE_ATTRIBUTE.finditer(source):
                body = self._balanced(source, match.end() + 1, "{", "}")
                for entry in self._entries(body):
                    value = entry.split(":", 1)[1].strip() if ":" in entry else ""
                    if value[:1] in ("\"", "'") or value[:1].isdigit():
                        offenders.append(f"{path}: {entry.strip()}")
        self.assertEqual(
            offenders,
            [],
            "these components state their own spacing, colours or type instead of "
            "naming them in the sheet:\n" + "\n".join(offenders),
        )

    @staticmethod
    def _entries(body: str) -> list[str]:
        """A style object's declarations, split on its own top-level commas."""
        entries, depth, current, quote = [], 0, "", ""
        for char in body:
            if quote:
                quote = "" if char == quote else quote
            elif char in "\"'`":
                quote = char
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif char == "," and depth == 0:
                entries.append(current)
                current = ""
                continue
            current += char
        entries.append(current)
        return [entry for entry in entries if ":" in entry]


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


class HiddenMeansHidden(unittest.TestCase):
    """The `hidden` attribute, on every element, whichever sheet names it.

    The UA sheet says ``[hidden] { display: none }`` and **any** author rule that
    sets ``display`` beats it. This project's sheets set ``display`` on almost
    everything they name, so the fix grew one selector at a time —
    ``.command[hidden]``, ``.cmdx__bar[hidden]``, ``.cmdx__status[hidden]``,
    ``.cmdx__empty[hidden]``, ``.cmdx__group[hidden]``, ``.system[hidden]`` — and
    that list was complete right up until the next element to ship ``hidden`` fell
    through the hole it left.

    Measured on the rebuilt station table before this law existed: every row the
    filter had just excluded still computed ``display: grid`` at **359px tall**
    while the count beside the list said *“2 of 308 in Finnish”*, and the two
    controls that exist only for a browser *with* JavaScript — the tools bar and
    the button that opens the rest — were on screen with nothing to do. The patch
    list was not wrong about any of those elements; it just did not mention them,
    which is the whole failure mode of a rule per element.

    So the law is the general form: one rule, in the shared sheet, with
    ``!important`` — because the defect *is* a specificity contest — and no sheet
    patching it back one selector at a time.
    """

    def test_the_shared_sheet_hides_a_hidden_element_by_itself(self):
        rules = [body for selector, body in blocks(read(PUBLIC_SHEET)) if selector == "[hidden]"]
        self.assertEqual(
            len(rules),
            1,
            "static/meow.css has no single `[hidden]` rule, so every element that "
            "ships hidden depends on somebody remembering to patch its own "
            "selector — which is how a filtered list stayed on screen",
        )
        self.assertRegex(
            rules[0],
            r"display:\s*none\s*!important",
            "the `[hidden]` rule has to win the specificity contest it exists for: "
            "a `.radio-row { display: grid }` beats a plain `[hidden] { display: none }`",
        )

    def test_no_sheet_keeps_a_patch_list_beside_it(self):
        """``[hidden]`` anywhere else is that list growing again.

        The one in ``static/meow.css`` is the general rule; a second one in
        ``pages.css`` or ``landing.css`` would mean an element whose own selector
        was written out by hand, which is exactly how the six-selector patch list
        came to exist — and why the seventh element was broken.
        """
        general = read(PUBLIC_SHEET)
        for name in ("pages.css", "landing.css"):
            source = read(os.path.join(ROOT, "static", name))
            with self.subTest(sheet=name):
                self.assertNotIn(
                    "[hidden]",
                    source,
                    f"static/{name} patches `[hidden]` for a selector of its own; "
                    "the rule belongs in the shared sheet, once, where the next "
                    "element gets it too",
                )
        self.assertIn("[hidden]", general)


class NoSheetDrawsItsOwnPointer(unittest.TestCase):
    """The pointer is the browser's own, on every public sheet.

    This class used to hold the opposite guarantee: a paw for the page and a paw
    with a finger for anything pressable, authored in one block because eight
    components had each grown a ``cursor: pointer`` of their own and the layer
    kept losing to them. The apparatus is gone. What is worth a law now is that
    nothing brings it back — an image a browser refuses draws *nothing* and logs
    nothing, so a sheet that names one can silently take the pointer away.
    """

    def test_no_public_sheet_names_a_cursor_image(self):
        for name in ("meow.css", "landing.css", "pages.css"):
            source = _without_comments(read(os.path.join(ROOT, "static", name)))
            with self.subTest(sheet=name):
                self.assertNotIn(
                    "cursor: url(",
                    source,
                    f"static/{name} draws its own pointer over the browser's",
                )

    def test_the_other_public_sheets_leave_the_pointer_alone(self):
        """A page sheet is the easiest place to add one by accident."""
        for name in ("landing.css", "pages.css"):
            source = _without_comments(read(os.path.join(ROOT, "static", name)))
            with self.subTest(sheet=name):
                self.assertNotIn(
                    "cursor:",
                    source,
                    f"static/{name} sets a cursor of its own; the pointer is "
                    "decided once, in the shared sheet, or not at all",
                )


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

    def test_the_dashboard_is_chunked_the_same_way(self):
        """Nine pages, and the same law the owner's rail is held to.

        This was a flat `assertLessEqual(len(DASHBOARD_PAGES), 7)` — a limit that
        only ever held because the page count happened to stay under it, and that
        said nothing about whether the rail could be read. The dashboard grew to
        nine when Members and Server arrived, which is the point the law's own
        remedy applies: chunk it, do not cap it. The assertions are deliberately
        the same ones the owner rail is held to, because it is the same problem.
        """
        import keep_alive

        declared = {page_id for page_id, *_rest in keep_alive.DASHBOARD_PAGES}
        placed = [
            page_id
            for _label, page_ids in keep_alive.DASHBOARD_GROUPS
            for page_id in page_ids
        ]
        self.assertEqual(len(placed), len(set(placed)), "a page is in two groups")

        home = keep_alive.DASHBOARD_PAGES[0][0]
        missing = declared - set(placed)
        self.assertEqual(
            missing,
            {home},
            "these pages are in no group, so the rail would draw them ungrouped: "
            + ", ".join(sorted(missing)),
        )
        self.assertEqual(
            placed[: len(placed)],
            [page_id for page_id, *_rest in keep_alive.DASHBOARD_PAGES if page_id != home],
            "the groups do not hold the pages in the order they were declared",
        )

        for label, page_ids in keep_alive.DASHBOARD_GROUPS:
            self.assertTrue(label.strip(), "a group with no label is not a group")
            self.assertLessEqual(
                len(page_ids),
                5,
                f"the {label} group has {len(page_ids)} entries; five is where a list "
                "stops being scannable",
            )
        self.assertLessEqual(
            len(keep_alive.DASHBOARD_GROUPS),
            5,
            "chunking nine pages into more than five groups is not chunking",
        )

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


class ThePointerIsTheBrowsers(unittest.TestCase):
    """No surface draws its own cursor, and nothing may start.

    This class used to be `ThePointerIsMovedByOneThing`, and it held the shape of
    a cursor layer: a glow, a swarm of seven particles, four entity divs, the
    `translate` the script wrote for each, and the `transform` the sheet was
    forbidden to animate. That layer is gone — from `static/motion.js` and from
    `static/meow.css` — on every surface it ran on.

    Why it went is worth keeping beside the law, because "it was decorative" is
    not the reason. The browser's arrow owns the point: it is what a click hits,
    so a 320px glow centred there is a decoration *behind* a decoration, in every
    state the layer could report. It cost a frame per `pointermove` on all four
    surfaces while only some of them linked the sheet that drew it — `meoww.css`
    never styled the layer, so the public pages ran the loop for particles nobody
    could see — and what it was for, "this is pressable", is a hover state the
    sheets already state without following anything.

    What is left to hold is the absence, in two halves that have to agree: no
    live sheet draws one, and no live script builds one.
    """

    #: Every class name the layer was built from. Any one reappearing is the
    #: feature coming back.
    GONE = (
        "meow-layer",
        "meow-glow",
        "sparkle-particle",
        "vortex-container",
        "radar-hud",
        "sword-entity",
        "blob-drop",
    )

    def test_no_sheet_draws_a_pointer_layer(self):
        for name in ("meow.css", "meoww.css", "pages.css", "market.css"):
            source = _without_comments(read(os.path.join(ROOT, "static", name)))
            for gone in self.GONE:
                with self.subTest(sheet=name, part=gone):
                    self.assertNotIn(gone, source, f"static/{name} draws a pointer layer again")

    def test_no_script_builds_a_pointer_layer(self):
        """The other half: a sheet cannot draw an element no script inserts.

        Read without comments, deliberately: `static/motion.js` *names* the
        layer it used to build — that note is how the next person learns why
        there is no cursor here — and a law that counted prose would make
        recording the removal illegal.
        """
        for rel in ("static/motion.js", "static/meoww.js", "static/market.js"):
            source = _without_comments(read(os.path.join(ROOT, rel)))
            for gone in self.GONE:
                with self.subTest(script=rel, part=gone):
                    self.assertNotIn(gone, source, f"{rel} builds a pointer layer again")

    def test_the_pointer_the_browser_draws_is_left_alone(self):
        """Nothing hides the real pointer where nothing replaces it."""
        for rel in ("static/meow.css", "static/meoww.css"):
            source = _without_comments(read(os.path.join(ROOT, rel)))
            with self.subTest(sheet=rel):
                self.assertNotIn(
                    "cursor: none",
                    source,
                    f"{rel} hides the pointer and puts nothing in its place",
                )


class ALabelColumnKeepsItsRoom(unittest.TestCase):
    """A row of two columns has to give the first one a width.

    Two lists on the public site are a name and the sentence about it — the
    station catalogue and the command list — and neither column was given a
    width. That is not the same as an even split: a flex item with no basis is
    sized by its content, so the longer column (the sentence) takes the whole
    remainder and the shorter one is held at its *min-content* width, which is
    the longest word in it and no more. Measured on the built page, the name
    column of a station row came out at **55px of a 285px row** on a phone and
    **117px of a 651px row** on a desktop, and 82 of the catalogue's 146 rows
    paid for it: the tag broke across two lines at its own hyphen, so a reader
    looking for an ad-free station read ``Chill · ad-`` and then ``free``. The
    command list did the same to ``/music radio``.

    So the law is that these rows state their columns: a basis or a stated grid
    for each one, so a phone moves the sentence *below* the name rather than
    squeezing it — and ``white-space: nowrap`` on the tag, because a hyphen is
    where a line breaker is allowed to break and “ad-free” split in half says the
    opposite of what it is there to say.
    """

    def _declarations(self, css: str, selector: str) -> dict[str, str]:
        """What one selector declares, as written."""
        wanted = " ".join(selector.split())
        found: dict[str, str] = {}
        for rule, body in blocks(css):
            if rule != wanted:
                continue
            for line in body.split(";"):
                name, _, value = line.partition(":")
                if value:
                    found[name.strip()] = value.strip()
        return found

    def test_the_tag_cannot_be_broken_at_its_own_hyphen(self):
        """Re-pinned to the markup the pages write now.

        This law spent a rebuild pointing at ``.lp-tag`` on a page that no longer
        exists, failing for a year of commits about the *absence* of a thing
        rather than about the defect it was written for, which is that a line
        breaker treats a hyphen as an invitation: ``ad-`` on one line and
        ``free`` on the next. The class is ``.mw-tag`` now and it lives in the
        shared sheet, and the tag itself is written by the home page — where one
        of them carries a two-word label, which is exactly the shape the rule
        exists to hold together.
        """
        css = read(os.path.join(os.path.dirname(TEMPLATES), "static", "meoww.css"))
        self.assertEqual(
            self._declarations(css, ".mw-tag").get("white-space"), "nowrap",
            ".mw-tag is what holds a two-word label together: a hyphen is a "
            "break opportunity to a line breaker",
        )
        # Asserted as a search rather than with ``assertIn``: a failure here
        # would print a whole page rather than the one line that matters.
        template = read(os.path.join(TEMPLATES, "home.html"))
        self.assertTrue(
            re.search(r'<span class="mw-tag">', template),
            "the page writes no tag, so the rule above guards nothing and a "
            "label that needs holding together breaks at its own hyphen",
        )

    #: The notes that are a *value* — a reading that belongs at the row's right
    #: edge — rather than a sentence about the name. The list is short on
    #: purpose: a row defaults to the prose shape, which is the shape that
    #: cannot collapse, and naming a value here is how a row opts back out of it.
    VALUE_NOTES = ("room.people", "item.href")

    def test_a_row_that_puts_a_sentence_beside_a_name_declares_the_prose_shape(self):
        """The dashboard's rows, which measured a 0px name column.

        ``.list__row`` gives its first column ``minmax(0, 1fr)`` so a value can
        sit at the right edge, and for ``Lobby | 2 people · live`` that is right.
        The front page also put a *sentence* in that second field — thirteen
        systems, each with its description — and a zero minimum is not a narrow
        column, it is no column: the name rendered one word per line, on top of
        the description, at every width. Nothing could see it, because the
        markup was valid and the sheet was valid and only the *pair* was wrong.

        So the pair is what is checked: a row with a label and a note declares
        ``--prose`` unless its note is one of the values named above, and a row
        with no label at all is exempt because there is no column to keep room
        for. What the class does is asserted as well, or the name could keep its
        class and lose its shape.
        """
        sheet = read(os.path.join(os.path.dirname(TEMPLATES), "static", "pages.css"))
        shape = self._declarations(sheet, ".list__row--prose").get(
            "grid-template-columns", ""
        )
        self.assertNotIn(
            "1.4fr", shape, "the prose row restates the flexible track it replaced"
        )
        self.assertIn(
            "auto", shape,
            ".list__row--prose states no second track, so it is the same row it "
            "was written to replace",
        )
        self.assertEqual(
            self._declarations(sheet, ".list__row--prose .list__note").get(
                "grid-column"
            ),
            "1 / -1",
            "the sentence does not get a line of its own, so it shares the "
            "label's and the label is the column that loses",
        )

        template = read(os.path.join(TEMPLATES, "dashboard.html"))
        rows = re.findall(r'<li class="(list__row[^"]*)">(.*?)</li>', template, re.S)
        self.assertGreaterEqual(len(rows), 6, "the dashboard's rows are not being read")
        offenders = []
        for classes, body in rows:
            if "list__note" not in body or "list__key" not in body:
                continue
            note = body.split('class="list__note"', 1)[1]
            if any(value in note for value in self.VALUE_NOTES):
                continue
            if "list__row--prose" not in classes:
                offenders.append(" ".join(body.split())[:70])
        self.assertFalse(
            offenders,
            "a dashboard row puts a sentence in the second field without "
            "declaring the prose shape, so its name column collapses to 0px and "
            "paints over the sentence: " + "; ".join(offenders),
        )

    def test_a_row_that_carries_a_switch_states_its_shape(self):
        """The front page's list of systems: one switch per line, and no sentence.

        This row is the third shape a `.list__row` gets asked for, and the one
        that has nothing to align *to*: baseline alignment exists so two texts
        share a baseline, and a switch has none — measured at 1280, the row came
        out 51px tall with the name pushed below the control's own middle. It is
        ``--control`` now, centred, with the control being 44px of the row's
        height by itself so the padding is a token rather than a gap.

        The card also gave up its thirteen descriptions, which is what the
        shape is for: they live on the Systems page, and each name here links to
        that system's own card there rather than to the top of a long page.
        """
        sheet = read(os.path.join(os.path.dirname(TEMPLATES), "static", "pages.css"))
        control = self._declarations(sheet, ".list__row--control")
        self.assertEqual(
            control.get("align-items"),
            "center",
            "a row holding a switch keeps the baseline alignment of a row of "
            "text, which a switch has none of",
        )

        template = read(os.path.join(TEMPLATES, "dashboard.html"))
        rows = re.findall(r'<li class="(list__row[^"]*)">(.*?)</li>', template, re.S)
        offenders = [
            " ".join(body.split())[:70]
            for classes, body in rows
            if ("role=\"switch\"" in body or "module_switch(" in body)
            and "list__row--prose" not in classes
            and "list__row--control" not in classes
        ]
        self.assertFalse(
            offenders,
            "a dashboard row carries a switch without stating which shape it "
            "is — prose (it has a sentence too) or control (it is the name and "
            "the control): " + "; ".join(offenders),
        )

    def test_the_footer_does_not_collapse_its_first_column_on_a_phone(self):
        """Four columns with one zero minimum is three columns and a sliver.

        ``.mw-foot__grid`` reserves the first track for the brand with
        ``minmax(0, 1.4fr)``, and the other three are ``minmax(140px, 1fr)``:
        at 390px two 140px tracks and two gaps leave thirty pixels for the
        flexible one, so the brand column measured **14px** — its own sentence
        wrapped one word per line, printed across the list beside it, and the
        footer's links came out as 14px-wide targets. The narrow block stacks
        it. The same file's other half is checked here as well, because a footer
        is where a thumb ends up: the links carry the tap minimum below that
        breakpoint.
        """
        css = read(os.path.join(os.path.dirname(TEMPLATES), "static", "meoww.css"))
        narrow = css.split("@media (max-width: 900px)", 1)[1].split("@media", 1)[0]
        self.assertIn(
            ".mw-foot__grid",
            narrow,
            "the footer keeps four tracks on a phone, which is how its brand "
            "column came to be 14px wide with its text printed across the list "
            "beside it",
        )
        rule = self._declarations(narrow, ".mw-foot__grid")
        self.assertIn(
            "repeat(",
            rule.get("grid-template-columns", ""),
            "the footer's narrow shape does not state its columns",
        )
        self.assertIn(
            ".mw-foot__grid > :first-child",
            narrow,
            "the brand block does not take the whole width, so the lists "
            "squeeze it again",
        )
        self.assertIn(
            "var(--mw-tap)",
            self._declarations(narrow, ".mw-foot__list a, .mw-link, .mw-pages__link").get(
                "min-height", ""
            ),
            "the footer's links and the page's secondary links are text lines "
            "with nothing around them on a phone",
        )


class TheMastheadFitsAPhone(unittest.TestCase):
    """A header row that cannot fit puts its *last* control off the screen.

    Measured on the pre-rebuild page at 390px, the control group (theme, *Add to
    Discord*, *Sign in*, the burger) needed **302px** of the header's 374 while
    the wordmark's own min-content held another 92, so the group ran **27px past
    the right edge** and the burger — on a phone the *only* way into the menu —
    went with it. That row answered by standing the wordmark down.

    The rebuilt frame answers the same arithmetic a different way, and this is
    the law that it does: below its narrow breakpoint the row **sheds** what it
    does not need there — the destination list folds into the Menu button, and
    the call to action leaves the header for the thumb zone, where it is the one
    control a phone actually needs. The wordmark stays, which is the half the old
    version of this law was protecting: a row that fits by dropping the product's
    own name is a row that fits by saying nothing. Both halves are asserted,
    because either one alone is satisfied by a header that simply stopped
    rendering its chrome — the markup would render perfectly without these rules,
    which is the whole problem.
    """

    #: The frame's narrow breakpoint, and the widest it may be: a phone is 390px
    #: in portrait and everything above this has room for the full row.
    NARROW = 900

    def _max_width_blocks(self, css: str) -> list[tuple[int, str]]:
        """``(width, body)`` for every ``@media (max-width: …px)`` block.

        Braces are counted rather than matched, because the rules inside these
        blocks are rules themselves and ``blocks()`` cannot say which media a
        rule sits in — which is the only thing this law is about.
        """
        out: list[tuple[int, str]] = []
        for match in re.finditer(r"@media \(max-width: (\d+)px\)\s*\{", css):
            depth = 0
            for index in range(match.end() - 1, len(css)):
                if css[index] == "{":
                    depth += 1
                elif css[index] == "}":
                    depth -= 1
                    if depth == 0:
                        out.append((int(match.group(1)), css[match.end():index]))
                        break
        return out

    def _narrow(self) -> str:
        """Every narrow block of the frame's own sheet, as one string.

        There may be more than one below the breakpoint and this law is about
        the set rather than about which block a rule landed in — the question is
        whether a narrow screen is described at all.
        """
        sheet = read(os.path.join(os.path.dirname(TEMPLATES), "static", "meoww.css"))
        return "\n".join(
            body for width, body in self._max_width_blocks(sheet) if width <= self.NARROW
        )

    def test_the_row_sheds_the_list_and_the_action_at_the_narrow_breakpoint(self):
        narrow = self._narrow()
        self.assertTrue(
            narrow,
            f"the frame's sheet has no block at or below {self.NARROW}px, so on a "
            "phone the header row holds every destination and the action as well "
            "and the Menu button — the only way into the menu — is pushed off "
            "the edge",
        )
        for selector, why in (
            (".mw-nav__list", "the destination list folds into the Menu button"),
            (".mw-nav .mw-nav__toggle", "the Menu button is what replaces it"),
            (".mw-nav__actions > .mw-btn--primary", "the action leaves the header row"),
            (".mw-sticky", "and arrives in the thumb zone, which is where a phone "
                           "can actually reach it"),
        ):
            with self.subTest(selector=selector):
                rule = re.search(
                    re.escape(selector) + r"\s*\{([^}]*)\}", narrow
                )
                self.assertIsNotNone(rule, f"{selector} is not described: {why}")

    def test_the_name_is_never_the_thing_that_goes(self):
        """A header that fits by dropping the product's name fits by saying
        nothing — and on the rebuilt frame the wordmark is drawn unconditionally,
        so the only way to lose it is a narrow rule that hides it.
        """
        frame = read(os.path.join(TEMPLATES, "meoww", "base.html"))
        self.assertIn('class="mw-brand"', frame, "the frame stopped drawing the wordmark")
        for width, body in self._max_width_blocks(
            read(os.path.join(os.path.dirname(TEMPLATES), "static", "meoww.css"))
        ):
            if width > self.NARROW:
                continue
            with self.subTest(width=width):
                self.assertNotRegex(
                    " ".join(body.split()),
                    r"\.mw-brand[^,{]*\{[^}]*display:\s*none",
                    f"the {width}px block hides the masthead wordmark, so the site "
                    "is nameless on the device that can least afford it",
                )


class TheBackgroundIsBelowThePage(unittest.TestCase):
    """Every page that draws the background layer must lift its own wrapper above it.

    ``meow.css``'s ``.aurora`` is ``position: fixed`` with ``z-index: 0``, and its
    own background ends in an opaque ``linear-gradient(var(--bg), var(--bg-deep))``
    — so it paints **over** every in-flow, static element in the document, which is
    all of a page's content unless the page says otherwise. A positioned element is
    what says otherwise, and this frame's wrapper has said it since the layer was
    written (``.shell``) — which is exactly why the defect went unseen: every page
    that extends the frame was fine, and the one page that brings its own body was
    painted over.

    Found by measuring rather than reading. On the market page at 1280px the rail's
    rows computed **white on ``rgb(16,16,18)`` at ``opacity: 1``** with nothing above
    them in the hit-tested stack, while the screen below the header was black; the
    chart and the header survived and the rail did not, for one reason — a canvas
    inside a positioned panel and a ``position: sticky`` header are painted in the
    positioned layer, and a static list is not.

    The law reads the *template* for what the layer is and what the wrapper is
    called, and the sheets that template links for the answer, so a page that adds
    the background later — or renames its wrapper — is asked the same question
    instead of being missed because a list in here went stale.
    """

    #: The layer, by class, as the templates draw it.
    LAYER = "aurora"

    def _templates_with_the_layer(self) -> list[str]:
        found = []
        for name in sorted(os.listdir(TEMPLATES)):
            if not name.endswith(".html"):
                continue
            path = os.path.join(TEMPLATES, name)
            if f'class="{self.LAYER}"' in read(path):
                found.append(path)
        return found

    def _wrapper_after_the_layer(self, template: str) -> str | None:
        """The first element after the layer that is not part of the layer."""
        source = _without_comments(read(template))
        start = source.find(f'class="{self.LAYER}"')
        if start == -1:
            return None
        for candidate in re.findall(r'<div class="([\w-]+)"', source[start:]):
            if not candidate.startswith(self.LAYER):
                return candidate
        return None

    def _sheets_linked_by(self, template: str) -> list[str]:
        names = re.findall(r"/static/([\w.-]+\.css)", read(template))
        return [os.path.join(ROOT, "static", name) for name in names]

    def _rule_for(self, wrapper: str, sheets: list[str]) -> str | None:
        for sheet in sheets:
            if not os.path.isfile(sheet):
                continue
            for selector, body in blocks(read(sheet)):
                if re.search(rf"\.{re.escape(wrapper)}\b", selector):
                    return body
        return None

    def test_the_layer_is_actually_above_static_content(self):
        """The premise, asserted rather than remembered.

        If the background ever stops being a positioned layer with an opaque
        gradient, the law below is asking the wrong question — and asking it
        forever without noticing would be worse than not asking it.
        """
        rule = None
        for selector, body in blocks(read(PUBLIC_SHEET)):
            if re.search(r"\.aurora\b", selector) and wrapper_is_plain(selector):
                rule = body
                break
        self.assertIsNotNone(rule, "`static/meow.css` has no `.aurora` rule")
        self.assertIn("position: fixed", rule, "the background layer is no longer fixed")
        self.assertIn("z-index: 0", rule, "the background layer no longer claims the base layer")
        self.assertIn(
            "linear-gradient",
            rule,
            "the layer's own background is no longer opaque, so it may no longer be "
            "painting over the pages at all — re-measure before trusting this law",
        )

    def test_every_page_that_draws_the_layer_lifts_its_wrapper(self):
        pages = self._templates_with_the_layer()
        self.assertTrue(pages, "no template draws the background layer any more")
        for template in pages:
            with self.subTest(template=os.path.basename(template)):
                wrapper = self._wrapper_after_the_layer(template)
                self.assertIsNotNone(
                    wrapper,
                    f"{os.path.basename(template)} draws the layer and no wrapper after it",
                )
                rule = self._rule_for(wrapper, self._sheets_linked_by(template))
                self.assertIsNotNone(
                    rule,
                    f".{wrapper} is the wrapper {os.path.basename(template)} draws its page "
                    "inside, and no sheet that template links styles it — so the "
                    "background layer paints over the page",
                )
                self.assertRegex(
                    rule,
                    r"position:\s*(relative|absolute|sticky|fixed)",
                    f".{wrapper} is not positioned, so the fixed background layer "
                    "paints over everything inside it",
                )
                zindex = re.search(r"z-index:\s*([\w-]+)", rule)
                self.assertIsNotNone(
                    zindex,
                    f".{wrapper} is positioned but given no `z-index`, so it stays in "
                    "the same stacking level as the background layer and loses on "
                    "document order",
                )
                self.assertNotIn(
                    zindex.group(1),
                    ("auto", "0"),
                    f".{wrapper} must sit above the background layer's own `z-index: 0`",
                )


def wrapper_is_plain(selector: str) -> bool:
    """Whether a selector list names the layer itself rather than its children."""
    return any(
        part.strip().rstrip(",") == ".aurora" for part in selector.split(",")
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
