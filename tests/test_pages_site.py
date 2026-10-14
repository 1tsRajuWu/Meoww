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

from flask import session

import keep_alive
from scripts import build_site
from utils import discord_auth, features

ROOT = Path(__file__).resolve().parent.parent
LIVE = "https://bot.example.test"
SITE = "https://dacat.pages.dev"


class RewriteUrlCase(unittest.TestCase):
    """The one function every URL in every page goes through."""

    def test_assets_become_relative_to_the_site_root(self):
        self.assertEqual(build_site.rewrite_url("/static/meow.css", LIVE), "static/meow.css")
        # A cache-busting query has to survive, or the deploy serves a stale asset.
        self.assertEqual(
            build_site.rewrite_url("/static/favicon.svg?v=2.2", LIVE),
            "static/favicon.svg?v=2.2",
        )

    def test_a_page_that_exists_here_becomes_its_file(self):
        self.assertEqual(build_site.rewrite_url("/pricing", LIVE), "pricing.html")
        self.assertEqual(build_site.rewrite_url("/privacy", LIVE), "privacy.html")
        self.assertEqual(build_site.rewrite_url("/terms", LIVE), "terms.html")
        # The three site pages: every link between them is rewritten by this one
        # function, so a route missing from `PAGES` is a link left pointing at the
        # live app — which answers 404 for a page that is a file here.
        self.assertEqual(build_site.rewrite_url("/systems", LIVE), "systems.html")
        self.assertEqual(build_site.rewrite_url("/start", LIVE), "start.html")
        # ...and a fragment into either of them survives.
        self.assertEqual(build_site.rewrite_url("/start#ways-in", LIVE), "start.html#ways-in")
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
        """One prefix or none.

        ``meow.css`` sits in ``static/``, so ``static/logo.svg`` written inside
        it resolves to ``static/static/…`` and the mark never loads.
        """
        self.assertEqual(
            build_site.rewrite_url("/static/logo.svg", LIVE, from_stylesheet=True),
            "logo.svg",
        )
        self.assertEqual(
            build_site.rewrite_url("/static/logo.svg", LIVE),
            "static/logo.svg",
        )

    def test_an_already_relative_url_is_not_double_rewritten(self):
        """Building twice must be safe; a relative path is already correct."""
        self.assertEqual(build_site.rewrite_url("static/meow.css", LIVE), "static/meow.css")
        self.assertEqual(build_site.rewrite_url("pricing.html", LIVE), "pricing.html")


class TheBuildRefusesToPublishTheRepository(unittest.TestCase):
    """``--out .`` is how a private repository becomes a public website.

    This is the state the Pages project was actually in: no build command and
    no output directory, so Cloudflare published the checkout — and its own log
    said so plainly ("No build command specified. Skipping build step.", then
    "Uploading... (185/185)", and the repository has exactly 185 tracked files).
    While that lasted, ``/bot.py``, ``/utils/cloud_sync.py``, ``/cogs/economy.py``,
    ``/render.yaml`` and ``/.env.example`` all answered 200 at the site's own
    address. No *values* leaked, because every secret lives in the host's
    environment rather than in the tree — but the tree was public.

    A static host is handed a directory and told "everything in here is
    published", so the one thing this builder must never accept is a directory
    that contains the source. It is refused rather than warned about: by the
    time the upload runs, it is too late to take back.
    """

    def test_the_repository_root_is_refused_however_it_is_spelled(self):
        root = Path(build_site.ROOT).resolve()
        spellings = [
            str(root),
            str(root) + os.sep,
            os.path.join(str(root), "."),
            os.path.join(str(root), "sub", ".."),
        ]
        for target in spellings:
            with self.subTest(target=target):
                with self.assertRaises(SystemExit) as caught:
                    build_site.build(target, LIVE, SITE)
                self.assertIn("repository root", str(caught.exception))

    def test_an_ordinary_output_directory_still_builds(self):
        """The guard has to cost a real build nothing, or it is a trap."""
        out = tempfile.mkdtemp(prefix="meow-site-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        with mock.patch.dict(
            os.environ,
            {
                "MEOW_STATIC_SITE": "1",
                "MEOW_SITE_PREVIEW": "1",
                "DISCORD_CLIENT_ID": "1548380269776605324",
            },
        ):
            build_site.build(out, LIVE, SITE)
        self.assertTrue((Path(out) / "index.html").is_file())

    def test_a_retired_page_cannot_survive_in_the_artifact(self):
        """The directory is the build's output, not a place it writes into.

        This is a defect that shipped: `/music` was retired and the *file* of its
        old page was still in the build directory, so every deploy uploaded it
        again — a page advertising "a live queue with real-time audio effects",
        next to the `_redirects` rule meant to answer that address with the page
        that replaced it. A static host publishes everything it is handed, so a
        leftover is a page nobody chose to publish.
        """
        out = tempfile.mkdtemp(prefix="meow-site-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        stale = Path(out) / "music.html"
        stale.write_text("<html>a live queue with real-time audio effects</html>")
        buried = Path(out) / "static" / "old-thing.js"
        buried.parent.mkdir(parents=True, exist_ok=True)
        buried.write_text("// a script from a build nobody remembers")

        with mock.patch.dict(
            os.environ,
            {
                "MEOW_STATIC_SITE": "1",
                "MEOW_SITE_PREVIEW": "1",
                "DISCORD_CLIENT_ID": "1548380269776605324",
            },
        ):
            build_site.build(out, LIVE, SITE)

        self.assertFalse(stale.exists(), "a retired page was left to be published")
        self.assertFalse(buried.exists(), "a stale asset was left to be published")
        self.assertTrue((Path(out) / "pricing.html").is_file(), "the live page is missing")


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
                # The maintenance screen is what a *deployment* publishes; these
                # laws are about the pages underneath it, so the build under
                # test steps around the BRB hook for the duration of the render
                # (`MEOW_SITE_PREVIEW`, the flag `scripts/preview_site.py` sets
                # for a laptop). Scoped, never left in the environment: the BRB
                # suite removes `pytest` from `sys.modules` and expects the hook
                # to answer.
            },
        )
        cls._env.start()
        cls.report = build_site.build(cls.out, LIVE, SITE)
        cls.pages = {
            name: (Path(cls.out) / name).read_text(encoding="utf-8")
            for name in (
                "index.html",
                "systems.html",
                "start.html",
                "pricing.html",
                "privacy.html",
                "terms.html",
            )
        }

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()
        shutil.rmtree(cls.out, ignore_errors=True)

    def test_every_page_is_written(self):
        for name in (
            "index.html",
            "systems.html",
            "start.html",
            "pricing.html",
            "privacy.html",
            "terms.html",
        ):
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

    def test_the_built_404_is_the_page_the_app_serves(self):
        """One error page, two hosts — and no asset that only exists in a memory.

        The published `404.html` used to be HTML written in `build_site.py` by
        hand, and by the time anybody looked it named `page-404`, `brand-logo`,
        `hero-cta`, `btn-primary` and three more classes the sheet had stopped
        defining, beside `static/logo.svg`, which the redesign deleted. A mistyped
        address on the CDN got a broken-image glyph and plainly unstyled text.

        It is now the app's own answer for a path nothing routes, fetched the same
        way every other page is — so this reads the built file the way a browser
        would: the headings have to be there, and every local asset it names has
        to exist in the directory beside it.
        """
        path = Path(self.out) / "404.html"
        self.assertTrue(path.is_file(), "no 404.html was written")
        body = path.read_text(encoding="utf-8")
        self.assertIn("nothing at that address", body)
        self.assertIn("meow.css", body, "the built 404 does not load the sheet")
        self.assertNotIn("/static/", body, "a root-relative URL survived into the 404")
        local = re.findall(r'(?:src|href)="((?!https?:|#)[^"]+)"', body)
        self.assertTrue(local, "the built 404 links to nothing at all")
        for url in local:
            target = (Path(self.out) / url.split("?")[0].split("#")[0]).resolve()
            if target.is_dir() or url.endswith("/"):
                continue
            self.assertTrue(
                target.is_file() or (target.parent / "index.html").is_file(),
                f"the built 404 points at {url}, which is not in the build",
            )

    def test_the_served_stylesheet_names_no_cursor_image(self):
        """The silent one: a cursor image a browser refuses is the system arrow.

        Such a path is relative to the *stylesheet*, so a browser resolves it
        next to that file — and nothing here can notice when it is missing. The
        sheet therefore names none.
        """
        css = (Path(self.out) / "static" / "meow.css").read_text(encoding="utf-8")
        self.assertEqual([], re.findall(r"cursor:\s*url\(", css))

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
        # `/start` is where the two grants are offered side by side now — the copy
        # that compares them moved there with the rest of the get-started content,
        # and the front door keeps only the default link.
        # pricing.html carries only the site-wide footer's default link and
        # describes no pair; the choice is offered where the copy compares the
        # two grants — the front door's step and /start's ways-in.
        for name in ("index.html", "start.html"):
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

    def test_a_static_copy_does_not_publish_the_builder_s_own_database(self):
        """The tracker is a file on the machine running the build.

        Every activity tile on the front page is drawn server-side from that
        snapshot, so a build printed the operator's own laptop as the deployment:
        *467 messages this week* on a public page, about a bot the reader is not
        running. The hub's pages applied this rule from the day they were written
        and the landing page never did — which is why the guard is on the built
        artifact rather than on the function that produces it.

        An em dash, not a zero: *nobody has read this* and *this is empty* are
        different claims, and only one of them is true here.
        """
        # On `/systems`, which is where the deployment's own readings live: the
        # front door kept the live *counts* and the activity cards went with the
        # rest of the detail. The tiles are identified by the key they carry
        # (`data-figure`, which `/api/figures` answers for a built copy) and the
        # label beside them; the value is what this law is about.
        systems = self.pages["systems.html"]
        tiles = re.findall(
            r'<b data-figure="activity:[^"]*"[^>]*>([^<]*)</b>\s*'
            r'<span>(?:messages|voice hours|people seen|channels used)</span>',
            systems,
        )
        self.assertTrue(tiles, "the activity tiles left the site")
        for value in tiles:
            self.assertEqual(value, "—", f"a built page carries a local reading: {value}")
        # ...and the control tiles are the two that are facts about the code, so
        # they are present and counted from the registry rather than blanked.
        self.assertRegex(
            systems,
            rf'<b data-figure="systems"[^>]*>{features.system_count()}</b>\s*'
            rf'<span>systems</span>',
        )
        self.assertRegex(
            systems,
            rf'<b data-figure="commands"[^>]*>{features.entry_count()}</b>\s*'
            rf'<span>commands</span>',
        )
        # The front door must not have grown a second, unfiltered copy of them.
        self.assertNotIn(
            'data-figure="activity:',
            self.pages["index.html"],
            "the front door carries activity windows of its own",
        )

    def test_the_front_page_and_the_reference_agree_on_how_many_of_each(self):
        """One bot, one answer — the defect this page had in production.

        The front page advertised **11 systems and 100 commands** while the hub a
        click away said **12 and 90**. Neither count was broken: *systems* was the
        dashboard's module list on one surface and the registry on the other, and
        *commands* was every declared string — including `/market buy` — against
        the distinct names Discord publishes. A visitor had no way to tell which
        page was lying, which is the whole cost of two renderers and two numbers.
        """
        index = self.pages["index.html"]
        systems, commands = features.system_count(), features.entry_count()
        switchable = len(features.dashboard_modules())
        self.assertLess(switchable, systems, "the split this page explains has gone")
        # The counts the front door states, and the *split* stated rather than
        # left to be discovered — in the hero's own lede, in the "what is in it"
        # band's heading and in the panel's figure cards, so two pages
        # disagreeing about one word is what this catches.
        self.assertIn(f"{systems} systems", index)
        self.assertIn(f"{commands} commands", index)
        self.assertIn(f"{switchable} of them switchable", index)
        # The hero's figure card is the *total*, not the switchable subset — which
        # is how this page came to advertise eleven of twelve systems. The split it
        # may not blur is asserted on the line above; this is that the card itself
        # carries the registry's own number, with the key the live refresh reads.
        self.assertRegex(
            index,
            rf'<b data-figure="systems"[^>]*>{systems}</b>\s*'
            rf'<span>systems</span>',
        )
        # ...and the reference page agrees with it, from the same registry —
        # stated in its own heading ("16 systems. 139 commands.").
        self.assertIn(f"{systems} systems", self.pages["systems.html"])
        self.assertIn(f"{commands} commands", self.pages["systems.html"])
        self.assertNotIn(f'data-figure="commands">{commands + 10}<', index)

    def test_no_page_links_to_the_private_repository(self):
        """A footer link that 404s is how a project reads as abandoned.

        The repository is private, so "Source code" and "GitHub" handed every
        visitor GitHub's *"This is not the web page you are looking for"* — on the
        footer of the page that asks them to trust this bot with their server. The
        link is gone while there is nowhere to go, and this is the guard on it.
        """
        for name, body in self.pages.items():
            self.assertNotIn("github.com/", body, f"{name} links to a repository")

    def test_a_static_copy_does_not_claim_a_process_is_starting(self):
        """There is no process behind a file, so 'starting up' never resolves."""
        self.assertIn("Always on", self.pages["index.html"])
        self.assertNotIn("sleeping free-tier host", self.pages["index.html"])

    def test_the_pricing_page_says_that_nothing_can_be_bought_yet(self):
        """No checkout is configured for this build, so that is what it must say."""
        pricing = self.pages["pricing.html"]
        self.assertIn("There is no paid tier", pricing)
        self.assertNotIn("lemonsqueezy.com", pricing)

    def _rebuild(self, **env):
        """A second build under extra environment, inspected the same way.

        The switch that decides whether anything is for sale is a fact about the
        deployment, so the only honest way to test the *other* position is to
        build again with it set — a page rendered from a hard-coded boolean would
        prove nothing about the host.
        """
        out = tempfile.mkdtemp(prefix="meow-site-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        with mock.patch.dict(os.environ, {"MEOW_SITE_PREVIEW": "1", **env}):
            build_site.build(out, LIVE, SITE)
        return {
            name: (Path(out) / name).read_text(encoding="utf-8")
            for name in ("index.html", "systems.html", "start.html", "pricing.html")
        }

    def test_the_price_on_the_page_is_the_one_the_bot_will_hold_servers_to(self):
        """One source for both, so the site cannot advertise a number the code lacks.

        They were two: the page carried its own literal prices while the plan
        model in ``utils/plans.py`` carried the limits, and the only thing keeping
        them in step was somebody noticing.

        Checked on the build where the tiers are *on sale*, because that is the
        only build that shows a number — the free one deliberately names no
        price at all (see the test below, which pins that).
        """
        from utils import plans

        pricing = self._rebuild(PLAN_CHECKOUT="1")["pricing.html"]
        for plan in (plans.FREE, plans.PRO, plans.ENTERPRISE):
            self.assertIn(
                f">{plan.price}<", pricing,
                f"{plan.label} is {plan.price} in the plan model but not on the page",
            )

    def test_the_free_page_quotes_no_price_and_no_billing_period(self):
        """`₹0` beside a `/mo` is a price tag on a product nobody can buy.

        The page no longer advertises the tiers, but its own free card kept
        rendering the model's currency and a monthly period — so the one place
        that should say "free" read as a subscription that costs nothing, which
        is a different claim from "there is nothing to buy".
        """
        pricing = self.pages["pricing.html"]
        self.assertNotIn("₹", pricing, "the free page still quotes a price")
        self.assertNotIn("/mo<", pricing, "the free page still names a billing period")

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
        for name in ("systems.html", "start.html", "pricing.html", "privacy.html", "terms.html"):
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
            "MEOW_SITE_PREVIEW": "1",
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
        self.assertNotIn("There is no paid tier", pricing)
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
        self.assertIn("There is no paid tier", pricing)
        self.assertNotIn("Checkout by Lemon Squeezy", pricing)
        for ghost in ("₹499", "₹1,499", ">Pro<", ">Enterprise<"):
            self.assertNotIn(ghost, pricing, f"a configured store still shows {ghost}")
        # What a visitor is left with is the product, still addable and still
        # fully described — including the systems this page used to sell. (The
        # old third item, "Real-time dashboard", was a feature name rather than
        # a registry system; the two real ones are here under their own names.)
        for system in ("Support Tickets", "Activity Tracker"):
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


class TheWorkerAndTheRoutesAgreeAboutWhatIsDynamic(unittest.TestCase):
    """Two lists in one build, and a path has to be on both to be proxied.

    `_routes.json` is the first gate: Cloudflare decides from it whether the
    worker runs **at all**, so a prefix that is in the worker's own
    ``DYNAMIC_PREFIXES`` but missing there is answered from the published
    directory instead — a 404 for anything this site does not publish, with
    nothing in any log to say why, while the worker that would have proxied it
    sits unreachable beside it.

    That is the state `/agree` was in. It is the clickwrap at the door: every
    route that signs somebody in answers a redirect to it before anything else,
    and no page of this site is it — so with the path missing from this list the
    agreement was a page nobody could reach, on a host where the worker had been
    taught to proxy it and never got the chance.
    """

    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.mkdtemp(prefix="meow-site-routes-")
        build_site.build(cls.out, LIVE, proxy_mode=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def _worker_prefixes(self) -> list[str]:
        worker = (Path(self.out) / "_worker.js").read_text(encoding="utf-8")
        block = re.search(r"const DYNAMIC_PREFIXES = \[(.*?)\];", worker, flags=re.S)
        self.assertIsNotNone(block, "the worker no longer declares what it proxies")
        return re.findall(r'"([^"]+)"', block.group(1))

    def _routes_include(self) -> list[str]:
        import json

        routes = json.loads((Path(self.out) / "_routes.json").read_text(encoding="utf-8"))
        return routes["include"]

    def test_every_dynamic_prefix_reaches_the_worker(self):
        include = self._routes_include()
        self.assertGreaterEqual(len(self._worker_prefixes()), 5, "the prefixes were not found")
        for prefix in self._worker_prefixes():
            with self.subTest(prefix=prefix):
                self.assertTrue(
                    prefix in include or prefix + "/*" in include,
                    f"the worker proxies {prefix} and Cloudflare never runs the "
                    "worker for that path, so its answer is never reached and the "
                    "request is served from the published files instead",
                )

    def test_the_console_is_handed_to_its_own_host_rather_than_served_here(self):
        """`/owner` is still this worker's business, and no longer this site's page.

        The fleet console — every server, the economy ledgers, the audit trail and
        the God tier — used to be served **under this site's marketing header**, on
        the members' domain, with a nav bar offering "Add to Discord". Reported
        from a browser, and the reason "is this the dashboard?" had two answers:
        the operator's console read as a page of the public site, and the public
        domain was an address where the fleet's controls were reachable at all.

        Both halves are asserted here, because either one alone is the old
        behaviour: the path is still in the worker's list (a path *missing* from
        it is answered from the published files instead, which is a 404 rather
        than the redirect), and the redirect is written above the proxy.
        """
        worker = (Path(self.out) / "_worker.js").read_text(encoding="utf-8")
        self.assertIn('const CONSOLE_HOST = "dacatowner.pages.dev";', worker)
        self.assertIn("/owner", self._worker_prefixes())
        guard = worker.index('url.pathname === "/owner"')
        self.assertLess(
            guard,
            worker.index("const isDynamic = DYNAMIC_PREFIXES.some("),
            "the console handover sits after the proxy, so /owner is proxied anyway",
        )
        handover = worker[guard : worker.index("const isDynamic", guard)]
        self.assertIn("status: 301", handover)
        self.assertIn("target.hostname = CONSOLE_HOST", handover)

    def test_nothing_is_sent_to_the_worker_that_it_does_not_handle(self):
        prefixes = self._worker_prefixes()
        for entry in self._routes_include():
            base = entry.rstrip("/*")
            with self.subTest(route=entry):
                self.assertTrue(
                    any(base == p or base.startswith(p + "/") for p in prefixes),
                    f"Cloudflare runs the worker for {entry} and the worker does "
                    "not know that path, so it falls through to the CDN",
                )

    def test_the_clickwrap_is_proxied_rather_than_published(self):
        """The one page of the sign-in flow that no build can render.

        Derived from the backend's own redirect rather than named here: the path
        is a `Location` header, so it is in no template and no page list, and the
        next door added to the flow is covered by this test existing.

        The session is signed in on purpose, and that is not decoration: the door
        is only the agreement for a session that has not agreed — a visitor gets
        the page they asked for — so reading it anonymously returns `/dashboard`
        and every assertion below would be about the wrong path. (It was, in the
        first draft of this test.)
        """
        with keep_alive.app.test_request_context():
            session["authenticated"] = True
            door = urllib.parse.urlsplit(keep_alive._door_target()).path
        self.assertEqual(
            door,
            "/agree",
            "a signed-in session that has not agreed is no longer sent to the "
            "clickwrap, so this test is asserting about the wrong page",
        )
        self.assertNotIn(
            door,
            [route for route, _name in build_site.PAGES],
            f"{door} is built as a page, and the agreement needs a session and a "
            "CSRF token — a static copy of it would be a form nothing accepts",
        )
        self.assertFalse(
            (Path(self.out) / (door.strip("/") + ".html")).exists(),
            f"the build published {door} after all, so the CDN answers it and the "
            "worker never sees the request",
        )
        prefixes = self._worker_prefixes()
        self.assertTrue(
            any(door == p or door.startswith(p + "/") for p in prefixes),
            f"every sign-in is redirected to {door!r} and this site does not "
            "proxy it, so the clickwrap 404s on the host that links to it",
        )


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
