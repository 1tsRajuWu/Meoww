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


class TheStaticCopyIsProtectedLikeTheLiveOne(unittest.TestCase):
    """A CDN has no Flask, so the headers it cannot inherit have to be shipped.

    Every security header this bot serves — CSP, `X-Frame-Options`, the referrer
    policy, HSTS — is set by `keep_alive.set_security_headers`, which runs on the
    Render host and nowhere else. So the copy published to a static host, without
    a `_headers` file, is the *least* protected version of the pages: no CSP, no
    frame denial, no HSTS. These tests exist because that gap is invisible from
    both ends — the Flask tests all pass, and a static host serves it happily.
    """

    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.mkdtemp(prefix="meow-site-hdr-")
        build_site.build(cls.out, "https://bot.example.test")
        cls.headers = (Path(cls.out) / "_headers").read_text(encoding="utf-8")
        cls.redirects = (Path(cls.out) / "_redirects").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def _value(self, name: str) -> str:
        for line in self.headers.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith(name.lower() + ":"):
                return stripped.split(":", 1)[1].strip()
        self.fail(f"_headers does not set {name}")

    def test_it_covers_every_page(self):
        self.assertIn("/*", self.headers)

    def test_it_agrees_with_the_live_host_on_the_headers_that_have_one_answer(self):
        """Two hosts must not disagree about the same protection.

        Read from a real response rather than restated, because a copy of a policy
        is what drifts — the same failure that made the preview harness answer
        differently from the bot.
        """
        live = keep_alive.app.test_client().get("/health").headers
        for name in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"):
            self.assertEqual(
                self._value(name),
                live.get(name),
                f"{name} differs between the static copy and the live host",
            )

    def test_the_policy_forbids_being_framed(self):
        csp = self._value("Content-Security-Policy")
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("default-src 'self'", csp)
        self.assertIn("base-uri 'self'", csp)
        self.assertIn("form-action 'self'", csp)

    def test_the_static_policy_is_not_looser_than_the_live_one(self):
        """It may be narrower — the market page is not built here — never wider.

        The chart library's two CDNs are needed by the live market page and by
        nothing in this build, so allowing them here would be a permission for a
        resource no static page requests.
        """
        csp = self._value("Content-Security-Policy")
        for cdn in ("unpkg.com", "jsdelivr", "googleapis", "gstatic"):
            self.assertNotIn(cdn, csp, f"{cdn} is allowed but nothing here loads it")

    def test_hsts_is_set(self):
        self.assertIn("max-age=", self._value("Strict-Transport-Security"))

    def test_the_pages_are_not_cached_as_hard_as_the_assets(self):
        """A deploy has to be visible; a stranger on a slow link still gets a cache."""
        self.assertIn("/static/*", self.headers)
        self.assertIn("immutable", self.headers)
        self.assertNotIn("no-store", self._value("Cache-Control"))

    def test_the_clean_urls_the_live_host_prints_still_work(self):
        """Clean URLs must not cause circular 308 redirect loops on Cloudflare Pages.

        Cloudflare Pages handles extensionless clean URLs out of the box. Rewriting
        `/pricing` to `/pricing.html 200` triggers Cloudflare's clean-URL redirect
        loop. _redirects is present but avoids self-referential .html loops.
        """
        for route, filename in (
            ("/pricing", "pricing.html"),
            ("/privacy", "privacy.html"),
            ("/terms", "terms.html"),
        ):
            self.assertNotIn(
                f"{route} /{filename} 200",
                self.redirects,
                f"{route} rewrite to /{filename} causes a 308 redirect loop on Cloudflare Pages",
            )

    def test_cloudflare_pages_worker_and_routes_are_present(self):
        """Cloudflare Pages needs _worker.js and _routes.json to proxy dynamic routes."""
        import json
        worker_path = Path(self.out) / "_worker.js"
        routes_path = Path(self.out) / "_routes.json"
        self.assertTrue(worker_path.is_file(), "_worker.js was not generated")
        self.assertTrue(routes_path.is_file(), "_routes.json was not generated")

        worker_content = worker_path.read_text(encoding="utf-8")
        self.assertIn("bot.example.test", worker_content)
        self.assertIn("/login", worker_content)
        self.assertIn("/dashboard", worker_content)

        routes_data = json.loads(routes_path.read_text(encoding="utf-8"))
        self.assertIn("/login", routes_data["include"])
        self.assertIn("/dashboard", routes_data["include"])

    def test_proxy_mode_keeps_dynamic_links_relative(self):
        """In proxy mode, dynamic links stay on the Cloudflare domain rather than redirecting."""
        self.assertEqual(build_site.rewrite_url("/login", LIVE, proxy_mode=True), "/login")
        self.assertEqual(build_site.rewrite_url("/dashboard", LIVE, proxy_mode=True), "/dashboard")
        self.assertEqual(build_site.rewrite_url("/servers", LIVE, proxy_mode=True), "/servers")
        # Static files and assets still rewrite properly
        self.assertEqual(build_site.rewrite_url("/pricing", LIVE, proxy_mode=True), "pricing.html")


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
