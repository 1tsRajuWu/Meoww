"""The marketing site, rebuilt: eight pages, one frame, and one way out of the BRB screen.

Everything here is new — the eight templates, the frame they extend, and the two
assets that dress them (`static/site.css`, `static/site.js`) — so these are the
laws for a surface that had none. Three groups:

  1. **The eight pages** answer, carry the frame, and each has exactly one job:
     the front door claims, `/systems` lists, `/start` explains, `/pricing`
     prices, `/premium` shows the VIP edition, and the three policy pages print
     their document and nothing else.
  2. **The frame's own rules**, which it keeps for six pages it did not rebuild
     (`login`, `servers`, `agree`, `error`, `owner`, `owner_login` — all of which
     still extend `site_base.html`): the load order in the head, the capped nav
     with the action last, the cream of the footer, and *nothing inline* — no
     `<style>`, no `style=` attribute, no handler — because every rule belongs in
     a sheet and every listener in `site.js`.
  3. **The maintenance screen is still the default.** `MEOW_SITE_PREVIEW` is the
     only way past the BRB hook, nothing in a deploy path sets it, and with it
     unset the hook answers exactly as it did before this rebuild. That law sits
     here, beside the pages it hides, so the pair can never drift apart.
"""
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

import keep_alive
from utils import config_audit, figures

ROOT = Path(__file__).resolve().parent.parent

#: The site's public pages, in the order the sitemap lists them.
ROUTES = ("/", "/systems", "/start", "/pricing", "/premium", "/privacy", "/terms", "/ai")

#: What each page exists for, as the marker that proves it. One job per page is
#: the whole point of the multi-page build: no page carries every section, and
#: the marker is what would go missing first if it started to.
JOBS = {
    "/": ("site-hero", "site-marquee", 'data-figure="servers"'),
    "/systems": ("systems-list", "site-systems", "site-details"),
    "/start": ("site-steps", "site-details", "Does the bot need administrator permission?"),
    "/pricing": ("site-plan",),
    "/premium": ("site-mock", "Spotify Music Engine"),
    "/privacy": ("prose", "site-legal__meta"),
    "/terms": ("prose", "site-legal__meta"),
    "/ai": ("prose", "site-legal__meta"),
}


class TheEightPagesAnswer(unittest.TestCase):
    """One build-shaped request per route, read the way a visitor's browser would."""

    @classmethod
    def setUpClass(cls):
        # The BRB hook answers any process that is not a test with the
        # maintenance screen — this class is about the pages *underneath* it, so
        # the hook is stepped around for the life of the class with the same flag
        # `scripts/preview_site.py` sets for a laptop. Scoped, never left behind.
        cls._preview = mock.patch.dict(
            os.environ,
            {"MEOW_SITE_PREVIEW": "1", "DISCORD_CLIENT_ID": "1548380269776605324"},
        )
        cls._preview.start()
        client = keep_alive.app.test_client()
        cls.responses = {route: client.get(route) for route in ROUTES}

    @classmethod
    def tearDownClass(cls):
        cls._preview.stop()

    def _body(self, route: str) -> str:
        response = self.responses[route]
        self.assertEqual(response.status_code, 200, f"{route} did not answer")
        return response.get_data(as_text=True)

    def test_every_route_answers_with_the_page_not_the_maintenance_screen(self):
        for route in ROUTES:
            with self.subTest(route=route):
                body = self._body(route)
                self.assertIn('class="site-shell"', body, route)
                self.assertNotIn("Building The Website", body, route)

    def test_every_page_has_one_job_and_the_marker_to_prove_it(self):
        for route, markers in JOBS.items():
            body = self._body(route)
            for marker in markers:
                with self.subTest(route=route, marker=marker):
                    self.assertIn(marker, body)

    def test_the_frame_loads_theme_first_then_the_shared_sheet_then_this_layer(self):
        """The head's order is load-bearing, so it is asserted rather than trusted."""
        body = self._body("/")
        theme = body.index("/static/theme.js?v=")
        meow = body.index("/static/meow.css?v=")
        site = body.index("/static/site.css?v=")
        script = body.index("/static/site.js?v=")
        self.assertLess(theme, meow, "theme.js must apply the palette before the sheets")
        self.assertLess(meow, site, "the marketing layer is written against meow.css")
        self.assertLess(site, script, "the script is the last thing the page needs")
        # The theme script runs before the first paint — deferred, a visitor who
        # chose light would watch the dark one arrive and be replaced.
        theme_tag = body[body.rindex("<script", 0, theme):body.index(">", theme)]
        self.assertNotIn("defer", theme_tag, "theme.js may not be deferred")
        self.assertIn(f"/static/site.css?v={keep_alive.asset_version('site.css')}", body)
        self.assertIn(f"/static/site.js?v={keep_alive.asset_version('site.js')}", body)

    def test_the_nav_is_capped_and_the_action_comes_last(self):
        body = self._body("/")
        links = list(re.finditer(r'class="site-nav__link"', body))
        # Three destinations while nothing is for sale (Home, Systems, Get
        # started); Pricing joins them only when `selling` is on, which
        # `test_selling_surface` drives from the environment.
        self.assertEqual(len(links), 3, "the nav cap moved")
        action = body.index("btn btn--primary")
        self.assertGreater(action, links[-1].start(), "the action must come after the list")

    def test_every_page_carries_the_trap_and_the_footer_facts(self):
        for route in ROUTES:
            with self.subTest(route=route):
                body = self._body(route)
                self.assertIn('class="trap"', body, "the crawler decoys left the footer")
                self.assertIn("Not affiliated with Discord Inc.", body)
                self.assertRegex(
                    body,
                    r"Messages are kept \d+ days|Nothing is kept longer than it is needed",
                    "the footer stopped stating the retention window",
                )

    def test_nothing_is_inline_on_a_marketing_page(self):
        """No `<style>`, no `style=` attribute, no handler, no `javascript:`.

        A rule written inline is a rule `site.css` cannot restyle and a review
        cannot find; a handler written inline is the one thing the CSP's
        `'unsafe-inline'` would let through. Everything this site animates is a
        `data-` attribute that `site.js` reads, and everything it paints is a
        class.
        """
        for route in ROUTES:
            body = self._body(route)
            with self.subTest(route=route):
                self.assertNotIn("<style>", body)
                self.assertNotIn(' style="', body)
                self.assertIsNone(
                    re.search(r"(?<![-\w])on[a-z]+\s*=", body),
                    "an inline event handler arrived",
                )
                self.assertNotIn("javascript:", body)

    def test_the_figure_cells_carry_a_key_and_never_a_zeroed_dash(self):
        body = self._body("/systems")
        keys = re.findall(r'data-figure="([^"]+)"', body)
        self.assertTrue(keys, "the figure cells lost their keys")
        contract = re.compile(r"^(servers|members|systems|commands|activity:[a-z]+:[a-z_]+)$")
        for key in keys:
            self.assertRegex(key, contract, "a figure cell carries a key /api/figures cannot answer")
        for cell in re.findall(r"<b data-figure=\"[^\"]*\"[^>]*>—</b>", body):
            self.assertNotIn("data-count", cell, "an em dash was handed to the counter")
        self.assertNotIn('data-count="—"', body)
        self.assertNotIn('data-count=""', body)

    def test_every_activity_window_is_in_the_markup(self):
        """The switcher is a redraw, so every window has to be in the document."""
        body = self._body("/systems")
        periods = [pid for pid, _label, _days in figures.PERIODS]
        for period in periods:
            with self.subTest(period=period):
                self.assertEqual(body.count(f'data-window="{period}"'), 1)
                self.assertEqual(body.count(f'data-window-panel="{period}"'), 1)
        panels = re.findall(r'<div data-window-panel="([^"]+)"[^>]*>', body)
        self.assertEqual(len(panels), len(periods))
        visible = [
            match.group(1)
            for match in re.finditer(r'<div data-window-panel="([^"]+)"[^>]*>', body)
            if "hidden" not in match.group(0)
        ]
        self.assertEqual(len(visible), 1, "exactly one window opens with no script")
        self.assertEqual(visible[0], "week", "the week window is the one the page opens on")

    def test_the_six_pages_that_were_not_rebuilt_still_wear_the_frame(self):
        """The frame's second job: the pages this rebuild did not touch.

        `login`, `error`, `servers`, `agree`, `owner` and `owner_login` extend
        `site_base.html` and were deliberately left alone — so the frame they
        inherit must still be there for them. Asserted on the two of the six a
        stranger can reach without signing in.
        """
        client = keep_alive.app.test_client()
        login = client.get("/login").get_data(as_text=True)
        self.assertIn('class="site-shell"', login)
        self.assertIn('class="auth"', login, "the sign-in card lost its interior sheet's class")
        missing = client.get("/nothing-here")
        self.assertEqual(missing.status_code, 404)
        self.assertIn('class="site-shell"', missing.get_data(as_text=True))


class TheNewAssetsKeepTheHousesRules(unittest.TestCase):
    """`site.css` and `site.js` are new, and the rules they inherit are old."""

    def setUp(self):
        self.css = (ROOT / "static" / "site.css").read_text(encoding="utf-8")
        self.js = (ROOT / "static" / "site.js").read_text(encoding="utf-8")

    def test_the_sheet_dials_out_nothing_and_overrides_nothing(self):
        self.assertNotIn("url(", self.css, "a remote face or image would be a new CSP hole")
        self.assertNotIn("!important", self.css, "a layer that has to shout is a layer at war")
        self.assertNotIn("http://", self.css)
        self.assertNotIn("https://", self.css)

    def test_the_sheet_speaks_for_every_palette_the_frame_offers(self):
        for theme in ('[data-theme="light"]', '[data-theme="contrast"]'):
            with self.subTest(theme=theme):
                self.assertIn(theme, self.css, "a palette the theme picker offers is unpainted")

    def test_the_sheet_has_its_own_off_switch_and_a_soft_transition(self):
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertIn("@view-transition", self.css, "cross-page fades are progressive: keep them")
        # The marquee and the drifting mesh are the two pieces whose *end state*
        # still matters under reduced motion, so they are named there.
        reduced = self.css.split("@media (prefers-reduced-motion: reduce)")[1]
        self.assertIn(".site-marquee__track", reduced)
        self.assertIn(".site-bg__mesh", reduced)

    def test_the_script_is_self_contained_and_quiet(self):
        self.assertIn("use strict", self.js)
        self.assertNotIn("http://", self.js)
        self.assertNotIn("https://", self.js)
        self.assertIn("prefers-reduced-motion", self.js, "every effect checks the setting")

    def test_the_script_hands_its_elements_back_to_their_stylesheet(self):
        """An inline transform outranks every rule for the life of the page.

        The same law `motion.js` records: a reveal or a magnet that leaves its
        transform behind removes behaviours it never touched — a button's press
        state, a hover lift — so the resting state is `removeProperty`, not a
        zeroed value.
        """
        self.assertGreaterEqual(self.js.count('removeProperty("transform")'), 2)

    def test_the_loop_is_one_and_it_is_passive(self):
        self.assertIn("function schedule()", self.js)
        self.assertRegex(
            self.js,
            r"""addEventListener\(\s*['"]scroll['"]\s*,\s*schedule""",
            "the scroll listener is not the one shared schedule",
        )
        self.assertIn("{ passive: true }", self.js)
        self.assertLess(
            self.js.count("requestAnimationFrame"),
            6,
            "a page that schedules a loop per effect janks on the device least able to afford it",
        )


class TheMaintenanceScreenIsStillTheDefault(unittest.TestCase):
    """The BRB screen the rebuild was told to keep — pinned beside the pages it hides."""

    def test_the_preview_flag_is_off_unless_somebody_turned_it_on(self):
        with mock.patch.dict(os.environ) as env:
            env.pop("MEOW_SITE_PREVIEW", None)
            self.assertFalse(config_audit.flag("MEOW_SITE_PREVIEW"))

    def test_the_hook_answers_with_the_maintenance_screen_when_the_flag_is_unset(self):
        """The same shape `test_dacat_under_construction_e2e` pins, run once here.

        With `pytest` gone from `sys.modules` *and* the preview flag unset, `/`
        is answered by the BRB page — so the rebuild has not opened a second way
        past the screen, and the one way it does have is a test-only flag no
        deploy sets (the next test).
        """
        with mock.patch.dict(os.environ) as env:
            env.pop("MEOW_SITE_PREVIEW", None)
            with keep_alive.app.test_request_context("/"):
                original = sys.modules.pop("pytest", None)
                try:
                    response = keep_alive._enforce_be_right_back()
                finally:
                    if original is not None:
                        sys.modules["pytest"] = original
        self.assertIsNotNone(response, "the BRB hook stopped intercepting /")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Building The Website", response.get_data(as_text=True))

    def test_no_deploy_path_can_set_the_bypass(self):
        """The flag exists for a laptop. A deploy that could set it would be a
        one-line way to take the maintenance screen down by accident, so the
        paths that publish are read and the two readers of the flag are named.
        """
        for path in (
            ROOT / "scripts" / "ship.py",
            ROOT / ".github" / "workflows" / "site.yml",
            ROOT / "render.yaml",
            ROOT / "Dockerfile",
            ROOT / "Procfile",
        ):
            if not path.is_file():
                continue
            with self.subTest(path=path.name):
                self.assertNotIn(
                    "MEOW_SITE_PREVIEW",
                    path.read_text(encoding="utf-8"),
                    f"{path.name} mentions the maintenance bypass",
                )
        for path in (
            ROOT / "scripts" / "preview_site.py",
            ROOT / "scripts" / "build_site.py",
        ):
            with self.subTest(path=path.name):
                self.assertIn("MEOW_SITE_PREVIEW", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
