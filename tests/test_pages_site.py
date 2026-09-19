"""The static site built for the static host (Cloudflare Pages).

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
import urllib.parse
from pathlib import Path
from unittest import mock

import keep_alive
from scripts import build_site
from utils import discord_auth

ROOT = Path(__file__).resolve().parent.parent
LIVE = "https://bot.example.test"
SITE = "https://meow-bot.pages.dev"


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

    def test_the_pages_offer_both_grants_and_the_second_one_is_smaller(self):
        """The limited link is the entire justification for the default one.

        A careful owner who will not hand a bot the whole server has to be able
        to *see* the alternative before clicking add, and it has to be a real
        alternative — the same two scopes, a smaller bitfield. A page that offered
        one link while describing two would be worse than the blanket grant alone,
        because it would read as a choice that does not exist.
        """
        for name in ("index.html", "pricing.html"):
            with self.subTest(page=name):
                urls = re.findall(
                    r"https://discord\.com/oauth2/authorize\?[^\"'\s]+", self.pages[name]
                )
                grants = {
                    # Jinja escapes the `&` between parameters, and a browser
                    # reads `&amp;` as `&` — so the mark-up has to be decoded
                    # before the query is parsed, or every key is "amp;…".
                    urllib.parse.parse_qs(
                        urllib.parse.urlparse(url.replace("&amp;", "&")).query
                    )["permissions"][0]
                    for url in urls
                }
                self.assertEqual(
                    grants,
                    {str(discord_auth.INVITE_PERMISSIONS), str(discord_auth.MINIMAL_INVITE_PERMISSIONS)},
                    f"{name} does not offer both grants, it offers {grants}",
                )

    def test_no_page_is_a_dead_end(self):
        for name, text in self.pages.items():
            self.assertNotIn('href="#"', text, f"{name} has a link that goes nowhere")

    def test_a_static_copy_does_not_claim_a_process_is_starting(self):
        """There is no process behind a file, so 'starting up' never resolves."""
        self.assertIn("Always on", self.pages["index.html"])
        self.assertNotIn("sleeping free-tier host", self.pages["index.html"])

    def test_the_pricing_page_says_that_nothing_can_be_bought_yet(self):
        """No checkout is configured for this build, so that is what it must say."""
        pricing = self.pages["pricing.html"]
        self.assertIn("No billing yet", pricing)
        self.assertNotIn("lemonsqueezy.com", pricing)

    def test_the_price_on_the_page_is_the_one_the_bot_will_hold_servers_to(self):
        """One source for both, so the site cannot advertise a number the code lacks.

        They were two: the page carried its own literal prices while the plan
        model in ``utils/plans.py`` carried the limits, and the only thing keeping
        them in step was somebody noticing.
        """
        from utils import plans

        pricing = self.pages["pricing.html"]
        self.assertIn(
            f">{plans.FREE.price}<", pricing,
            f"Free is {plans.FREE.price} in the plan model but not on the page",
        )

    def test_a_page_that_has_nothing_to_sell_does_not_describe_a_shop(self):
        """The premium surface is gone while ``PLAN_CHECKOUT`` is off.

        It used to stay up with the buttons switched off: three tiers, their
        prices, a "Most Popular" badge, and a comparison table with columns for
        plans nobody could buy. That is not a roadmap, it is a page advertising a
        product that does not exist — and the one answer it could not give a
        visitor was the one they came for, which is "what does this cost me".
        """
        pricing = self.pages["pricing.html"]
        for ghost in ("₹499", "₹1,499", "Most Popular", ">Pro<", ">Enterprise<"):
            self.assertNotIn(ghost, pricing, f"the free pricing page still offers {ghost}")
        # The element, not the string: `compare-table` is also a class name in
        # the page's own stylesheet, which is still emitted in both states.
        self.assertNotIn("<table", pricing, "the free page still renders a comparison table")

    def test_the_free_page_says_what_happens_later_without_promising_the_world(self):
        """A price may come back, and the page has to be able to say so.

        Two failure modes are pinned here together because they are each other's
        opposite: a page that says nothing about the future (so a free product
        reads as one that may start charging without warning), and a page that
        promises it will never charge, or that nothing will ever be taken away —
        a promise the plan model can break from the environment, since turning
        ``PLAN_ENFORCEMENT`` on withholds systems from a server with no licence.
        """
        pricing = self.pages["pricing.html"]
        self.assertIn("paid plans", pricing)
        self.assertIn("this page will say so first", pricing)
        for overreach in ("stays free forever", "never charge", "will always be free"):
            self.assertNotIn(overreach, pricing, f"the page promises {overreach!r}")

    def test_the_page_does_not_sell_a_yearly_price_that_does_not_exist(self):
        """Both products in the store are monthly subscriptions.

        The toggle switched every price to a yearly number and a "Save 20%"
        badge, which is a price nobody could ever be charged — the same defect as
        the dead buttons this page was rebuilt to remove, one layer further in.
        """
        pricing = self.pages["pricing.html"]
        for ghost in ("data-yearly", "billingToggle", "Save 20%", "Yearly"):
            self.assertNotIn(ghost, pricing, f"the pricing page still offers {ghost}")

    def test_the_pricing_page_does_not_promise_things_the_code_cannot_keep(self):
        """Four answers on this page described a product that does not exist.

        A trial no product offers and no code implements; a "setting up our
        payment processors" line that stays on the page after checkout is wired;
        **encrypted backups**, when the cloud backup is a raw SQLite file in a
        private bucket and encryption at rest was deliberately removed with the
        old Gist backup; and a 30-day post-downgrade retention window that
        appears in no code path and contradicts the policy's own 90/365 days.
        Each is pinned by name, because all four read perfectly plausible.
        """
        pricing = self.pages["pricing.html"]
        for claim in (
            "7-day free trial",
            "payment processors",
            "encrypted backups",
            "30 days in case",
        ):
            self.assertNotIn(claim, pricing, f"the pricing page still claims {claim!r}")

    def test_the_sitemap_names_only_pages_that_exist(self):
        sitemap = (Path(self.out) / "sitemap.xml").read_text(encoding="utf-8")
        self.assertIn(SITE, sitemap)
        for name in ("pricing.html", "privacy.html", "terms.html"):
            self.assertIn(name, sitemap)
        # Nothing dynamic may be advertised to a crawler: those URLs are on the
        # live app and a sitemap entry for one would 404 for the crawler.
        for path in ("/dashboard", "/login", "/servers"):
            self.assertNotIn(path, sitemap)


class BuiltSiteWithCheckout(unittest.TestCase):
    """The same build with a store behind it — and what it must admit then.

    Two facts only the deployment knows change what is true on this page: whether
    a checkout exists at all, and whether the tiers are enforced. Getting the
    first wrong publishes a page that cannot be bought from; getting the second
    wrong charges for limits that are not in force.
    """

    CHECKOUT_PRO = "https://store.lemonsqueezy.com/checkout/buy/aaa-pro"
    CHECKOUT_ENTERPRISE = "https://store.lemonsqueezy.com/checkout/buy/aaa-ent"

    @classmethod
    def _build(cls, **extra):
        out = tempfile.mkdtemp(prefix="meow-site-paid-")
        env = {
            "MEOW_STATIC_SITE": "1",
            "DISCORD_CLIENT_ID": "1548380269776605324",
            "LEMONSQUEEZY_CHECKOUT_PRO": cls.CHECKOUT_PRO,
            "LEMONSQUEEZY_CHECKOUT_ENTERPRISE": cls.CHECKOUT_ENTERPRISE,
            "PLAN_CHECKOUT": "1",
            **extra,
        }
        with mock.patch.dict(os.environ, env):
            build_site.build(out, LIVE, SITE)
        return (Path(out) / "pricing.html").read_text(encoding="utf-8"), out

    def test_a_configured_store_is_where_the_buttons_go(self):
        pricing, out = self._build()
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        self.assertIn(self.CHECKOUT_PRO, pricing)
        self.assertIn(self.CHECKOUT_ENTERPRISE, pricing)
        self.assertNotIn("No billing yet", pricing)
        self.assertIn("Checkout by Lemon Squeezy", pricing)
        self.assertNotIn('href="#"', pricing)

    def test_the_page_says_plainly_while_the_limits_are_not_enforced(self):
        """Selling a tier whose limits are dormant has to be said out loud."""
        pricing, out = self._build()
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        self.assertIn("Limits are not switched on yet", pricing)

    def test_the_admission_disappears_once_the_limits_are_real(self):
        pricing, out = self._build(PLAN_ENFORCEMENT="1")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        self.assertNotIn("Limits are not switched on yet", pricing)
        self.assertIn(self.CHECKOUT_PRO, pricing)

    def test_a_configured_store_does_not_sell_until_the_switch_is_on(self):
        """Both URLs filled in is configuration, not consent to take money.

        The way this goes wrong is quiet: a store is set up over an evening, one
        link is pasted into the host, and the page starts charging before anyone
        meant it to. ``PLAN_CHECKOUT`` is what separates the two — and with it off
        the premium surface goes with the buttons, rather than staying up as a
        price list nobody can pay. This is the assertion that flipped: it used to
        require the tiers to remain.
        """
        pricing, out = self._build(PLAN_CHECKOUT="0")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        self.assertNotIn("lemonsqueezy.com", pricing, "a Buy link survived the switch")
        self.assertIn("No billing yet", pricing)
        self.assertNotIn("Checkout by Lemon Squeezy", pricing)
        for ghost in ("₹499", "₹1,499", ">Pro<", ">Enterprise<"):
            self.assertNotIn(ghost, pricing, f"a configured store still shows {ghost}")
        # What a visitor is left with is the product, still addable and still
        # fully described — including the systems this page used to sell.
        for system in ("Support Tickets", "Activity Tracker", "Real-time dashboard"):
            self.assertIn(system, pricing)
        self.assertIn("discord.com/oauth2/authorize", pricing)

    def test_a_store_that_is_really_open_still_sells_the_named_prices(self):
        """Switching it on brings the tiers back, at the model's own numbers."""
        from utils import plans

        pricing, out = self._build(PLAN_ENFORCEMENT="1")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        for plan in (plans.FREE, plans.PRO, plans.ENTERPRISE):
            self.assertIn(
                f">{plan.price}<", pricing,
                f"{plan.label} is {plan.price} in the plan model but not on the page",
            )


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
