"""The brand mark, and the promise a file path makes.

The bug this exists for had no failing test and no error anywhere: `Shell.tsx`
built the operator console's logo path as `/static/logo-<theme>.svg`, the theme
vocabulary grew from four to nine, and only four of the marks were ever drawn — so
on Aurora Borealis, Cosmic Nebula, Cyber Plasma, Sunset Flare and Synthwave the rail
drew the browser's broken-image glyph next to the bot's own name. Nothing raised.
The operator reported it as *"the logo aren't loading"*, which is precisely what a
missing file in an `<img src>` looks like.

So the law was never "the four logos exist" — it is **the list and the disk agree**,
and it is still the law. What moved is who keeps the list. The answer the project
settled on is in `utils/brand.py`, and it is stronger than a list that agrees: there
is **one** mark, `static/logo.svg`, drawn as a badge that carries its own ground, and
`MARKS`/`MARK_FOR` point every palette at it — so the failure above cannot be reached
by adding a theme. `tests/test_brand.py::TheMarkFamilyIsThePaletteFamily` is where the
list, the disk and the two builders are held to each other now, and
`tests/test_brand.py::TheMarkFamilyIsThePaletteFamily::test_no_mark_the_palette_vocabulary_retired_is_still_shipped`
is the half that refuses a per-theme mark coming back.

What is left here is the half that is *this* file's subject: the surfaces must not
build a mark path out of a palette's name, and a palette chosen on one surface has to
be the palette of the next one.

**Retired here**, with what they pinned and where the claim lives now:

* ``TheConsoleAsksForAMarkThatWasDrawn::test_the_theme_vocabulary_was_actually_found``
  and ``::test_every_theme_resolves_to_a_file_that_exists`` read the console's own
  nine-theme vocabulary and its ``THEMED_LOGOS`` list — three marks declared and no
  such files on disk, because the one-mark rebrand deleted them. That arrangement is
  exactly what `utils/brand.py` refuses (its own comment: a theme asked for
  ``logo-aurora.svg``, no such file existed, and the browser drew its broken-image
  glyph beside the product's own name). `tests/test_brand.py` holds the surviving
  rule, and the archived console's ``THEMED_LOGOS`` is dead code rather than a
  promise anything keeps.
* ``ThePublicPagesAskForAMarkThatWasDrawn``'s parse of ``static/theme.js``
  (``THEMED_MARKS``, ``THEMED_MARKS.indexOf``) went the same way: the rebuilt frame
  names one constant mark and no script builds a path from the visitor's choice.
  The class remains, re-pointed at that absence.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THEME_MODULE = ROOT / "console" / "src" / "lib" / "theme.ts"
STATIC = ROOT / "static"
SHELL = ROOT / "console" / "src" / "app" / "Shell.tsx"
#: The script every other page loads in `<head>`, and the only other thing that
#: remembers a theme choice.
PUBLIC_THEME_SCRIPT = ROOT / "static" / "theme.js"


def _storage_key(path: Path) -> str | None:
    """The `localStorage` name a script remembers the theme under.

    Read out of each file rather than restated here: two literals compared to
    each other would have passed while they disagreed with these two. The two
    files spell the declaration differently — TypeScript's module-scope
    ``const STORAGE_KEY``, and the head script's much older ``var KEY`` — so both
    spellings are read and which one a file uses is not this law's business.
    """
    source = path.read_text(encoding="utf-8")
    match = re.search(r'(?:STORAGE_KEY|KEY) = "([^"]+)"', source)
    return match.group(1) if match else None


class TheConsoleAsksForAMarkThatWasDrawn(unittest.TestCase):
    """The console's half: one door to a mark, and one theme on both surfaces.

    The two tests that read the console's nine-theme mark list are retired; the
    module docstring above says what they pinned and why the claim lives in
    ``tests/test_brand.py`` now. What is left is the pair that is still true and
    still worth holding: the component asks ``logoFor()`` instead of deciding
    what exists, and the palette is remembered under one name.
    """

    def test_the_default_mark_exists(self):
        """The fallback every other theme depends on, including the browser's own."""
        self.assertTrue((STATIC / "logo.svg").exists())

    def test_the_component_does_not_build_a_logo_path_by_hand(self):
        """One door, so there is one thing to check and one thing to fix.

        This is the same shape of rule the outbound-URL tests use: the failure was a
        component deciding what exists, so the component is not allowed to know.
        """
        source = SHELL.read_text(encoding="utf-8")
        self.assertNotIn(
            "/static/logo-${",
            source,
            "Shell.tsx builds a themed logo path itself; use logoFor() instead",
        )
        self.assertIn("logoFor(theme)", source)

    def test_the_theme_is_remembered_under_one_name_on_every_surface(self):
        """Two scripts, one entry in `localStorage`, and they never met.

        `static/theme.js` is loaded in `<head>` — without `defer`, so the page
        paints in the right palette — by every page the bot serves: the
        dashboard, the hub, the landing page, the three documents and all four
        sign-in forms. The console's bundle is loaded by one page. They read
        different names: the bundle wrote `meow-console-theme` where every other
        page read `meow-theme`, so a theme chosen in the console was not the
        theme of the next page you opened, and picking one there left the console
        on the old palette. The console was the only surface spelling it
        differently, so it moved — and it still *reads* the old name, so nobody's
        existing choice is reset to midnight by the fix.
        """
        public = _storage_key(PUBLIC_THEME_SCRIPT)
        console = _storage_key(THEME_MODULE)
        self.assertIsNotNone(public, "static/theme.js no longer names its storage key")
        self.assertIsNotNone(console, "theme.ts no longer names its storage key")
        self.assertEqual(
            console,
            public,
            f"the console remembers the theme as {console!r} while every other "
            f"page reads {public!r}, so the choice does not survive a page change",
        )
        source = THEME_MODULE.read_text(encoding="utf-8")
        self.assertEqual(
            source.count('"meow-console-theme"'),
            1,
            "the console's old storage name appears more than once, which means "
            "it is written as well as read — and a write is the thing that would "
            "put a choice back where nothing looks for it",
        )


class ThePublicPagesAskForAMarkThatWasDrawn(unittest.TestCase):
    """The same promise, on the surface that keeps it by not asking at all.

    The console was fixed and the public script was not: it built
    ``logo-<theme>.svg`` from whatever the visitor chose, so picking Sunset
    Flare, Cyber Plasma, Aurora Borealis, Cosmic Nebula or Synthwave put a 404 in
    the header's ``<img>`` — reproduced on the live site by clicking through the
    picker and reading ``naturalWidth``: 64 on the four that exist, **0 on the
    five that do not**. And it was the same defect the console had already
    answered, one file away.

    The rebuild answered it by removing the construction rather than by fixing
    the list of names: there is one mark, the frame names it as a constant in its
    own markup, and no script rewrites it from the palette the visitor picked. So
    what is asserted is that absence — which is a stronger claim than the one it
    replaces, because a list of names cannot be forgotten if nothing is building
    a name — plus the pair the promise still needs: the mark the frame asks for
    is on disk, and the palettes it can be shown under are the ones the sheet
    actually has.
    """

    #: The frame that every rebuilt public page is drawn inside, and the mark it
    #: names. One constant, in one place.
    FRAME = ROOT / "templates" / "meoww" / "base.html"
    MARK = "/static/logo.svg"

    def test_the_frame_names_one_mark_and_it_is_on_disk(self):
        frame = self.FRAME.read_text(encoding="utf-8")
        self.assertIn(f'src="{self.MARK}"', frame, "the frame stopped naming a mark")
        self.assertIn(
            'href="/static/favicon.svg"',
            frame,
            "the frame names no icon, so the browser asks for /favicon.ico and "
            "gets this deployment's 404 page as a picture",
        )
        self.assertTrue((STATIC / "logo.svg").exists())
        self.assertTrue((STATIC / "favicon.svg").exists())

    def test_no_public_script_builds_a_mark_path_from_a_palette(self):
        """The mechanism, not just the outcome — and this is the whole fix.

        A path built from the palette's own name is what put a 404 in the header,
        and the way a rebuild can quietly reintroduce it is one script deciding to
        be clever about a theme-aware asset. The check is a search rather than a
        parse on purpose: a name built anywhere is the defect, whatever shape it
        is built in — and the failure message says so instead of printing the
        script.
        """
        for path in sorted(STATIC.glob("*.js")):
            with self.subTest(script=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertFalse(
                    re.search(r"['\"`/]logo-", source),
                    f"{path.name} builds a mark path from a palette name, so the "
                    "next palette added to the menu asks for a file nobody drew",
                )


class TheBrandMarkCannotGoMissing(unittest.TestCase):
    def test_the_static_build_discovers_the_marks_rather_than_listing_them(self):
        """The other half of the promise, and the reason it can come back.

        ``scripts/build_owner.py`` copies the files the owner host serves, and it used
        to name four logos by hand — which is how a fifth one would go missing. A
        listed file is a promise somebody has to remember; a discovered one is not.
        """
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import build_owner  # noqa: PLC0415

        # The mark *family* rather than the logos alone: the brand draws one
        # wordmark and one icon, and the floor exists so this cannot pass by
        # checking an empty set — which is what a name pattern matching nothing
        # would do.
        patterns = ("logo", "favicon")
        copied = {
            name for name in build_owner.STATIC_ASSETS
            if name.startswith(patterns)
        }
        on_disk = {
            path.name for path in STATIC.iterdir()
            if path.name.startswith(patterns) and path.suffix == ".svg"
        }
        self.assertEqual(
            on_disk - copied,
            set(),
            "a mark exists in static/ but the owner build does not copy it",
        )
        self.assertGreaterEqual(len(copied), 2, "the mark family was not found at all")


if __name__ == "__main__":
    unittest.main()
