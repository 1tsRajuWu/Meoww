"""The brand is one fact, so it gets one author and a law that says so.

A rebrand is the largest change this project makes that **no test can see the
result of**: a wrong colour is a wrong colour, and every assertion in the suite
would pass with the old palette in the sheet, the old name in the header and the
old mark on disk. What *can* be held is agreement, which is the half that
actually breaks — the name spelled in one file and assumed in another, a mark
drawn for a palette nobody ships, an accent that moved in the stylesheet and
stayed behind in `utils/style.py`, because those are two unrelated declarations
of one colour and nothing had ever compared them.

So this file is the rebrand's own guard, and it is deliberately *not* about
taste: it says nothing about whether teal is a good accent. It says that the
value in `utils/brand.py` is the value the stylesheet declares, that every mark
on disk is one the module names and vice versa, that the two builders which copy
a `static/` asset are listing the same family, and that no page a person reads
still carries the name this product had before.

The last one is a *rendered* check on purpose. The old codename stays in
`MEOW_*` environment variables, the Pages project names, the `meow_bank` table,
the `meow-theme` `localStorage` key and `static/meow.css` — all of which are
names that live in a host's configuration or in stored state, where changing one
is a migration rather than an edit. Those are listed in `brand.INTERNAL_NAMES`
and this file leaves them alone. What it refuses is the old name in *markup*:
that is the surface where the last four rounds of this project kept finding a
stale copy nobody had looked at.
"""
from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from utils import brand, style

ROOT = Path(__file__).resolve().parents[1]
SHEET = ROOT / "static" / "meow.css"
TEMPLATES = ROOT / "templates"
KEEP_ALIVE = ROOT / "keep_alive.py"
BUILD_OWNER = ROOT / "scripts" / "build_owner.py"

#: The in-product vocabulary that is deliberately *not* the brand name. These are
#: the words a rename reaches last, and the plan chose user-facing strings only:
#: `/meow`, Meow Bank, the Meow Fee and the Meow Treat keep their names, because
#: renaming them touches stored rows and every card that quotes a fee. Written as
#: an explicit allow-list so the law below cannot be satisfied by adding a word
#: to a regex.
KEPT_VOCABULARY = ("Bank", "Fee", "Treat")

#: …and the subset of that vocabulary the *templates* actually render. The Meow
#: Fee and the Meow Treat are named in the bot's own cards and in the docs rather
#: than on a page, so asserting them here would be asserting that a page says
#: something it has never said — which is how a law starts failing for the wrong
#: reason. The bank is the one the site draws, on the market page and the economy
#: panel, so it is the one this file can hold.
RENDERED_VOCABULARY = ("Meow Bank",)

#: Capital-M "Meow" that is *not* one of the kept words. The negative lookahead is
#: the whole rule: "Meow Hub" is the old brand and fails, "Meow Bank" is a fee and
#: passes, and a lowercase `/meow` command name is not matched at all because a
#: command is not a brand.
RETIRED_WORD = re.compile(r"Meow(?!\s?(?:" + "|".join(KEPT_VOCABULARY) + r"))")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _root_block(css: str) -> str:
    """The `:root { … }` declaration text — where the palette is stated."""
    match = re.search(r":root\s*\{(.*?)\n\}", css, re.DOTALL)
    if match is None:  # pragma: no cover - the sheet is missing its palette
        raise AssertionError("static/meow.css declares no :root block")
    return match.group(1)


class TheNameHasOneAuthor(unittest.TestCase):
    """A name spelled twice is a name that will disagree with itself."""

    def test_the_initial_is_the_names_own_letter(self):
        self.assertEqual(brand.INITIAL, brand.NAME[:1].upper())

    def test_the_fallback_name_is_this_module_and_not_a_literal(self):
        """`_bot_display_name` prefers Discord's name, and falls back *here*.

        The fallback is what a page shows before the gateway connects, which on a
        host that sleeps is the first paint of every cold visit — so it is the
        brand, and it was spelled out in the middle of a ten-thousand-line file
        where nobody rebranding would think to look.
        """
        source = read(KEEP_ALIVE)
        self.assertIn("brand.NAME", source, "keep_alive no longer reads the brand module")
        self.assertNotIn(
            f'"{brand.RETIRED_NAME}"',
            source,
            "keep_alive.py still spells the retired name by hand",
        )

    def test_no_template_carries_the_retired_name(self):
        """Every rendered page, including the hub's own two files."""
        offenders = []
        for path in sorted(TEMPLATES.rglob("*.html")):
            for number, line in enumerate(read(path).splitlines(), start=1):
                found = RETIRED_WORD.search(line)
                if found:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{number}: {line.strip()[:90]}"
                    )
        self.assertEqual(
            offenders,
            [],
            "these rendered surfaces still say the name this product had before "
            "the rebrand (Meow Bank, the Meow Fee and the Meow Treat are kept on "
            "purpose and are not matched):\n" + "\n".join(offenders),
        )

    def test_the_kept_vocabulary_is_actually_kept(self):
        """So the allow-list above cannot quietly become a to-do list.

        The rule is "no rename of in-product vocabulary", and a law that only
        forbids the old brand name would pass just as well if somebody had gone
        through the economy renaming the bank — which is the change this file was
        written to make deliberate rather than incidental.
        """
        rendered = "\n".join(read(path) for path in TEMPLATES.rglob("*.html"))
        # Collected and compared as a list rather than asserted one at a time:
        # `assertIn` prints its whole container on failure, which for a join of
        # every template in the tree is a screenful of markup and no answer.
        missing = [phrase for phrase in RENDERED_VOCABULARY if phrase not in rendered]
        self.assertEqual(
            missing,
            [],
            "these in-product names have gone from the markup; if that rename was "
            "intended it needs its own migration and its own pass",
        )


class TheAccentHasOneAuthor(unittest.TestCase):
    """One colour, two declarations, one of which Discord reads as an integer."""

    def test_the_card_accent_is_this_module(self):
        self.assertEqual(style.ACCENT, brand.ACCENT)
        self.assertEqual(f"#{brand.ACCENT:06x}", brand.ACCENT_HEX)

    def test_the_sheet_declares_the_palette_this_module_names(self):
        declared = dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", _root_block(read(SHEET))))
        missing = sorted(name for name in brand.PALETTE if name not in declared)
        self.assertEqual(missing, [], "the sheet no longer declares these tokens")
        wrong = {
            name: (declared[name].strip(), value)
            for name, value in brand.PALETTE.items()
            if declared[name].strip() != value
        }
        self.assertEqual(
            wrong,
            {},
            "utils/brand.py and static/meow.css disagree about the palette; one "
            "of them is a copy of the other and the sheet is the author for the "
            "browser:\n" + "\n".join(f"--{k}: sheet {v[0]!r}, brand {v[1]!r}" for k, v in wrong.items()),
        )

    def test_every_palette_names_the_accent_the_same_way(self):
        """Both halves of the accent — the fill and the shade used for text.

        A rebrand that moves `--accent` and leaves `--accent-text` is the defect
        the two names exist to prevent: a teal button and a coral link, which
        reads as a colour somebody forgot rather than as two deliberate floors.
        """
        css = read(SHEET)
        blocks = dict(
            (selector, body)
            for selector, body in re.findall(r'(\[data-theme="[a-z]+"\][^{]*)\{(.*?)\n\}', css, re.DOTALL)
        )
        self.assertEqual(sorted({re.search(r'"(light|contrast)"', s).group(1) for s in blocks}), ["contrast", "light"])
        for selector, body in blocks.items():
            tokens = dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", body))
            theme = re.search(r'"(light|contrast)"', selector).group(1)
            # Every theme must restate the accent pair, not inherit one half.
            for name in ("accent", "accent-text", "accent-rgb", "accent-text-rgb"):
                self.assertIn(name, tokens, f"the {theme} palette dropped --{name}")
            self.assertNotEqual(
                tokens["accent"].strip(),
                brand.ACCENT_HEX,
                f"the {theme} palette reuses the house accent, which is under the "
                "text floor on a light ground",
            )


class TheMarkFamilyIsThePaletteFamily(unittest.TestCase):
    """Three palettes, three marks — and the two builders ship the same family."""

    def test_every_palette_has_a_mark_and_a_favicon(self):
        for theme in brand.THEMES:
            with self.subTest(theme=theme):
                self.assertIn(theme, brand.MARK_FOR)
                self.assertIn(theme, brand.FAVICON_FOR)
                self.assertIn(brand.mark_for(theme), brand.MARKS)
                self.assertIn(brand.favicon_for(theme), brand.FAVICONS)

    def test_an_unknown_palette_falls_back_to_the_default_mark(self):
        """A mark that does not exist is invisible, and nothing reports it.

        The generation before this one shipped five themes that asked for a logo
        nobody had drawn, so the header rendered the browser's broken-image glyph
        on every page — no console error, no failed request the page could see,
        and nothing in the suite. Failing *closed to a real file* is the fix.
        """
        self.assertEqual(brand.mark_for("no-such-theme"), brand.MARKS[0])
        self.assertEqual(brand.favicon_for(""), brand.FAVICONS[0])

    def test_every_named_mark_is_on_disk(self):
        for name in brand.ASSETS:
            with self.subTest(asset=name):
                self.assertTrue((ROOT / "static" / name).exists(), f"static/{name} is missing")

    def test_no_mark_the_palette_vocabulary_retired_is_still_shipped(self):
        """The old family was named after a palette vocabulary that is gone.

        `logo-sakura.svg`, `logo-forest.svg` and `logo-ember.svg` were drawn for a
        nine-theme menu, and a second generation (`logo-light`, `logo-contrast`)
        was drawn before the mark became a badge that carries its own ground.
        Both are the shape this project keeps deleting: a name kept "in case",
        which the next reader has to re-decide about — and a themed mark is also
        a second chance for a palette to ask for a file nobody drew.
        """
        for retired in ("sakura", "forest", "ember", "light", "contrast"):
            for family in ("logo", "favicon"):
                path = ROOT / "static" / f"{family}-{retired}.svg"
                with self.subTest(asset=path.name):
                    self.assertFalse(
                        path.exists(),
                        f"static/{path.name} was drawn for a retired palette name and "
                        "is still shipped; the mark family is one badge now",
                    )

    def test_the_two_builders_ship_the_same_family(self):
        """`hub.SHARED_ASSETS` and `build_owner.py`, which copy assets by name.

        Both are lists that a renamed file silently falls off: the build refuses
        at ship time when a page names an asset nobody ships, and a page that
        names *nothing* is a page with no logo — which is the failure mode this
        repository recorded twice, once as a broken-image glyph on five themes.
        """
        from utils import hub

        self.assertEqual(
            set(brand.ASSETS),
            {name for name in hub.SHARED_ASSETS if name != "hub.css"},
            "utils/hub.py's shared-asset list and the brand module's family differ",
        )
        # The owner build reads the family rather than naming it, which is the
        # point: a list of file names is the copy that goes stale, and this one
        # had named four marks drawn for a nine-theme vocabulary for two releases
        # after that vocabulary was gone.
        owner = read(BUILD_OWNER)
        self.assertIn(
            "*brand.ASSETS",
            owner,
            "scripts/build_owner.py no longer copies the brand's asset family",
        )

    def test_every_mark_is_a_well_formed_svg(self):
        """A `.svg` that does not parse is a broken image with no other symptom."""
        for name in brand.MARKS + brand.FAVICONS + brand.CURSORS:
            with self.subTest(asset=name):
                tree = ET.parse(ROOT / "static" / name)
                root = tree.getroot()
                self.assertTrue(root.get("viewBox"), f"{name} declares no viewBox")
                source = read(ROOT / "static" / name)
                self.assertTrue(
                    brand.ACCENT_HEX in source.lower() or "currentColor" in source,
                    f"{name} carries neither the brand accent nor currentColor",
                )

    def test_the_logo_carries_its_name_for_a_screen_reader(self):
        """The mark is the only image on every page, and it has a `role`."""
        for name in brand.MARKS:
            with self.subTest(asset=name):
                tree = ET.parse(ROOT / "static" / name)
                self.assertEqual(tree.getroot().get("role"), "img")
                self.assertEqual(tree.getroot().get("aria-label"), brand.NAME)


class TheModuleIsOneAuthorByConstruction(unittest.TestCase):
    """It imports nothing, so no import can make it disagree with itself."""

    def test_it_is_dependency_free(self):
        source = read(ROOT / "utils" / "brand.py")
        imports = [
            line.strip()
            for line in source.splitlines()
            if line.startswith(("import ", "from ")) and "__future__" not in line
        ]
        self.assertEqual(
            imports,
            [],
            "utils/brand.py must stay importable by the bot, the Flask thread, the "
            "two builders and the suite alike — which means importing nothing:\n"
            + "\n".join(imports),
        )

    def test_every_internal_name_that_keeps_the_codename_is_listed(self):
        """The exception list is a decision, so it is asserted to exist.

        Not a check that the list is *complete* — that would need to know every
        identifier in the tree — but that the places carrying the old codename in
        a way a rename would break are written down where the next rebrand will
        read them.
        """
        self.assertGreaterEqual(len(brand.INTERNAL_NAMES), 5)
        self.assertIn("the meow-theme localStorage key", brand.INTERNAL_NAMES)


if __name__ == "__main__":
    unittest.main()
