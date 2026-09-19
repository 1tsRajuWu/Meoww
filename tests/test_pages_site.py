"""The static site built for GitHub Pages.

The bot's own pages are rendered by a Flask app that happens to be running; the
copy published to Pages is a set of files, and every way that difference can bite
is pinned here:

* A **project** site is served from ``/<repo>/``, so a single surviving
  root-relative URL (``/static/meow.css``) is a 404 — and the one that fails
  *silently* is the cat cursor, because a browser that cannot fetch a ``url()``
  cursor just draws the system arrow and says nothing in any console.
* The templates **omit** the invite button when no client id is known, so a
  build without one produces a landing page that cannot add the bot and reports
  no error at all. That is the exact state the live site was in.
* The landing page's status line is a claim about *this process*. On Pages there
  is no process, so "a sleeping free-tier host takes a moment to wake" is a
  promise that never resolves and a visitor reads a permanently half-started bot.
* The pricing page lists prices for things nobody can buy and used to carry two
  dead ``href="#"`` buttons.

Each test below was written against the defect, and the ones about the build's
output would fail on the naive version of it.
"""
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import keep_alive
from scripts import build_site

ROOT = Path(__file__).resolve().parent.parent
LIVE = "https://bot.example.test"
SITE = "https://owner.github.io/meow-discord-bot"


class RewriteUrlCase(unittest.TestCase):
    """The one function every URL in every page goes through."""

    def test_assets_become_relative_to_the_site_root(self):
        self.assertEqual(build_site.rewrite_url("/static/meow.css", LIVE), "static/meow.css")
        # A cache-busting query has to survive, or the deploy serves a stale asset.
        self.assertEqual(
            build_site.rewrite_url("/static/particles.js?v=2.2", LIVE),
            "static/particles.js?v=2.2",
        )

    def test_a_page_that_exists_here_becomes_its_file(self):
        self.assertEqual(build_site.rewrite_url("/pricing", LIVE), "pricing.html")
        self.assertEqual(build_site.rewrite_url("/privacy", LIVE), "privacy.html")
        self.assertEqual(build_site.rewrite_url("/terms", LIVE), "terms.html")
        # The front page is the directory index, so the URL stays short.
        self.assertEqual(build_site.rewrite_url("/", LIVE), "./")
        # An anchor into another page keeps its fragment.
        self.assertEqual(build_site.rewrite_url("/pricing#faq", LIVE), "pricing.html#faq")

    def test_a_page_that_needs_the_backend_goes_to_the_live_app(self):
        for path in ("/dashboard", "/login", "/servers", "/health", "/market/BTC"):
            self.assertEqual(build_site.rewrite_url(path, LIVE), LIVE + path)

    def test_urls_that_must_not_be_touched_are_left_alone(self):
        for url in (
            "https://discord.com/oauth2/authorize?client_id=1",
            "http://plain.example/x",
            "//protocol-relative.example/x",
            "mailto:someone@example.com",
            "data:image/svg+xml,<svg/>",
            "#features",
            "",
            None,
        ):
            self.assertEqual(build_site.rewrite_url(url, LIVE), url)

    def test_a_stylesheet_resolves_its_assets_against_its_own_folder(self):
        """The bug the cursor test above found: one prefix or none.

        ``meow.css`` sits in ``static/``, so ``static/cursor-default.svg``
        inside it resolves to ``static/static/…`` and the cat cursor never
        loads — with the system arrow as the only symptom.
        """
        self.assertEqual(
            build_site.rewrite_url("/static/cursor-default.svg", LIVE, from_stylesheet=True),
            "cursor-default.svg",
        )
        self.assertEqual(
            build_site.rewrite_url("/static/cursor-default.svg", LIVE),
            "static/cursor-default.svg",
        )

    def test_an_already_relative_url_is_not_double_rewritten(self):
        """Building twice must be safe; a relative path is already correct."""
        self.assertEqual(build_site.rewrite_url("static/meow.css", LIVE), "static/meow.css")
        self.assertEqual(build_site.rewrite_url("pricing.html", LIVE), "pricing.html")


class BuiltSiteCase(unittest.TestCase):
    """One real build, inspected the way a visitor's browser would."""

    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.mkdtemp(prefix="meow-site-")
        cls._env = mock.patch.dict(
            os.environ,
            {
                # Set by the real builder; here it is set explicitly because the
                # build is driven directly rather than through ``main``.
                "MEOW_STATIC_SITE": "1",
                "DISCORD_CLIENT_ID": "1548380269776605324",
            },
        )
        cls._env.start()
        cls.report = build_site.build(cls.out, LIVE, SITE)
        cls.pages = {
            name: (Path(cls.out) / name).read_text(encoding="utf-8")
            for name in ("index.html", "pricing.html", "privacy.html", "terms.html")
        }

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()
        shutil.rmtree(cls.out, ignore_errors=True)

    def test_every_page_is_written(self):
        for name in ("index.html", "pricing.html", "privacy.html", "terms.html"):
            self.assertTrue((Path(self.out) / name).is_file(), f"{name} was not built")

    def test_the_thing_github_pages_needs_is_present(self):
        """Jekyll would otherwise process the site — and silently drop underscores."""
        self.assertTrue((Path(self.out) / ".nojekyll").is_file())

    def test_not_one_root_relative_url_survives_anywhere(self):
        """The whole class of 404 this build exists to prevent.

        Checked over the built files rather than over the rewrite function,
        because the failure this catches is a URL that a template emits and the
        rewriter never saw.
        """
        offenders = {}
        for name, text in self.pages.items():
            leftovers = build_site._clean_targets(text, LIVE)
            if leftovers:
                offenders[name] = sorted(set(leftovers))
        css = (Path(self.out) / "static" / "meow.css").read_text(encoding="utf-8")
        leftovers = build_site._clean_targets(css, LIVE)
        if leftovers:
            offenders["meow.css"] = sorted(set(leftovers))
        self.assertEqual(offenders, {}, f"root-relative URLs would 404: {offenders}")

    def test_the_cat_cursor_is_actually_fetchable_from_the_stylesheet(self):
        """The silent one: a missing cursor image is the system arrow.

        The path is relative to the *stylesheet*, so this resolves it the way a
        browser does and insists the file is there.
        """
        css_path = Path(self.out) / "static" / "meow.css"
        css = css_path.read_text(encoding="utf-8")
        cursor_urls = re.findall(r"cursor:\s*url\((['\"]?)([^'\")]+)\1\)", css)
        self.assertTrue(cursor_urls, "the stylesheet no longer styles a cursor")
        for _quote, url in cursor_urls:
            resolved = (css_path.parent / url.split("?")[0]).resolve()
            self.assertTrue(resolved.is_file(), f"{url} does not exist next to the stylesheet")

    def test_the_front_page_can_actually_add_the_bot(self):
        """A landing page with no invite link is the bug this caught live."""
        self.assertIn("discord.com/oauth2/authorize", self.pages["index.html"])
        self.assertIn("client_id=1548380269776605324", self.pages["index.html"])

    def test_no_page_is_a_dead_end(self):
        for name, text in self.pages.items():
            self.assertNotIn('href="#"', text, f"{name} has a link that goes nowhere")

    def test_a_static_copy_does_not_claim_a_process_is_starting(self):
        """There is no process behind a file, so 'starting up' never resolves."""
        self.assertIn("Always on", self.pages["index.html"])
        self.assertNotIn("sleeping free-tier host", self.pages["index.html"])

    def test_the_pricing_page_says_that_nothing_can_be_bought_yet(self):
        pricing = self.pages["pricing.html"]
        self.assertIn("No billing yet", pricing)

    def test_the_sitemap_names_only_pages_that_exist(self):
        sitemap = (Path(self.out) / "sitemap.xml").read_text(encoding="utf-8")
        self.assertIn(SITE, sitemap)
        for name in ("pricing.html", "privacy.html", "terms.html"):
            self.assertIn(name, sitemap)
        # Nothing dynamic may be advertised to a crawler: those URLs are on the
        # live app and a sitemap entry for one would 404 for the crawler.
        for path in ("/dashboard", "/login", "/servers"):
            self.assertNotIn(path, sitemap)


class TheLiveSiteIsNotAffected(unittest.TestCase):
    """The static flag must not change what a running bot says about itself."""

    def test_a_running_process_still_describes_its_own_gateway(self):
        with mock.patch.dict(os.environ, {"MEOW_STATIC_SITE": ""}):
            keep_alive.BOT_INSTANCE = None
            status = keep_alive._home_status()
        self.assertNotIn("Always on", status["label"])
        self.assertIn("not connected yet", status["detail"])

    def test_the_static_flag_changes_only_the_status_line(self):
        with mock.patch.dict(os.environ, {"MEOW_STATIC_SITE": "1"}):
            self.assertTrue(keep_alive._static_site())
        with mock.patch.dict(os.environ, {"MEOW_STATIC_SITE": "0"}):
            self.assertFalse(keep_alive._static_site())


if __name__ == "__main__":
    unittest.main()
