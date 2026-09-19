"""One product: the pages must not each carry their own copy of the system.

The dashboard, the market and the landing page all link `static/meow.css`. They
used not to agree: the dashboard re-declared every token under the *same name*
with a different value, so a page linking the shared sheet and this one painted
two palettes at once — its `--bg` was the blue-black that `meow.css` documents as
the thing it moved away from, its accent was Discord blurple beside the shared
indigo, and its radii disagreed by two pixels. None of that is visible as a bug.
It is visible as two products.

These tests are about the *rules* that keep it that way, because every one of
them failed silently: a template adding `:root { --bg: ... }` looks like a
reasonable local choice, and nothing on screen says otherwise.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHEET = ROOT / "static" / "meow.css"
MOTION = ROOT / "static" / "motion.js"
TEMPLATES = sorted((ROOT / "templates").glob("*.html"))

#: The names the shared sheet owns. A template that gives one of these a literal
#: value is a second palette, whether or not it meant to be.
SHARED = {
    "bg", "surface", "surface-card", "surface-hover", "border", "border-active",
    "text-primary", "text-secondary", "text-muted", "accent", "accent-hover",
    "accent-text", "accent-quiet", "green", "red", "gold", "radius-lg",
    "radius-md", "radius-sm", "font-sans", "font-mono", "ease",
}


class OnePalette(unittest.TestCase):
    def setUp(self):
        self.sheet = SHEET.read_text(encoding="utf-8")

    def test_the_shared_sheet_defines_every_token_it_owns(self):
        """The floor under the next test: a name absent here is one the pages
        cannot resolve, and an unresolved `var()` is not an error — it is an
        empty value, which is how a rule silently does nothing."""
        for token in sorted(SHARED):
            self.assertRegex(
                self.sheet, rf"--{re.escape(token)}\s*:",
                f"meow.css no longer defines --{token}",
            )

    def test_no_page_gives_a_shared_token_its_own_value(self):
        for page in TEMPLATES:
            text = page.read_text(encoding="utf-8")
            for token in sorted(SHARED):
                for match in re.finditer(
                    rf"--{re.escape(token)}\s*:\s*([^;{{}}]+);", text
                ):
                    value = match.group(1).strip()
                    self.assertTrue(
                        value.startswith("var("),
                        f"{page.name} gives --{token} the literal {value!r}; "
                        "the palette belongs in meow.css, where every page sees it",
                    )

    def test_every_token_a_page_asks_for_is_defined_somewhere(self):
        """A `var()` naming nothing is an empty value, so the rule around it
        quietly does nothing — the failure mode that made the dashboard's two
        palettes so hard to see in the first place.

        Defined by the shared sheet, or by the page itself: a page-local name
        that aliases a shared token (`--panel: var(--surface-card)`, which is how
        the market keeps reading the way it always did) is fine, and the test
        above is what stops the alias from carrying a value of its own.
        """
        for page in TEMPLATES:
            text = page.read_text(encoding="utf-8")
            local = set(re.findall(r"(--[a-z0-9-]+)\s*:", text))
            for match in re.finditer(r"var\((--[a-z0-9-]+)\)", text):
                name = match.group(1)
                if name in local:
                    continue
                self.assertRegex(
                    self.sheet, rf"{re.escape(name)}\s*:",
                    f"{page.name} asks for {name}, which nothing defines",
                )


class OneMotionSystem(unittest.TestCase):
    """`static/motion.js` on both pages, and never a curtain in front of a tool.

    The primitives are opt-in through `data-` attributes, so a page that wants
    none of them gets none of them. The one thing a page must not opt into by
    accident is the loader: a landing page's job is to arrive, a control panel's
    is to already be there.
    """

    def setUp(self):
        self.script = MOTION.read_text(encoding="utf-8")

    def test_every_reveal_hands_the_element_back_to_its_stylesheet(self):
        """An inline `transform`/`opacity` outranks every CSS rule for the life
        of the page, so a reveal that does not clear them removes behaviours it
        never touched — the dashboard's `.module-card.busy` dimming and any
        hover spring on a revealed card. Invisible in both files, because each
        one is correct on its own."""
        self.assertIn("function release()", self.script)
        self.assertIn('removeProperty("transform")', self.script)
        self.assertIn('removeProperty("opacity")', self.script)
        self.assertIn("}, release);", self.script, "the release never runs")

    def test_the_dashboard_does_not_put_a_curtain_in_front_of_itself(self):
        """Checked on the elements, not on the file: the dashboard *discusses*
        the curtain in a comment, and a test that cannot tell a comment from
        markup is a test that would rather delete the explanation."""
        dashboard = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
        for tag in re.findall(r"<[a-z]+[^>]*>", dashboard):
            self.assertNotIn("data-loader", tag, f"curtain: {tag[:120]}")

    def test_the_dashboard_uses_the_shared_primitives(self):
        dashboard = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("data-reveal", dashboard)
        self.assertIn("motion.js", dashboard)

    def test_a_revealed_element_is_never_also_hover_sprung(self):
        """Both springs write `transform`, so on one element they fight: the
        reveal's frame and the hover's frame overwrite each other, and whichever
        finishes first loses the element. The dashboard puts the reveal on the
        grid and the spring on the card for exactly this reason."""
        dashboard = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
        for tag in re.findall(r"<[a-z]+[^>]*>", dashboard):
            if "data-reveal" in tag and "data-hover" in tag:
                self.fail(f"reveal and hover share one element: {tag[:120]}")


class AssetsAreVersionedByTheirOwnContents(unittest.TestCase):
    """`/static/*` is served `immutable` for a year.

    That is only safe while the URL changes when the file does, and a
    hand-written `?v=2.2` does not: the dashboard, the login page, the market and
    the policies all shipped one, on the favicon and on `particles.js`, and none
    of them would have changed on an edit. The sheet itself was the one that had
    *no* token at all, which is how the whole family was found.
    """

    def test_no_template_carries_a_hand_written_version(self):
        for page in TEMPLATES:
            text = page.read_text(encoding="utf-8")
            for match in re.finditer(r'(?:src|href)="(/static/[^"]+)"', text):
                url = match.group(1)
                self.assertNotRegex(
                    url, r"\?v=(?!\{\{)\w",
                    f"{page.name} hard-codes a version on {url}; "
                    "use asset_version() so the token follows the file",
                )

    def test_every_versioned_asset_asks_for_a_token(self):
        seen = set()
        for page in TEMPLATES:
            text = page.read_text(encoding="utf-8")
            for match in re.finditer(r"asset_version\('([^']+)'\)", text):
                seen.add(match.group(1))
        self.assertIn("meow.css", seen)
        self.assertIn("motion.js", seen)

    def test_the_named_assets_really_exist(self):
        """A token for a file that is not there is a 404 with a cache-buster."""
        for name in ("meow.css", "motion.js", "favicon.svg"):
            self.assertTrue(
                (ROOT / "static" / name).is_file(), f"static/{name} is missing"
            )


class NoDecorativeCanvas(unittest.TestCase):
    """`particles.js` drew a full-viewport canvas of drifting dots and paw prints.

    It was the page's only ornament that carried no information, and it was the
    expensive kind: a `requestAnimationFrame` loop running for the whole life of
    every page it loaded on, redrawing 45 shapes a frame behind content that
    never moved, on a control panel somebody leaves open. It was loaded by five
    templates and by none of the two that were designed after it, which is the
    tell — a new page never reached for it.

    Removing a decoration is not something a test can notice, so this pins the
    decision rather than the behaviour: the file and every reference to it are
    gone, so putting the canvas back has to be deliberate.
    """

    def test_the_file_is_gone(self):
        self.assertFalse(
            (ROOT / "static" / "particles.js").exists(),
            "static/particles.js is back; the pages were designed without it",
        )

    def test_no_page_loads_a_particle_canvas(self):
        for page in TEMPLATES:
            self.assertNotIn(
                "particles",
                page.read_text(encoding="utf-8"),
                f"{page.name} loads the particle canvas again",
            )


if __name__ == "__main__":
    unittest.main()
