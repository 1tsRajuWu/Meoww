"""The landing site — three pages now, and the four things each must keep.

The composition (see `body.landing` in `static/meow.css`) is a hero with a
clip-masked headline, a paper band, and a band of live figures, with
`static/motion.js` doing the reveals. Motion is the part that can quietly become
essential, so these tests are about what has to be true when the script does *not*
run, when the fonts arrive late, and when a page is served from a CDN with a
year-long cache.

**Three pages, so the assertions had to be split as well.** Two of them are about
a *page* — its headline is real text, its figures are a term and a definition, its
links go somewhere — and are checked per file. The rest are about the *site* and
are checked over all three at once, because "the content does not need the script"
is a claim about every page, and a claim checked on one of them is how the other
two drift. The page a given piece of content lives on is asserted where it
matters: the systems list and its demo are on `/systems`, the steps and the ways
in are on `/start`, and neither is on the front door.
"""
import os
import re
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

#: The three pages of the site, and the layout and header they share.
FRONT = ROOT / "templates" / "home.html"
SYSTEMS = ROOT / "templates" / "systems.html"
START = ROOT / "templates" / "start.html"
SITE_PAGES = (FRONT, SYSTEMS, START)
BASE = ROOT / "templates" / "site_base.html"
HEADER = ROOT / "templates" / "site_header.html"

#: The front door, under its old name, for the assertions that are about it
#: specifically rather than about the site.
LANDING = FRONT
MOTION = ROOT / "static" / "motion.js"
SHEET = ROOT / "static" / "meow.css"
#: The landing page's own sheet, which now carries the rules that used to be
#: borrowed from `/systems` — the figures band among them.
LANDING_SHEET = ROOT / "static" / "landing.css"
#: The shared coordinated-motion module the interactive sections drive.
ANIM = ROOT / "static" / "anim.js"
#: The landing page's own behaviour.
LANDING_SCRIPT = ROOT / "static" / "landing.js"


def _all_markup() -> str:
    """Every page and every partial, as one string.

    For the assertions that are about the *site* — a double slash in a command
    name, a third-party URL, a hidden label — 'somewhere' is the right question,
    and asking it per file makes the failure message name the file instead of the
    defect. Assertions that are about one page read that page.
    """
    return "".join(
        path.read_text(encoding="utf-8") for path in (*SITE_PAGES, BASE, HEADER)
    )


class TheContentDoesNotNeedTheScript(unittest.TestCase):
    """A reveal that hides content until JavaScript runs is an outage.

    Every gated element is moved *out* of position by the script rather than into
    it: the markup carries the final state, so a visitor whose browser blocks
    `/static/motion.js` — or whose connection drops it — reads the whole page,
    and so does a crawler.
    """

    def setUp(self):
        self.markup = _all_markup()
        self.script = MOTION.read_text(encoding="utf-8")
        self.sheet = SHEET.read_text(encoding="utf-8")

    def test_the_headline_is_real_text_in_the_markup(self):
        """The words are in the template, not assembled by the script.

        The reveal *splits* them into clip masks at runtime; if it were the thing
        that created them, a blocked script would leave an empty `h1`.
        """
        self.assertIn("Own your server", self.markup)
        self.assertRegex(
            self.markup,
            r'<h1[^>]*data-text-reveal="words"[^>]*>\s*Own your server\s*</h1>',
            "the headline must be the sentence itself, in one element",
        )

    def test_the_page_carries_its_own_status_and_not_a_promise_of_one(self):
        """No script is involved in the state line, and a file is not a process."""
        self.assertIn("{{ status.detail }}", self.markup)
        self.assertNotIn("sleeping free-tier host", self.markup)

    def test_reduced_motion_is_honoured_in_both_places(self):
        """The script *and* the sheet, because either can act first.

        The curtain is dismissed by a CSS animation as well as by the script —
        that is what stops a blocked script stranding a visitor behind it — so
        both have to respect the preference.
        """
        self.assertIn("prefers-reduced-motion", self.script)
        self.assertIn("prefers-reduced-motion", self.sheet)
        self.assertIn("lp-curtain-away", self.sheet)

    def test_nothing_here_loads_a_third_party(self):
        """The CSP allows `'self'` for scripts; the market page is the exception.

        Asserted on the templates rather than on a response because this is about
        what a page *asks for*, and a page that asks is broken everywhere it is
        served, not only where the header is set. Per file, so the failure names
        the page — and so a fourth page cannot be added outside the loop.
        """
        for path in (*SITE_PAGES, BASE, HEADER):
            with self.subTest(page=path.name):
                for match in re.findall(r'(?:src|href)="([^"]+)"', path.read_text(encoding="utf-8")):
                    self.assertFalse(
                        match.startswith(("http://", "https://", "//")),
                        f"{path.name} loads {match} from somewhere else",
                    )


class TheFiguresAreShownOnce(unittest.TestCase):
    """The bug this pins: a live figure rendered as "systems 11 systems".

    The figures are a `<dt>`/`<dd>` pair, and the first version hid the term with
    a `.sr-only` class that this page never had — so the label showed up twice
    on screen for everybody, and the accessible name was doubled for everybody
    using a screen reader. The fix is markup order plus one CSS rule, and the
    thing that makes it invisible is that the page still *looks* plausible.
    """

    def setUp(self):
        self.markup = _all_markup()
        self.sheet = SHEET.read_text(encoding="utf-8")

    def test_each_figure_is_a_term_and_a_definition_with_no_repetition(self):
        self.assertIn("<dt>{{ stat.label }}</dt>", self.markup)
        self.assertIn("<dd>{{ stat.value }}</dd>", self.markup)
        self.assertNotIn("sr-only", self.markup, "the label is hidden again")

    def test_the_figure_is_drawn_above_its_label_by_the_stylesheet(self):
        """`<dt>` must precede its `<dd>`, so the visual order comes from CSS.

        The rule moved with the band: the front door's figures are `.lx-figure`
        in `static/landing.css` now, because the page has a sheet of its own
        rather than borrowing `/systems`' classes. What is being pinned is the
        same and is not about the class name — those two numbers beside each
        other in the markup are the defect, and the stylesheet is the only thing
        allowed to swap them on screen.
        """
        sheet = LANDING_SHEET.read_text(encoding="utf-8")
        stat = sheet.split(".lx-figure {", 1)[1]
        self.assertIn("display: flex", stat)
        self.assertIn("order: -1", sheet.split(".lx-figure dd {", 1)[1])

    def test_the_accessibility_utility_exists_on_the_shared_sheet(self):
        """.sr-only was defined inside `dashboard.html` and nowhere else.

        Which is exactly how the page that needed it next did not have it.
        """
        self.assertIn(".sr-only {", self.sheet)


class AssetsAreCacheSafe(unittest.TestCase):
    """`/static/*` is served `immutable` for a year by the static host.

    That is only safe while the URL changes with the file, and hand-maintained
    `?v=` strings had already drifted: the stylesheet was linked bare, so a CSS
    edit would have been invisible to returning visitors for up to a year while
    looking perfectly correct to whoever deployed it.
    """

    def test_every_page_versions_the_stylesheet(self):
        import keep_alive

        pages = sorted((ROOT / "templates").glob("*.html"))
        self.assertTrue(pages)
        for page in pages:
            text = page.read_text(encoding="utf-8")
            if "/static/meow.css" not in text:
                continue
            self.assertIn(
                "asset_version('meow.css')", text,
                f"{page.name} links the sheet without a token that changes with it",
            )
        self.assertEqual(
            keep_alive.asset_version("meow.css"), keep_alive.asset_version("meow.css"),
            "the token is not stable between renders, so every request misses the cache",
        )

    def test_the_token_changes_when_the_file_does(self):
        import keep_alive

        meow = keep_alive.asset_version("meow.css")
        motion = keep_alive.asset_version("motion.js")
        self.assertNotEqual(meow, motion, "two different files share a token")
        with mock.patch.object(keep_alive.os, "stat") as stat:
            stat.return_value = os.stat_result((0, 0, 0, 0, 0, 0, 999999, 0, 0, 0))
            changed = keep_alive.asset_version("meow.css")
        self.assertNotEqual(changed, meow, "a changed file kept its old token")

    def test_a_missing_asset_is_not_an_exception(self):
        """A template must not 500 because a file was renamed."""
        import keep_alive

        self.assertEqual(keep_alive.asset_version("not-a-real-file.css"), "0")

    def test_the_fit_script_measures_the_content_box(self):
        """The headline is fitted by measurement, not by a `vw` guess.

        The first version fitted it to the *padding* box, so it filled its box and
        still crossed the band's own padding, where `overflow: hidden` cut the
        last letter off — "OWN YOUR SERV". A test rather than a comment because
        the failure looks like a font problem.
        """
        script = MOTION.read_text(encoding="utf-8")
        self.assertIn("paddingLeft", script)
        self.assertIn("paddingRight", script)
        self.assertIn("scrollWidth", script)


class TheTryPanelIsRealAndSaysSo(unittest.TestCase):
    """The one claim on this page a reader cannot check by reading.

    The page says a system switched off disappears from *one server's* command
    picker. That is the product's actual difference from a bot whose commands are
    global, so it ships as something the reader can operate — press a system and
    watch the list change — rather than as another paragraph asserting it.

    What is pinned here is the set of ways that could quietly stop being true:
    the panel is drawn from the registry rather than a hand-written copy, the
    names it prints are the names the bot registers, the payload is escaped so it
    can neither close its own `<script>` nor be mangled by Jinja, and the section
    says nothing before the script runs that the script then contradicts.
    """

    def setUp(self):
        # The systems list and its demo, which is the only place a command name
        # is printed at all — the front door lists system *names* now.
        self.markup = SYSTEMS.read_text(encoding="utf-8")

    def test_a_command_is_printed_with_the_one_slash_it_already_has(self):
        """`/{{ cmd }}` rendered `//rank`, `//balance` and `//daily` on this page.

        Every name in the registry carries its own leading slash, and the
        dashboard's module cards have always rendered `{{ cmd }}` — so the two
        surfaces disagreed and only this one was wrong. It is the kind of defect
        that survives review because a double slash still *reads* as a command.
        """
        import keep_alive

        with mock.patch.object(keep_alive, "BOT_INSTANCE", None):
            commands = [
                command
                for module in keep_alive.AVAILABLE_MODULES
                for command in (module.get("commands") or [])
            ]
        self.assertTrue(commands, "no commands in the registry, so nothing is checked")
        without = [c for c in commands if not str(c).startswith("/")]
        self.assertEqual(without, [], "a name without a slash would now render as /name")

        # And the template must not add one of its own. Matched on the *loop
        # bodies*, because the same page prints a slash in prose elsewhere.
        for body in re.findall(r"\{%\s*for cmd in [^%]*%\}(.*?)\{%\s*endfor\s*%\}", self.markup, re.S):
            with self.subTest(body=body.strip()[:60]):
                self.assertNotIn(">/{{ cmd }}<", body)
                self.assertNotIn("textContent = \"/\"", body)

    def test_the_panel_is_drawn_from_the_registry_not_a_copy_of_it(self):
        self.assertIn("{% for feature in features %}", self.markup)
        self.assertIn('data-system="{{ feature.id }}"', self.markup)
        self.assertIn("try_payload", self.markup)

    def test_it_is_the_systems_page_that_carries_it(self):
        """The split's whole point: the demo is one click from the front door.

        A front door that still rendered the panel would have been moved rather
        than shortened, and the claim it makes is the reason `/systems` exists.
        """
        front = FRONT.read_text(encoding="utf-8")
        self.assertNotIn("data-try", front)
        self.assertNotIn("try_payload", front)
        self.assertIn("/systems", front, "the front door cannot reach it")

    def test_the_payload_cannot_close_its_own_script_element(self):
        """A `</script` inside the data would end the tag and spill into the page."""
        import keep_alive

        payload = keep_alive._try_payload()
        self.assertNotIn("<", payload)
        self.assertNotIn("</script", payload.lower())
        self.assertTrue(payload.startswith("["))

    def test_the_payload_is_json_that_the_page_can_actually_parse(self):
        """Autoescaping turned every quote into `&#34;`, which is valid HTML and
        invalid JSON — so the panel silently fell back to its static state on
        every page while looking perfectly correct in the markup."""
        import json

        import keep_alive
        from flask import render_template

        with keep_alive.app.test_request_context("/systems"):
            rendered = render_template("systems.html", **{**self._minimal_home(), "try_payload": keep_alive._try_payload()})
        found = re.search(
            r'<script type="application/json" data-try-data>(.*?)</script>', rendered, re.S
        )
        self.assertIsNotNone(found, "the payload is not in the rendered page")
        parsed = json.loads(found.group(1))
        self.assertTrue(parsed, "the payload parsed to nothing")
        self.assertIn("id", parsed[0])
        self.assertIn("commands", parsed[0])

    def test_the_sections_static_state_is_the_truthful_one(self):
        """With no script at all it must claim everything is on, not nothing.

        A server nobody has configured runs every system, so that is what the
        markup says. A panel that started from "nothing is on" would be telling a
        reader something false about the product every time the script failed.
        """
        self.assertIn("aria-pressed=\"true\"", self.markup)
        self.assertIn("All {{ features|length }} systems on.", self.markup)

    def test_the_empty_state_is_in_the_markup_rather_than_built_by_script(self):
        self.assertIn('data-try-empty hidden', self.markup)

    def test_it_promises_that_nothing_is_sent(self):
        """A visitor who has not added the bot must not be made to wonder.

        Both halves of the promise, not the sentence that used to carry them: the
        panel's copy was shortened in the same pass as the rest of the page, and a
        test pinned to the old wording asserts a phrase rather than the claim a
        reader has to be able to rely on.
        """
        self.assertIn("Nothing is sent", self.markup)
        self.assertIn("No server is changed", self.markup)

    @staticmethod
    def _minimal_home():
        """The names `systems.html` reaches for that this test does not care about."""
        return {
            "bot_name": "Meow Bot",
            "bot_initial": "M",
            # The three pages share a layout and differ by which nav link is theirs.
            "current": "systems",
            "system_count": 12,
            "command_count": 90,
            "status": {"state": "up", "label": "Up"},
            "stats": [],
            # The two preview cards. Empty here because this test is about the
            # systems panel's payload; the cards have their own test in
            # `tests/test_figures.py`, which renders them with real ones.
            "control_tiles": [],
            "activity_tiles": [],
            "activity_periods": [],
            "system_bars": [],
            "figures_json": "{}",
            "figures_url": "/api/figures",
            "features": [],
            "invite_url": "",
            "invite_limited_url": "",
            "invite_here_url": "",
            "enter_url": "/login",
            "enter_label": "Sign in",
            "retention_days": 30,
            "oauth_enabled": False,
            "price_free": "Free",
            "price_pro": "",
            "price_enterprise": "",
            "selling": False,
            "source_repository": "https://example.test/repo",
            "meta_description": "a bot",
            "asset_version": lambda name: "0",
        }


class TheSiteIsThreePagesAndTheyAllWork(unittest.TestCase):
    """The split itself — and the way it breaks is not a broken page.

    Splitting one document into three does not produce a 404 or a stack trace when
    it goes wrong. It produces a page that *works*, whose navigation points at a
    section that is no longer on it: a fragment cannot be redirected by a server,
    so `#features` on a page without one scrolls nowhere, with no error in any
    console and nothing an automated crawl would notice. Every assertion below is
    one of those quiet failures.
    """

    def setUp(self):
        self.pages = {path.name: path.read_text(encoding="utf-8") for path in SITE_PAGES}
        self.base = BASE.read_text(encoding="utf-8")

    def test_every_page_extends_the_base_and_fills_its_own_title(self):
        for name, markup in self.pages.items():
            with self.subTest(page=name):
                self.assertIn('{% extends "site_base.html" %}', markup)
                self.assertIn("{% block main %}", markup)
                self.assertIn("{% block title %}", markup)

    def test_the_navigation_is_absolute_and_names_the_three_pages(self):
        """Absolute, because a bare `#features` means a different section per page."""
        nav = self.base.split('<nav class="lp-menu-nav"', 1)[1].split("</nav>", 1)[0]
        for href in ("/systems", "/start", "/start#ways-in", "/privacy"):
            self.assertIn(f'href="{href}"', nav, f"the menu has lost {href}")
        for href in re.findall(r'href="([^"]+)"', nav):
            self.assertFalse(href.startswith("#"), f"the shared menu links to {href}")

    def test_the_phone_menu_offers_every_page_the_desktop_row_does(self):
        """Two navigations, one site — and only one of them gets updated.

        The header's row is what a laptop shows; the menu is a phone's *only*
        navigation. Adding a page to one and forgetting the other is invisible
        from the machine this is developed on, which is how `/radio` — the page
        this site is now about — shipped reachable from a desktop and from
        nowhere at all on a phone. Both lists are read as written, because the
        question is only ever whether the markup offers the link.
        """
        header = HEADER.read_text(encoding="utf-8")
        desktop = header.split('<nav class="lp-nav"', 1)[1].split("</nav>", 1)[0]
        menu = self.base.split('<nav class="lp-menu-nav"', 1)[1].split("</nav>", 1)[0]
        wanted = set(re.findall(r'href="(/[^"]*)"', desktop))
        offered = set(re.findall(r'href="([^"]*)"', menu))
        self.assertTrue(wanted, "the desktop row was not found to compare against")
        missing = sorted(wanted - offered)
        self.assertEqual(missing, [], f"the phone menu cannot reach: {missing}")

    def test_the_footer_is_absolute_too(self):
        footer = self.base.split('<footer class="lp-footer', 1)[1]
        for href in ("/systems", "/start"):
            self.assertIn(f'href="{href}"', footer, f"the footer has lost {href}")

    def test_the_front_door_keeps_the_anchors_that_were_already_linked(self):
        """`/#features` and `/#start` are in links that were pasted months ago.

        A fragment is not sent to the server, so no redirect can rescue one — the
        only way an old link still lands somewhere is for that section to still be
        at that address. The full version moved to `/systems` and `/start`; the id
        stayed put, pointing at the teaser that says where it went.
        """
        front = self.pages["home.html"]
        for anchor in ("features", "start", "ways-in", "add"):
            self.assertIn(f'id="{anchor}"', front, f"/#{anchor} now lands nowhere")

    def test_each_page_that_gets_a_fragment_actually_has_it(self):
        """Every cross-page fragment must exist on the page it points at."""
        for name, markup in self.pages.items():
            for target in set(re.findall(r'href="(/[^"]*)#([a-z-]+)"', markup)):
                base, fragment = target
                other = "home.html" if base == "/" else base.strip("/") + ".html"
                with self.subTest(page=name, to=f"{base}#{fragment}"):
                    self.assertIn(other, self.pages, f"{base} is not a page of this site")
                    self.assertIn(f'id="{fragment}"', self.pages[other])

    def test_no_page_declares_the_same_id_twice(self):
        """The defect the dashboard's composer died of, one site over.

        Two elements with one id means `getElementById` hands back whichever came
        first, and every anchor to the second one scrolls to the first. Nothing
        about the markup looks wrong.
        """
        for name, markup in self.pages.items():
            ids = re.findall(r'\bid="([^"]+)"', markup)
            duplicates = {value for value in ids if ids.count(value) > 1}
            self.assertEqual(duplicates, set(), f"{name} declares {sorted(duplicates)} twice")

    def test_the_front_door_is_the_short_of_it(self):
        """What is *not* on it is the point: the detail lives on the other two."""
        front = self.pages["home.html"]
        for detail in ("lp-rows", "lp-cmd-pill", "data-try", "lp-panels", "lp-cards"):
            self.assertNotIn(detail, front, f"the front door still carries {detail}")
        # ...and it says where each of them went.
        self.assertIn("/systems", front)
        self.assertIn("/start", front)

    def test_the_two_counts_about_systems_add_up_and_name_the_difference(self):
        """A total, a shorter list, and the name of what is missing.

        Both pages print *12 systems, 11 of them switchable* and then show eleven
        things — a heading and a list that disagree to anybody who counts, with no
        way to find out what the twelfth is. The difference is the system with no
        toggle, and it is named from the registry rather than typed into a
        template, so the two pages cannot name two different ones.
        """
        import keep_alive
        from utils import features

        always_on = features.always_on_names()
        self.assertTrue(always_on, "the always-on system vanished from the registry")
        self.assertEqual(
            len(features.dashboard_modules()) + len(always_on),
            features.system_count(),
            "the switchable list and the always-on list no longer make the total",
        )
        # Read from the registry rather than typed into a template: a name written
        # down twice is how two pages come to name two different systems.
        for page in ("home.html", "systems.html"):
            self.assertIn("always_on", self.pages[page], f"{page} does not read the registry")
        # ...and rendered, because the name *is* the registry's, so a template in
        # which it appears verbatim would be the defect this is testing for. Only
        # the escaping can be wrong, and only rendering shows that.
        browser = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
        with keep_alive.app.test_client() as client:
            for path in ("/", "/systems"):
                body = client.get(path, headers=browser).get_data(as_text=True)
                with self.subTest(path=path):
                    for name in always_on:
                        self.assertTrue(
                            name in body or name.replace("&", "&amp;") in body,
                            f"{path} prints a total and a shorter list without naming {name}",
                        )

    def test_the_systems_page_carries_the_list_the_demo_and_the_dashboard(self):
        systems = self.pages["systems.html"]
        for markup in ("lp-rows", "data-try", "data-figures-url", "data-bars"):
            self.assertIn(markup, systems, f"/systems has lost {markup}")

    def test_the_start_page_carries_the_steps_the_ways_in_and_the_invite(self):
        start = self.pages["start.html"]
        for markup in ("lp-step-num", 'id="ways-in"', "invite_url", "oauth_enabled"):
            self.assertIn(markup, start, f"/start has lost {markup}")


class TheMenuIsUsableFromTheKeyboard(unittest.TestCase):
    """The bug this pins: an overlay that opens and forgets the caret.

    `.lp-menu` transitions `visibility` over 260ms so that a closing menu stays
    on screen while it fades, and a browser *silently ignores* `focus()` on an
    element that is still hidden. So "call focus once after a frame" did nothing
    at all — and because the panel's header link to the site root comes first in
    the DOM, the version that did work put the caret on a link that navigates
    away, so pressing Enter on a freshly opened menu left the page.

    Asserted on the script because this is timimg inside a browser, which the
    suite does not run; each check is one of the three decisions that were wrong,
    and each one fails *quietly* on screen.
    """

    def setUp(self):
        self.script = MOTION.read_text(encoding="utf-8")

    def test_the_focus_attempt_is_repeated_until_it_takes(self):
        """One frame is not enough while `visibility` is still transitioning."""
        self.assertIn("document.activeElement === first", self.script,
                      "nothing checks whether the focus actually landed")
        self.assertIn("requestAnimationFrame(function () { attempt(n + 1); })",
                      self.script, "the attempt is not retried")

    def test_the_menu_opens_onto_its_own_links_not_the_brand(self):
        """The first focusable element is a link home, which is not the point."""
        grab = self.script.split("function grabFocus()", 1)[1].split("function setMenu", 1)[0]
        self.assertIn('querySelector("nav a")', grab)
        self.assertLess(grab.index('querySelector("nav a")'), grab.index('querySelector("a, button")'),
                        "the brand link would be reached first again")

    def test_escape_only_closes_a_menu_that_is_open(self):
        """An unguarded close steals focus back on any Escape, anywhere."""
        self.assertIn('event.key === "Escape" && menu.classList.contains("is-open")',
                      self.script)


class EveryButtonIsDressedAsOne(unittest.TestCase):
    """The page's own controls must not arrive wearing the browser's chrome.

    `meow.css` styles `.btn` and the shared components and deliberately does not
    reset the `<button>` element, so a control this page gives only a colour, a
    size and a hover to renders with the *user agent's* background behind it —
    `buttonface` grey, the system font, and a 2px black border. Rendered, the
    tour rail and the six questions shipped as pale rows with near-white text on
    them: unreadable, and invisible in the stylesheet, because a rule that sets
    no `background` reads as a finished rule.

    The question below is therefore not "is the reset there" but "is every
    button on this page in it", and the class list is read from the *markup*
    rather than written down here: the way this breaks next is a control added
    below the list, which a test holding its own copy of the six would not
    notice.
    """

    #: The rule, identified by what it does rather than by where it sits: a
    #: block that both removes the background and hands the element the page's
    #: own typography.
    @staticmethod
    def _reset_classes(sheet: str) -> set[str]:
        classes: set[str] = set()
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", sheet):
            if "background: none" not in body or "font: inherit" not in body:
                continue
            # Comments sit between selectors in this file, so they are stripped
            # before the split rather than after it.
            cleaned = re.sub(r"/\*.*?\*/", "", selector, flags=re.S)
            for part in cleaned.split(","):
                part = part.strip()
                if re.fullmatch(r"\.[a-z][a-z0-9-]*", part):
                    classes.add(part)
        return classes

    def test_every_lx_button_class_is_in_the_page_reset(self):
        markup = FRONT.read_text(encoding="utf-8")
        sheet = LANDING_SHEET.read_text(encoding="utf-8")

        used = set()
        for value in re.findall(r'<button[^>]*\bclass="([^"]*)"', markup, re.S):
            used.update(
                token for token in value.split()
                if re.fullmatch(r"lx-[a-z][a-z0-9-]*", token)
            )
        self.assertTrue(
            used,
            "no classed buttons were found in the markup, so this check is now "
            "measuring nothing — the regex has drifted from the template",
        )

        reset = self._reset_classes(sheet)
        self.assertTrue(
            reset,
            "the page's button reset is gone or no longer recognisable, so every "
            "control on it is back to the browser's chrome",
        )
        missing = sorted(f".{token}" for token in used if f".{token}" not in reset)
        self.assertEqual(
            missing, [],
            "these classes are worn by a <button> on the front door and are not "
            "in the page's reset, so they render with the browser's own "
            "background and font",
        )

    def test_the_check_would_notice_a_button_left_out(self):
        """A guard that cannot fail is decoration."""
        sheet = """.lx-a, .lx-b { background: none; border: 0; font: inherit; }"""
        self.assertEqual(self._reset_classes(sheet), {".lx-a", ".lx-b"})
        self.assertNotIn(".lx-c", self._reset_classes(sheet))


if __name__ == "__main__":
    unittest.main()
