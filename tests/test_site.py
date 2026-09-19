"""The front door and the two policies, as the web sees them.

`/` used to redirect, so the first thing a visitor met was a password box for a
panel they had never heard of. What replaced it is a page built from the running
process's own figures, the feature registry and the command tree — so the tests
here are about *honesty* first (no invented number, no advertised system the bot
does not have, no claim on a page that the code contradicts) and about the
renderer that puts the policy files on the web second, because a renderer that
drops half a sentence from a policy is worse than no page at all.
"""
import os
import unittest
from pathlib import Path
from unittest import mock

import keep_alive
from tests.test_dashboard import CSRF, GUILD_ID, _DashboardFixture, make_bot
from utils import webdocs

ROOT = Path(__file__).resolve().parent.parent
PAGES = ("home.html", "legal.html", "login.html", "servers.html", "dashboard.html", "market_live.html", "pricing.html")


class TheRenderer(unittest.TestCase):
    """The subset of markdown the policies use — and nothing else."""

    def test_a_bullet_keeps_its_sentence_when_the_source_wraps(self):
        """The bug this exists for.

        The documents wrap at 80 columns, so `* **Identifiers:** … (a message` /
        `  ID only where a feature anchors to one).` is the normal shape. The
        continuation used to close the list and print as a paragraph, so the
        Privacy page showed "… (a message" as a bullet and "ID only where a
        feature anchors to one)." as body text.
        """
        html = webdocs.render(
            "* **Identifiers:** Discord user and channel IDs (a message\n"
            "  ID only where a feature anchors to one).\n"
            "\n"
            "A paragraph after the list.\n"
        )
        self.assertIn("<li><strong>Identifiers:</strong>", html)
        self.assertIn("(a message ID only where a feature anchors to one).</li>", html)
        self.assertIn("<p>A paragraph after the list.</p>", html)
        self.assertNotIn("one).</p>", html, "the continuation left its bullet")

    def test_a_numbered_list_is_a_numbered_list(self):
        html = webdocs.render("1. first\n2. second\n")
        self.assertIn("<ol>", html)
        self.assertIn("<li>first</li>", html)
        self.assertIn("<li>second</li>", html)

    def test_the_documents_own_title_is_the_pages_h1(self):
        text = "# Privacy Policy\n\n**Effective Date:** today\n"
        self.assertEqual(webdocs.title_from(text, "fallback"), "Privacy Policy")
        self.assertNotIn("<h1", webdocs.render(text), "the template owns the h1")
        self.assertIn("<h2>", webdocs.render("## A section\n"))

    def test_a_tag_in_the_source_is_text_not_a_tag(self):
        html = webdocs.render("Hello <script>alert(1)</script> there\n")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_a_javascript_link_is_not_a_link(self):
        html = webdocs.render("See [this](javascript:alert(1)) and [that](https://example.test/x)\n")
        self.assertNotIn('href="javascript:', html)
        self.assertIn('href="https://example.test/x"', html)
        self.assertIn("javascript:alert(1)", html, "the reader still sees what was written")

    def test_a_link_cannot_open_a_new_tab_without_noreferrer(self):
        html = webdocs.render("[x](https://example.test)\n")
        self.assertIn('rel="noopener noreferrer"', html)

    def test_emphasis_and_code(self):
        html = webdocs.render("**bold** and `code` and *italic*\n")
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<code>code</code>", html)
        self.assertIn("<em>italic</em>", html)

    def test_nothing_is_silently_dropped(self):
        """Every line of the real documents arrives, in one form or another."""
        for name in ("PRIVACY.md", "TERMS.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            html = webdocs.render(text)
            headings = [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]
            self.assertGreater(len(headings), 3, name)
            for heading in headings:
                self.assertIn(heading.replace("&", "&amp;"), html, f"{name}: {heading} went missing")


class TheLandingPage(unittest.TestCase):
    """/ answers a monitor with one line and a person with a page."""

    def setUp(self):
        self.bot, _guild, _other = make_bot()
        self._patched = mock.patch.object(keep_alive, "BOT_INSTANCE", self.bot)
        self._patched.start()
        self.client = keep_alive.app.test_client()

    def tearDown(self):
        self._patched.stop()

    def test_a_monitor_still_gets_the_one_line_it_parses(self):
        for agent in ("UptimeRobot/2.0", "curl/8.4.0"):
            body = self.client.get("/", headers={"User-Agent": agent}).get_data(as_text=True)
            self.assertEqual(body, "Meow Bot is alive and operational!", agent)

    def test_a_visitor_gets_the_page_not_a_redirect(self):
        response = self.client.get("/", headers={"User-Agent": "Mozilla/5.0"})
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        for fragment in (
            "Meow Bot",                 # the name, from the running process
            "systems",                  # how many there are
            'href="/login"',            # a way in
            "/privacy", "/terms",       # the policies
            'class="landing"',          # which surface this page is
            "/static/meow.css",         # the shared design system
        ):
            self.assertIn(fragment, body, fragment)

    def test_the_invite_is_offered_only_when_it_can_work(self):
        """An invite URL needs a client id — without one the button could only
        fail, so it is not rendered at all."""
        with mock.patch.dict(os.environ, {}, clear=True):
            without = self.client.get("/", headers={"User-Agent": "Mozilla/5.0"}).get_data(as_text=True)
        self.assertNotIn("discord.com/oauth2", without)
        with mock.patch.dict(
            os.environ,
            {"DISCORD_CLIENT_ID": "1234567890", "DASHBOARD_BASE_URL": "https://dash.test"},
        ):
            with_credentials = self.client.get("/", headers={"User-Agent": "Mozilla/5.0"}).get_data(as_text=True)
        self.assertIn("discord.com/oauth2/authorize", with_credentials)
        self.assertIn("applications.commands", with_credentials, "an invite without it yields no commands")

    def test_a_signed_in_session_is_sent_to_the_dashboard(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["auth_scope"] = "global"
        response = self.client.get("/", headers={"User-Agent": "Mozilla/5.0"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard", response.headers["Location"])

    def test_the_systems_on_the_page_are_the_registrys_own_list(self):
        """A hand-written copy is what let a removed module keep showing."""
        from utils import features

        body = self.client.get("/").get_data(as_text=True)
        for module in features.dashboard_modules():
            # Rendered, so "&" arrives as the entity.
            self.assertIn(module["name"].replace("&", "&amp;"), body, module["id"])

    def test_no_figure_is_invented_while_the_bot_is_asleep(self):
        """A cold host must not render a confident "0 servers"."""
        asleep = mock.Mock()
        asleep._has_started = False
        asleep.is_ready.return_value = False
        asleep.guilds = []
        with mock.patch.object(keep_alive, "BOT_INSTANCE", asleep):
            body = self.client.get("/").get_data(as_text=True)
        self.assertIn("Starting up", body)
        self.assertNotIn("servers</span>", body, "a sleeping host counted nobody's servers")
        self.assertNotIn("members reached", body)
        # What it does know without the gateway is still on the page.
        self.assertIn("systems</span>", body)

    def test_the_status_line_tells_the_three_states_apart(self):
        connected = mock.Mock()
        connected._has_started = True
        connected.is_ready.return_value = True
        connected.guilds = []
        connected.user.name = "Meow"
        with mock.patch.object(keep_alive, "BOT_INSTANCE", connected):
            self.assertIn("Online", self.client.get("/").get_data(as_text=True))
        connecting = mock.Mock()
        connecting._has_started = True
        connecting.is_ready.return_value = False
        connecting.guilds = []
        with mock.patch.object(keep_alive, "BOT_INSTANCE", connecting):
            self.assertIn("Reconnecting", self.client.get("/").get_data(as_text=True))


class ThePolicyPages(unittest.TestCase):
    def setUp(self):
        self.client = keep_alive.app.test_client()

    def test_both_documents_are_served_in_full(self):
        from cogs import legal

        for path, filename, heading in (
            ("/privacy", "PRIVACY.md", "Privacy Policy"),
            ("/terms", "TERMS.md", "Terms of Service"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            body = response.get_data(as_text=True)
            self.assertIn(heading, body)
            # Every section heading from the file is on the page — the budget that
            # truncates the Discord card does not exist here.
            text = legal.read_document(filename)
            for line in text.splitlines():
                if line.startswith("## "):
                    self.assertIn(line[3:].strip().replace("&", "&amp;"), body, f"{path}: {line}")
            self.assertIn("Effective", body)

    def test_the_effective_date_comes_from_the_document(self):
        body = self.client.get("/privacy").get_data(as_text=True)
        self.assertIn("September 19, 2026", body)
        self.assertNotIn("**", body, "markdown emphasis leaked into the page")

    def test_a_missing_document_is_a_404_and_not_an_empty_page(self):
        with mock.patch("cogs.legal.read_document", return_value=""):
            response = self.client.get("/privacy")
        self.assertEqual(response.status_code, 404)
        self.assertIn("missing from this deploy", response.get_data(as_text=True))


class ThePricingPage(unittest.TestCase):
    def setUp(self):
        self.client = keep_alive.app.test_client()

    def test_pricing_page_renders_tiers_and_options(self):
        response = self.client.get("/pricing")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Free", body)
        self.assertIn("Pro", body)
        self.assertIn("Enterprise", body)
        self.assertIn("/static/meow.css", body)


class TheSiteIsOneDesign(unittest.TestCase):
    """Every page wears the same system, and none of them calls out."""

    def test_every_page_links_the_shared_stylesheet(self):
        """The sheet, and a version token on it.

        Matched as a prefix because the URL now carries `?v=` — and it has to:
        `/static/*` is served `immutable` for a year, so a stylesheet edit with a
        stable URL is a year of invisibility on the CDN. Asserting the bare
        `"` here was what made the version token impossible to add.
        """
        for page in PAGES:
            markup = (ROOT / "templates" / page).read_text(encoding="utf-8")
            self.assertIn(
                'href="/static/meow.css', markup,
                f"{page} does not use the shared design system",
            )
            self.assertIn(
                "asset_version('meow.css')", markup,
                f"{page} links the shared sheet without a version token",
            )

    def test_no_page_loads_a_third_party_except_the_chart_library(self):
        """The CSP allows unpkg/jsdelivr for the market's chart and nothing else.

        This is about what a browser *fetches*, so an outbound link in the page
        (the chart's TradingView attribution, an invite, the repository) is not a
        violation — it costs the visitor nothing until they click it.
        """
        import re

        allowed = ("unpkg.com", "cdn.jsdelivr.net")
        loaders = re.compile(
            r'<script[^>]+src="([^"]+)"'
            r'|<link[^>]+href="([^"]+)"'
            r'|<img[^>]+src="([^"]+)"'
            r"|url\(\s*['\"]?([^)'\"]+)"
        )
        for page in PAGES:
            source = (ROOT / "templates" / page).read_text(encoding="utf-8")
            for match in loaders.finditer(source):
                url = next(group for group in match.groups() if group)
                if not url.startswith(("http://", "https://")):
                    continue  # our own origin, which is what the CSP is for
                host = url.split("/")[2]
                self.assertTrue(
                    host.endswith(allowed),
                    f"{page} loads a third party the CSP would block: {url}",
                )

    def test_the_stylesheet_is_reachable_from_the_app(self):
        client = keep_alive.app.test_client()
        response = client.get("/static/meow.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("--accent", response.get_data(as_text=True))

    def test_a_button_in_the_nav_keeps_a_legible_label(self):
        """Found by looking at the page: the nav's link colour won.

        ``.site-nav a`` (a class *and* a type) outranks ``.btn-primary`` (a
        class), so "Add to Discord" rendered its label in the nav's grey on the
        accent fill — about 2.2:1, on the primary call to action.
        """
        sheet = (ROOT / "static" / "meow.css").read_text(encoding="utf-8")
        self.assertIn(".site-nav a.btn {", sheet)

    def test_motion_is_switched_off_when_it_is_asked_to_be(self):
        sheet = (ROOT / "static" / "meow.css").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion: reduce", sheet)
        # And the entrance must not leave the content invisible once its
        # animation is suppressed to nothing.
        self.assertIn(".reveal {", sheet.split("prefers-reduced-motion: reduce")[1])


class TheHarnessServesTheRealPages(unittest.TestCase):
    """The preview must not restate a page it exists to show.

    Two defects in this file's neighbourhood came from exactly that: a harness
    that describes a route answers differently from the route.
    """

    def test_the_preview_uses_the_real_views(self):
        source = (ROOT / "scripts" / "preview_dashboard.py").read_text(encoding="utf-8")
        for name in ("home", "policy_privacy", "policy_terms", "login"):
            self.assertIn(f"keep_alive.{name}", source, f"the harness restates /{name}")

    def test_the_preview_can_serve_the_shared_stylesheet(self):
        """It could not: the script runs from ``scripts/``, so Flask's default
        static folder was ``scripts/static`` and every page in the preview came
        out unstyled."""
        source = (ROOT / "scripts" / "preview_dashboard.py").read_text(encoding="utf-8")
        self.assertIn('static_folder=os.path.join(ROOT, "static")', source)


if __name__ == "__main__":
    unittest.main()
