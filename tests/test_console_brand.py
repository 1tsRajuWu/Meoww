"""The console's brand mark, and the promise a file path makes.

The bug this exists for had no failing test and no error anywhere: `Shell.tsx`
built the operator console's logo path as `/static/logo-<theme>.svg`, the theme
vocabulary grew from four to nine, and only four of the marks were ever drawn — so
on Aurora Borealis, Cosmic Nebula, Cyber Plasma, Sunset Flare and Synthwave the rail
drew the browser's broken-image glyph next to the bot's own name. Nothing raised.
The operator reported it as *"the logo aren't loading"*, which is precisely what a
missing file in an `<img src>` looks like.

So the law is not "the four logos exist" — it is **the list and the disk agree**:

* every theme whose mark is on disk must be named in ``THEMED_LOGOS`` (or the mark
  is drawn on disk and never shipped);
* every theme named there must have the file (or the console asks for a 404);
* every theme *not* named there must fall back to ``logo.svg``;
* and no component may build a logo path by hand, because the whole failure was a
  component deciding for itself what exists.
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
    each other would have passed while they disagreed with these two.
    """
    match = re.search(r'STORAGE_KEY = "([^"]+)"', path.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def _theme_ids() -> list[str]:
    """The theme vocabulary, read out of the module that owns it."""
    source = THEME_MODULE.read_text(encoding="utf-8")
    block = source.split("export const THEMES", 1)[1].split("] as const;", 1)[0]
    return re.findall(r'id: "([a-z0-9-]+)"', block)


def _themed_logos() -> list[str]:
    """The themes declared to have a mark of their own."""
    source = THEME_MODULE.read_text(encoding="utf-8")
    block = source.split("export const THEMED_LOGOS", 1)[1].split("];", 1)[0]
    return re.findall(r'"([a-z0-9-]+)"', block)


def _public_theme_ids() -> list[str]:
    """The theme vocabulary as ``static/theme.js`` writes it."""
    source = PUBLIC_THEME_SCRIPT.read_text(encoding="utf-8")
    block = source.split("var THEMES = [", 1)[1].split("];", 1)[0]
    return re.findall(r'"([a-z0-9-]+)"', block)


def _public_themed_marks() -> list[str]:
    """The themes ``static/theme.js`` declares a mark for."""
    source = PUBLIC_THEME_SCRIPT.read_text(encoding="utf-8")
    block = source.split("var THEMED_MARKS = [", 1)[1].split("];", 1)[0]
    return re.findall(r'"([a-z0-9-]+)"', block)


class TheConsoleAsksForAMarkThatWasDrawn(unittest.TestCase):
    def test_the_theme_vocabulary_was_actually_found(self):
        """A parse that returns nothing would make every law below vacuous.

        Worth its own test: the whole class is a text read of a TypeScript file, and
        a refactor that renamed the export would leave four green assertions about an
        empty list — which is how a law quietly stops being one.
        """
        ids = _theme_ids()
        self.assertGreaterEqual(len(ids), 4, f"only found {ids}")
        self.assertIn("midnight", ids)
        self.assertTrue(_themed_logos())

    def test_every_theme_resolves_to_a_file_that_exists(self):
        """Both directions of the same promise, in one place.

        ``midnight`` is the default and asks for ``logo.svg``, which is why it is in
        neither direction — the themed set is *additions* to the default, not a
        complete listing of it.
        """
        themed = set(_themed_logos())
        for theme in _theme_ids():
            with self.subTest(theme=theme):
                drawn = (STATIC / f"logo-{theme}.svg").exists()
                self.assertEqual(
                    drawn,
                    theme in themed,
                    f"{theme}: file exists={drawn}, declared={theme in themed}"
                    + (
                        " — the console would ask for a logo nobody drew"
                        if theme in themed
                        else " — a mark is on disk and never shipped"
                    ),
                )

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
    """The same promise, on the surface that did not keep it.

    The console was fixed and the public script was not: it built
    ``logo-<theme>.svg`` from whatever the visitor chose, so picking Sunset
    Flare, Cyber Plasma, Aurora Borealis, Cosmic Nebula or Synthwave put a 404 in
    the header's ``<img>`` — reproduced on the live site by clicking through the
    picker and reading ``naturalWidth``: 64 on the four that exist, **0 on the
    five that do not**. And it was the same defect the console had already
    answered, one file away.
    """

    def test_the_vocabulary_was_actually_found(self):
        self.assertGreaterEqual(len(_public_theme_ids()), 4, "no themes were parsed")
        self.assertTrue(_public_themed_marks(), "the script names no themed mark")

    def test_every_theme_resolves_to_files_that_exist(self):
        """Both directions, for the mark and for the favicon.

        A theme whose art is on disk but unnamed loses it; a theme named with no
        art asks for a 404. The favicon is checked in the same breath because it
        is the same three files and the same rewrite — a theme with a mark and no
        icon is the bug half-fixed.
        """
        named = set(_public_themed_marks())
        for theme in _public_theme_ids():
            for kind, name in (("logo", f"logo-{theme}.svg"),
                               ("favicon", f"favicon-{theme}.svg")):
                with self.subTest(theme=theme, kind=kind):
                    drawn = (STATIC / name).exists()
                    self.assertEqual(
                        drawn,
                        theme in named,
                        f"{theme}: {name} exists={drawn}, named in THEMED_MARKS="
                        f"{theme in named}",
                    )

    def test_the_two_surfaces_name_the_same_marks(self):
        """They draw from one directory, so a difference is one of them wrong."""
        self.assertEqual(
            sorted(_public_themed_marks()),
            sorted(_themed_logos()),
            "static/theme.js and console/src/lib/theme.ts disagree about which "
            "themes have a mark of their own",
        )

    def test_the_script_asks_the_list_rather_than_the_chosen_name(self):
        """The mechanism, not just the outcome: a path built from the visitor's
        choice is what put a 404 in the header, so the choice is passed through a
        list of what was drawn."""
        source = PUBLIC_THEME_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("THEMED_MARKS.indexOf(themeName)", source)
        self.assertNotIn(
            'themeName === DEFAULT ? "" : "-" + themeName',
            source,
            "the suffix is built from the chosen theme again, so the next theme "
            "added to the menu breaks the logo",
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
