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

#: The in-product vocabulary that is *not* the brand name: the house account, the
#: fee on a transfer and the fee on a fill. A first pass left these on the older
#: name — renaming them looked like it reached stored rows and every card that
#: quotes a fee — and the pass this list now records finished the job, because the
#: *rows* are named inside the code (`meow_bank`, `meow_fee_enabled`) and only the
#: words a person reads had to move. Written as an explicit allow-list so the law
#: below cannot be satisfied by adding a word to a regex.
KEPT_VOCABULARY = ("Bank", "Fee", "Treat")

#: …and the subset of that vocabulary the *templates* actually render. The fee and
#: the treat are named in the bot's own cards and in the docs rather than on a
#: page, so asserting them here would be asserting that a page says something it
#: has never said — which is how a law starts failing for the wrong reason. The
#: bank is the one the site draws, on the market page and the economy panel, so it
#: is the one this file can hold.
RENDERED_VOCABULARY = ("DaCat Bank",)

#: The older spelling of that same vocabulary, which the rename finished. Named
#: as a list rather than left to the one retired *brand* word above, because the
#: failure this catches is a surface somebody forgot rather than a page somebody
#: wrote: the vocabulary lived in six modules, two templates and one bundle, and
#: "the brand is gone" was true for four rounds while all of them still said it.
RETIRED_VOCABULARY = ("Meow Bank", "Meow Fee", "Meow Treat")

#: Where those words were authored, and therefore where their absence is checked:
#: the markup a browser is served, plus every module that authors a card, a page
#: or a chart a person reads. Not the whole tree — the *identifiers* keep the
#: spelling and are the reason this list is explicit.
RENDERED_VOCABULARY_SOURCES = (
    "cogs/economy.py",
    "cogs/help.py",
    "cogs/market.py",
    "keep_alive.py",
    "static/market.js",
    "utils/chart.py",
    "utils/market.py",
    "utils/meowfee.py",
    "utils/operator_log.py",
)

#: The retired brand name that should no longer appear on rendered pages.
RETIRED_WORD = re.compile(r"Keystone")


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

    def test_the_display_name_has_one_author_and_it_is_this_module(self):
        """`_bot_display_name` answers with the brand, and stops there.

        It used to defer to the live Discord application's name. That reads well
        and fails silently: this product runs as a **verified** application, and
        Discord refuses a rename from the API with
        ``APPLICATION_VERIFIED_SUPPORT_REQUIRED``, so the field went on holding
        the retired name and four backend-rendered pages announced it while the
        wordmark beside them said the new one. The read is what has to stay gone,
        so this asks the source — the source is the half that cannot be renamed.

        The fallback is also what a page shows before the gateway connects, which
        on a host that sleeps is the first paint of every cold visit, and it was
        spelled out in the middle of a ten-thousand-line file where nobody
        rebranding would think to look.
        """
        source = read(KEEP_ALIVE)
        match = re.search(r"\ndef _bot_display_name\(\) -> str:.*?(?=\ndef |\nclass )", source, re.DOTALL)
        self.assertIsNotNone(match, "keep_alive has no _bot_display_name()")
        body = match.group(0)
        self.assertIn(
            "return brand.NAME",
            body,
            "the display name no longer comes from the brand module",
        )
        self.assertNotIn(
            "BOT_INSTANCE",
            body,
            "the display name reads the live application's name again — that is "
            "the record a verified application cannot rename",
        )
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
            "the rebrand:\n" + "\n".join(offenders),
        )

    def test_no_rendered_source_still_says_the_old_vocabulary(self):
        """The half of a rename that a *word* check cannot see.

        `RETIRED_WORD` above is the brand, and it was already gone while six
        modules still said "Meow Bank" on every economy card — because a rename
        that is only asserted against one word passes on the day somebody chose
        a second one. This is the other word, checked where it is authored.
        """
        offenders = []
        for name in RENDERED_VOCABULARY_SOURCES:
            for number, line in enumerate(read(ROOT / name).splitlines(), start=1):
                said = [word for word in RETIRED_VOCABULARY if word in line]
                if said:
                    offenders.append(f"{name}:{number}: {line.strip()[:90]}")
        self.assertEqual(
            offenders,
            [],
            "these surfaces a person reads still carry the vocabulary's older "
            "spelling; the identifiers keep it on purpose, the words do not:\n"
            + "\n".join(offenders),
        )

    def test_the_kept_vocabulary_is_actually_kept(self):
        """So the allow-list above cannot quietly become a to-do list.

        The vocabulary moves with the brand and keeps its three product words:
        a bank, a fee and a treat. A law that only forbids the older spelling
        would pass just as well if somebody had gone through the economy deleting
        the bank instead of renaming it — which is the change this file exists to
        make deliberate rather than incidental.
        """
        rendered = "\n".join(read(path) for path in TEMPLATES.rglob("*.html"))
        # Collected and compared as a list rather than asserted one at a time:
        # `assertIn` prints its whole container on failure, which for a join of
        # every template in the tree is a screenful of markup and no answer.
        missing = [phrase for phrase in RENDERED_VOCABULARY if phrase not in rendered]
        self.assertEqual(
            missing,
            [],
            "these in-product names have gone from the markup; the vocabulary moves "
            "with the brand, so vanishing is not how a rename lands here",
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


class TheHubWearsTheSameAccent(unittest.TestCase):
    """The hub has a palette of its own, and it was a different colour.

    `hub/static/hub.css` is deliberately self-contained — the hub is a separate
    Pages project, it has no access to `static/meow.css`, and a law in
    `tests/test_hub.py` asserts it declares its own palette rather than reading
    one. That arrangement is right, and it is also exactly how **the fourth host
    of four kept a violet accent** (`#8b7bff`) through the whole rebrand: two
    files declaring one brand, and nothing anywhere saying they were the same
    fact. A visitor to the developer hub got a periwinkle button under a teal
    product, and every test in this repository passed.

    So this law is the comparison that was missing. It reads the hub's *dark*
    accent from the palette module — one colour, one author — and its *light*
    accent from the light half of the site's own sheet, because the hub paints
    text in `--accent` where the site keeps a second, darker `--accent-text` for
    that, so the hub's light value has to be the site's light text shade.
    """

    def setUp(self):
        self.css = read(ROOT / "hub" / "static" / "hub.css")

    def test_the_hubs_dark_palette_is_the_modules_accent(self):
        root = re.search(r"^:root\s*\{(.*?)\n\}", self.css, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(root, "hub.css declares no :root palette")
        block = root.group(1)
        self.assertIn(
            f"--accent: {brand.ACCENT_HEX};",
            block,
            "the hub's dark palette does not paint the brand's accent",
        )
        self.assertIn(
            f"--accent-2: {brand.ACCENT_TEXT_HEX};",
            block,
            "the hub's second gradient stop is not in the brand's family",
        )

    def test_the_hubs_light_palette_is_the_sites_light_text_shade(self):
        light = re.search(r'\[data-theme="light"\]\s*\{(.*?)\n\}', read(SHEET), re.DOTALL)
        self.assertIsNotNone(light, "the site declares no light palette")
        tokens = dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", light.group(1)))
        shade = tokens["accent-text"].strip()
        # Two blocks — the explicit theme and the `prefers-color-scheme` copy that
        # keeps a visitor who has chosen nothing from getting the dark one.
        self.assertEqual(
            self.css.count(f"--accent: {shade};"),
            2,
            "the hub's light palettes do not use the shade the site uses for accent "
            "text on a light ground, or one of the two blocks was missed",
        )

    def test_the_retired_violet_is_gone(self):
        """The values this file shipped before, so the fix cannot half-land.

        Read out of the **declarations**, with the comments stripped: the accent
        was not only in the token block — the page's ambient glow, which is the
        largest colour on any page of the hub, spelled the same violet as three
        `rgba()` channels, and a fix that moved the tokens alone would have left a
        violet wash behind a teal button. A comment is allowed to name the value
        it replaced; a declaration is not.
        """
        declarations = re.sub(r"/\*.*?\*/", "", self.css, flags=re.DOTALL)
        for retired in ("#8b7bff", "#5b48e0", "139, 123, 255", "91, 72, 224"):
            with self.subTest(value=retired):
                self.assertNotIn(
                    retired, declarations, "the hub still carries its old accent"
                )

    def test_the_hub_404_is_not_the_old_name(self):
        """The one hand-written page of that host, and it read *“Keystone Hub”*."""
        builder = read(ROOT / "scripts" / "build_hub.py")
        self.assertNotIn("Keystone Hub", builder)
        self.assertIn("{hub_name}", builder, "the 404 title stopped being read from the brand")
        self.assertNotIn("Keystone Hub", read(ROOT / "hub" / "static" / "hub.js"))


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
        for name in brand.MARKS + brand.FAVICONS:
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


class TheSpaceWearsTheBrand(unittest.TestCase):
    """Two more surfaces a person reads: the Space card and the Space's own page.

    Both live outside the four Cloudflare hosts, and both were missed by the
    rebrand for the same reason: one is YAML front matter that huggingface.co
    reads rather than markup a template renders (it said `title: Meow`, a paw and
    an **indigo→purple** gradient), and the other is a Gradio block whose copy sat
    in `app.py` — a file no test had ever read. A visitor arriving at the host
    that actually runs the bot was greeted by the old name, in the old accent,
    with three unlabelled widgets and no way to reach anything.
    """

    def test_the_space_card_names_this_product(self):
        front = read(ROOT / "README.md").split("---")[1]
        self.assertRegex(front, rf"(?m)^title:\s*{brand.NAME}\s*$")
        self.assertNotRegex(
            front,
            r"(?m)^(title|emoji|colorFrom|colorTo):.*(Keystone|💠)",
            "the Space card still advertises the name, the mark or the palette that "
            "came before this one",
        )

    def test_the_answer_a_release_is_checked_against_is_derived_too(self):
        """``/health`` is the one page ``scripts/ship.py`` can hold the Space to.

        The machine carries no commit stamp, so the release check compares the
        name it answers with instead — which means an answer typed here and a
        check written there are two declarations of one fact, exactly the shape
        this module exists to remove. Derived, the two move together; a rename
        that misses one is a release that fails for the right reason.
        """
        self.assertEqual(brand.GATEWAY_SERVICE, f"{brand.NAME.lower()}-gateway")
        # Read as lines rather than as a file, because this fact is written in a
        # comment too: a check that the *text* names the expression passes on the
        # sentence explaining it, which is how a law stops measuring anything.
        served = [
            line for line in read(ROOT / "app.py").splitlines() if '"service"' in line
        ]
        self.assertTrue(served, "app.py no longer answers /health with a service name")
        for line in served:
            self.assertIn(
                "brand.GATEWAY_SERVICE",
                line,
                "the Space's /health spells the name instead of deriving it: " + line.strip(),
            )

    def test_the_spaces_own_page_is_drawn_from_the_brand(self):
        app = read(ROOT / "app.py")
        self.assertIn("from utils import brand", app, "the space page restates the name")
        self.assertIn("brand.NAME", app)
        self.assertNotIn("Meow", app, "the space still announces the retired name")
        # The ZeroGPU contract, which is the reason a GPU-decorated function has
        # to exist at all — and the reason its label may not read like a feature.
        self.assertIn("@spaces.GPU", app)
        self.assertIn("btn.click(fn=_zero_gpu_worker", app)
        self.assertIn("load check", app, "the GPU box is presented as a feature")


class TheCopyrightIsOneFact(unittest.TestCase):
    """The licence, the site's footer and the hub's all name one holder.

    Asked for as "add copyright my legal things", and the half worth a test is
    not the sentence — it is that five builds print the *same* holder and year as
    the licence they ship under. A year typed into a footer is a footer that is
    wrong twelve months later on four hosts nobody re-reads.
    """

    def test_the_licence_and_the_brand_agree(self):
        licence = read(ROOT / "LICENSE")
        self.assertIn(brand.COPYRIGHT_HOLDER, licence)
        self.assertIn(str(brand.COPYRIGHT_YEAR), licence)

    def test_every_footer_prints_the_modules_own_facts(self):
        for name in ("site_base.html", "hub/base.html"):
            target = TEMPLATES / name
            if not target.exists():
                continue
            with self.subTest(template=name):
                body = read(target)
                self.assertIn("{{ copyright_holder }}", body)
                self.assertIn("{{ copyright_year }}", body)

    def test_the_views_carry_them_rather_than_the_templates_knowing_them(self):
        import keep_alive

        self.assertEqual(keep_alive._site_context()["copyright_holder"], brand.COPYRIGHT_HOLDER)
        self.assertEqual(keep_alive._hub_context("overview")["copyright_year"], brand.COPYRIGHT_YEAR)


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
