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


class TheBrandMarkCannotGoMissing(unittest.TestCase):
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

    def test_the_static_build_discovers_the_marks_rather_than_listing_them(self):
        """The other half of the promise, and the reason it can come back.

        ``scripts/build_owner.py`` copies the files the owner host serves, and it used
        to name four logos by hand — which is how a fifth one would go missing. A
        listed file is a promise somebody has to remember; a discovered one is not.
        """
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import build_owner  # noqa: PLC0415

        copied = {name for name in build_owner.STATIC_ASSETS if name.startswith("logo")}
        on_disk = {path.name for path in STATIC.glob("logo*.svg")}
        self.assertEqual(
            on_disk - copied,
            set(),
            "a logo exists in static/ but the owner build does not copy it",
        )
        self.assertGreaterEqual(len(copied), 2)


if __name__ == "__main__":
    unittest.main()
