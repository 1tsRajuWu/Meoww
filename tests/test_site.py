"""The front door and the two policies, as the web sees them.

`/` used to redirect, so the first thing a visitor met was a password box for a
panel they had never heard of. What replaced it is a page built from the running
process's own figures, the feature registry and the command tree — so the tests
here are about *honesty* first (no invented number, no advertised system the bot
does not have, no claim on a page that the code contradicts) and about the
renderer that puts the policy files on the web second, because a renderer that
drops half a sentence from a policy is worse than no page at all.
"""
import html
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import keep_alive
from tests.test_dashboard import CSRF, GUILD_ID, _DashboardFixture, make_bot
from utils import brand, figures, webdocs

ROOT = Path(__file__).resolve().parent.parent
PAGES = ("home.html", "systems.html", "start.html", "legal.html", "login.html", "servers.html", "dashboard.html", "market.html", "pricing.html")

#: The pages that share a layout. They reach the stylesheet and the reveal script
#: through `site_base.html`, so what is asserted of them is that they reach it.
#: The dashboard is one of them as of its rebuild: eleven addresses of one
#: template that extends the shared layout, so asking it to link `meow.css` itself
#: would be asking for a second copy of the shell's own head. `market.html` is
#: deliberately *not* here — it is a trading surface with a frame of its own and
#: links the sheet directly, which is what the loop below asserts of it.
SITE_PAGES = (
    "home.html", "systems.html", "start.html",
    "legal.html", "login.html", "servers.html", "pricing.html",
    "dashboard.html",
)
BASE_TEMPLATE = "site_base.html"


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
        # `/` is now drawn from `utils.figures`, whose snapshot is cached for a
        # minute *process-wide* — so the reading one test takes is the reading the
        # next one is served. The cache cannot tell that the gateway it just
        # asked was a different mock, or that the database underneath it was
        # swapped. Every fixture that renders this page resets it, which is what
        # `figures.reset()` exists for.
        figures.reset()
        self.client = keep_alive.app.test_client()

    def tearDown(self):
        self._patched.stop()
        figures.reset()

    def test_a_monitor_still_gets_the_one_line_it_parses(self):
        for agent in ("UptimeRobot/2.0", "curl/8.4.0"):
            body = self.client.get("/", headers={"User-Agent": agent}).get_data(as_text=True)
            self.assertEqual(body, f"{brand.NAME} is alive and operational!", agent)

    def test_a_visitor_gets_the_page_not_a_redirect(self):
        response = self.client.get("/", headers={"User-Agent": "Mozilla/5.0"})
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        for fragment in (
            keep_alive._bot_display_name(),  # the name, as the process answers it
            "systems",                  # how many there are
            'href="/login"',            # a way in
            "/privacy", "/terms",       # the policies
            'class="page-home"',        # which surface this page is
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
        """A hand-written copy is what let a removed module keep showing.

        The list is on `/systems`, not on the front door: the band that used to
        carry all fifteen systems under the hero was a third of the page's height
        (`#systems` measured 1,549 of 5,000 pixels) and a second place to keep one
        list, and a list kept in two places is the copy that goes stale. The
        law did not move with the list by accident — this is the page that now
        names every system, so it is the page that has to be drawn from the
        registry rather than typed.
        """
        from utils import features

        body = self.client.get("/systems").get_data(as_text=True)
        # *The list*, not the page. Three other bands on this page name systems
        # too — the command-weight bars, the command groups, the always-on
        # sentence — so a law that searched the whole document would pass on a
        # list that had quietly lost a row, which is the defect it is here for.
        start = body.index('class="list"')
        rows = body[start : body.index("</ul>", start)]
        for module in features.dashboard_modules():
            # Rendered, so "&" arrives as the entity.
            self.assertIn(module["name"].replace("&", "&amp;"), rows, module["id"])

    def test_the_front_door_carries_no_list_it_would_have_to_keep_in_step(self):
        """What left the page, asserted as *absence* rather than by length.

        A front door that still rendered the registry row by row would be the
        section back with a shorter heading, and the failure it would restore is
        quiet: two lists, one registry, and the day they disagree is a day nobody
        notices. The panel beside the hero is the exception and stays — it names
        four systems and links to the rest.
        """
        front = self.client.get("/").get_data(as_text=True)
        self.assertNotIn('id="systems"', front, "the front door grew the band back")
        self.assertNotIn("data-explorer", front, "the front door filters a list of its own")
        self.assertIn("href=\"/systems\"", front, "and nothing on it reaches the list")

    def test_no_link_on_the_front_door_goes_to_a_fragment_it_has_not_got(self):
        """The way this change fails is silence.

        A fragment is never sent to the server, so a link to a section that has
        left the page scrolls nowhere, logs nothing and throws nothing — the
        browser stays exactly where it is and the reader concludes the link is
        dead. Both of the front door's own links into the systems band were
        rewritten to `/systems` when the band went; this is what would have
        caught it if they had not been, and what catches the next one.
        """
        front = self.client.get("/").get_data(as_text=True)
        ids = set(re.findall(r'\bid="([^"]+)"', front))
        targets = set(re.findall(r'href="#([^"]+)"', front))
        self.assertTrue(targets, "no in-page link was found, so nothing is checked")
        missing = sorted(target for target in targets if target not in ids)
        self.assertEqual(
            missing,
            [],
            f"the front door links to #{missing} and no element carries that id",
        )

    def test_no_figure_is_invented_while_the_bot_is_asleep(self):
        """A cold host must not render a confident "0 servers".

        Read as *values*, not as words. The first version of this asked whether
        the string "servers</span>" appeared anywhere on the page, which is true
        of the label beside the number as well as of the number — so it passed
        for the wrong reason and would have gone on passing if the tile had
        started reading `0` above it. What it means to check is that the two
        figures only the gateway can answer are *dashed* rather than zeroed, on
        both pages that draw them, and that the two the registry always knows
        are still counted.
        """
        asleep = mock.Mock()
        asleep._has_started = False
        asleep.is_ready.return_value = False
        asleep.guilds = []
        with mock.patch.object(keep_alive, "BOT_INSTANCE", asleep):
            front = self.client.get("/").get_data(as_text=True)
            # The deployment's own readings: this is the page that asks a
            # database and a gateway for figures, and the one where an absent
            # reading could be printed as a measurement.
            body = self.client.get("/systems").get_data(as_text=True)
        import re

        self.assertIn("Starting up", front)
        for label in ("servers", "members reached"):
            with self.subTest(label=label):
                tile = re.search(
                    rf'<span class="tile__value"[^>]*>([^<]*)</span>\s*'
                    rf'<span class="tile__label">{re.escape(label)}</span>',
                    body,
                )
                self.assertIsNotNone(tile, f"the {label} tile left the page")
                self.assertEqual(
                    tile.group(1).strip(), keep_alive.NOT_READ,
                    f"a sleeping host did not say it had no reading for {label}",
                )
        # What it does know without the gateway is still on the page, and the
        # same two rules hold one page earlier: the registry's counts are
        # *counted*, and the two figures only a gateway can answer are dashes.
        #
        # This used to be an absence — the front door's hero panel drew the
        # registry's two cards and nothing else, so the law could say *servers is
        # not on this page at all*. The panel now draws the same four cards
        # `/systems` opens with, from the same reader, which is a stronger claim
        # to hold: the two live ones are **present and dashed**, which is what a
        # visitor of a sleeping host reads, and it would catch a zero here where
        # the old wording could not.
        for label in ("systems", "commands"):
            with self.subTest(label=label):
                self.assertRegex(
                    front,
                    rf'data-count="\d+">\d+</dd>\s*'
                    rf'<dt class="figure__label">{label}</dt>',
                )
        for label in ("servers", "members reached"):
            with self.subTest(label=label):
                self.assertRegex(
                    front,
                    rf'<dd class="figure__value"[^>]*>—</dd>\s*'
                    rf'<dt class="figure__label">{re.escape(label)}</dt>',
                    f"the front door did not say it had no reading for {label}",
                )
        self.assertRegex(
            body,
            r'<span class="tile__value"[^>]*>\d+</span>\s*'
            r'<span class="tile__label">commands</span>',
        )

    def test_the_panels_four_cards_state_their_phone_shape(self):
        """`auto-fit` arrived at a layout nobody chose, and a phone showed it.

        Measured at 390px in a browser: four cards of `minmax(76px, 1fr)` inside
        a 346px panel made three columns of 81px, so *117 commands* wrapped onto
        a line of its own and `1,284` overflowed its cell by 7px — which took
        the page's own `.wrap` over the viewport with it. The row is *stated* at
        both sizes now: four cards on a desktop, two on a phone, and a value
        that may shrink for the widest figure the card can hold.
        """
        sheet = (ROOT / "static" / "landing.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", sheet)
        phone = sheet.split("@media (max-width: 560px)")[1]
        self.assertIn(".figures { grid-template-columns: repeat(2, minmax(0, 1fr)); }", phone)
        self.assertIn("font-size: clamp(1.25rem, 5vw, 1.5rem)", sheet)

    def test_the_front_door_asks_for_one_permission_and_says_so_twice(self):
        """The step and the first question disagreed about the same button.

        Read on the served page: step 01 said *"The invite asks for
        Administrator"*, and the question right below it answered *"No. The
        invite asks for what the systems you keep need"* — one URL, two
        permission claims, on one screen, for a visitor deciding whether to
        install. The link the page offers really does carry `permissions=8`
        (`discord_auth.INVITE_PERMISSIONS` is the Administrator bitfield, and
        the minimal one is the second link beside it), so the answer is the one
        that was wrong.
        """
        front = self.client.get("/").get_data(as_text=True)
        self.assertIn("asks for Administrator", front)
        answers = [answer for question, answer in keep_alive.LANDING_QUESTIONS
                   if "administrator" in question.lower()]
        self.assertTrue(answers, "the front door stopped answering its own question")
        for answer in answers:
            self.assertIn("Administrator", answer, answer)
            self.assertNotIn(
                "asks for what the systems you keep need",
                answer,
                "the answer claims a permission the invite button does not ask for",
            )

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
        """Read out of the file, not written down twice.

        This pinned the literal date, which is the one thing it is named for not
        doing: the document's date advanced a version and the page was already
        printing the new one correctly, while the assertion still asked for the
        old. It is read from the document now, so the two cannot drift — which is
        the property the page has and the test is here to hold.
        """
        import re

        from cogs import legal

        body = self.client.get("/privacy").get_data(as_text=True)
        text = legal.read_document("PRIVACY.md")
        found = re.search(r"\*\*Effective Date:\*\* ([A-Z][a-z]+ \d{1,2}, \d{4})", text)
        self.assertIsNotNone(found, "the document states no effective date")
        self.assertIn(found.group(1), body)
        self.assertNotIn("**", body, "markdown emphasis leaked into the page")

    def test_a_missing_document_is_a_404_and_not_an_empty_page(self):
        with mock.patch("cogs.legal.read_document", return_value=""):
            response = self.client.get("/privacy")
        self.assertEqual(response.status_code, 404)
        self.assertIn("missing from this deploy", response.get_data(as_text=True))


class ThePricingPage(unittest.TestCase):
    def setUp(self):
        self.client = keep_alive.app.test_client()

    def test_pricing_page_renders_the_product_that_exists(self):
        """One plan, every system, and the stylesheet.

        This asserted the three tier names while the page was listing Pro and
        Enterprise at prices nobody could pay — a test that passes on a page
        advertising a shop that is not open. What the route owes a visitor with
        nothing on sale is the product: the free plan, the systems it includes,
        and a stylesheet to read it with.
        """
        response = self.client.get("/pricing")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Free", body)
        self.assertIn("There is no paid tier", body)
        self.assertIn("the whole bot is in it", body)
        self.assertIn("Support Tickets", body)
        self.assertIn("/static/meow.css", body)

    def test_the_tiers_come_back_only_when_the_store_is_really_open(self):
        """The switch, through the live route rather than the template.

        `PLAN_CHECKOUT` is what separates a page describing a store from a page
        selling from one, and this is the route an operator actually flips it on.
        """
        with mock.patch.dict(
            os.environ,
            {
                "PLAN_CHECKOUT": "1",
                "LEMONSQUEEZY_CHECKOUT_PRO": "https://store.lemonsqueezy.com/checkout/buy/aaa-pro",
                "LEMONSQUEEZY_CHECKOUT_ENTERPRISE": "https://store.lemonsqueezy.com/checkout/buy/aaa-ent",
            },
        ):
            body = self.client.get("/pricing").get_data(as_text=True)
        self.assertIn("Pro", body)
        self.assertIn("Enterprise", body)
        self.assertNotIn("There is no paid tier", body)


class TheRadioTableCanBeAsked(unittest.TestCase):
    """``/radio`` renders 308 stations and opens with two dozen of them.

    The page it replaces listed the first forty-eight and then told the visitor
    to go and type into Discord — a table that answers questions about the bot
    by asking you to leave the page. What replaces it is a list with a search
    and the catalogue's own two menus in front of it, and every assertion below
    is a way that could go quietly wrong: a station that gets no row, a row with
    no haystack (so searching its own name cannot find it), an option in a menu
    that the table does not hold, a cap that is off by one, or a count printed
    on the page that is not the count of the thing it names.
    """

    def setUp(self):
        self.client = keep_alive.app.test_client()
        from utils import radio_catalogue

        self.catalogue = radio_catalogue.CATALOGUE
        self.index = radio_catalogue.index()

    def _page(self):
        response = self.client.get("/radio")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_every_station_gets_a_row_that_can_be_found_by_its_own_name(self):
        """One row per station, and each row carries its own haystack.

        The haystack is what the page's search reads, so an empty one is a row
        that the search cannot match however hard a visitor types — and it
        carries the name, country, language and genre, because those are the
        words the placeholder promises.
        """
        body = self._page()
        self.assertEqual(
            body.count("data-search="), len(self.catalogue),
            "the page does not render one searchable row per station",
        )
        # Unescaped, because a station called "I♥MUSIC & CHILL" is written into
        # the markup as `&amp;` — and the row a visitor searches for is the one
        # the *browser* holds, which is the unescaped one.
        text = html.unescape(body).lower()
        for station in self.catalogue[:20]:
            with self.subTest(station=station.name):
                self.assertIn(
                    station.name.lower(),
                    text,
                    "a station in the table has no row to be found by",
                )

    def test_the_cap_is_the_number_the_page_says_it_is(self):
        """Exactly ``STATION_PREVIEW`` rows are shown, and the sentence agrees.

        The count is one value (``keep_alive.STATION_PREVIEW``) that the template
        marks rows with and prints; a page whose sentence and whose rows disagree
        is the defect this whole file keeps meeting, one number apart.
        """
        body = self._page()
        beyond = body.count('data-beyond="true"')
        preview = keep_alive.STATION_PREVIEW
        self.assertEqual(
            beyond, len(self.catalogue) - preview,
            "the rows marked as beyond the cap are not the ones the cap leaves",
        )
        self.assertIn(
            f"Showing the first {preview} of {len(self.catalogue)}",
            body,
            "the note under the list does not name what the list is holding",
        )

    def test_the_page_counts_what_the_catalogue_counts(self):
        """Three numbers, read from the index the bot's picker reads.

        The lede's counts were computed in the template from the rows it had
        sliced; they now come from the catalogue's own index, so the page cannot
        advertise a language the picker does not count.
        """
        body = " ".join(self._page().split())
        self.assertIn(
            f"{len(self.catalogue)} stations in {len(self.index.languages)} languages "
            f"across {len(self.index.countries)} countries",
            body,
            "the page's own three counts are not the catalogue's",
        )

    def test_the_two_menus_offer_the_catalogue_and_nothing_else(self):
        """The language and country options are the index's own names.

        A hand-kept list of countries is how a page offers a station the bot
        would refuse to play, which is the failure the registry readers exist to
        prevent — so the menus are read out of the table rather than written
        beside it.
        """
        body = self._page()

        def options(marker: str) -> set[str]:
            block = body.split(marker, 1)[1].split("</select>", 1)[0]
            return set(re.findall(r"<option value=\"([^\"]*)\"", block))

        languages = options("data-station-language")
        countries = options("data-station-country")
        # The first option is the "any" of each menu.
        self.assertEqual(languages - {""}, {name for name, _ in self.index.languages})
        self.assertEqual(countries - {""}, set(self.index.countries))

    def test_the_ad_free_tag_is_shown_where_the_table_holds_one(self):
        """The badge comes from the station's own tags, counted here.

        "Ad-free" is the one claim on this page a visitor might choose a station
        by, so it is counted rather than spot-checked: the page shows it exactly
        as often as the catalogue records it.
        """
        body = self._page()
        expected = sum(1 for station in self.catalogue if "ad-free" in station.tags)
        self.assertGreater(expected, 0, "the catalogue has no ad-free tag left")
        self.assertEqual(
            body.count('<span class="tag">ad-free</span>'),
            expected,
            "the page shows the ad-free tag a different number of times than the table holds it",
        )
        self.assertEqual(
            self.index.ad_free, expected,
            "the index counts a different number of ad-free stations than the rows do",
        )


class TheSiteIsOneDesign(unittest.TestCase):
    """Every page wears the same system, and none of them calls out."""

    def test_every_page_links_the_shared_stylesheet(self):
        """The sheet, and a version token on it.

        Matched as a prefix because the URL now carries `?v=` — and it has to:
        `/static/*` is served `immutable` for a year, so a stylesheet edit with a
        stable URL is a year of invisibility on the CDN. Asserting the bare
        `"` here was what made the version token impossible to add.
        """
        # The three site pages reach it through the layout they share, so what is
        # asked of them is that they reach it; the link itself is asserted on the
        # layout, once, where it now lives.
        for page in PAGES:
            path = ROOT / "templates" / page
            if not path.exists():
                continue  # archived with its rebuild pending; nothing to wear
            markup = path.read_text(encoding="utf-8")
            if page in SITE_PAGES:
                self.assertIn(
                    '{% extends "site_base.html" %}', markup,
                    f"{page} does not reach the shared design system",
                )
                continue
            with self.subTest(page=page):
                self.assertIn(
                    'href="/static/meow.css', markup,
                    f"{page} does not use the shared design system",
                )
                self.assertIn(
                    "asset_version('meow.css')", markup,
                    f"{page} links the shared sheet without a version token",
                )
        base = (ROOT / "templates" / BASE_TEMPLATE).read_text(encoding="utf-8")
        self.assertIn('href="/static/meow.css', base)
        self.assertIn("asset_version('meow.css')", base)

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
            path = ROOT / "templates" / page
            if not path.exists():
                continue  # archived with its rebuild pending; loads nothing
            source = path.read_text(encoding="utf-8")
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

        ``.nav a`` (a class *and* a type) outranks ``.btn--primary`` (a class), so
        "Add to Discord" rendered its label in the nav's grey on the accent fill —
        about 2.2:1, on the primary call to action.
        """
        sheet = (ROOT / "static" / "meow.css").read_text(encoding="utf-8")
        self.assertIn(".nav a.btn {", sheet)
        self.assertIn(".nav a.btn--primary {", sheet)
        self.assertIn("color: var(--text-invert)", sheet.split(".nav a.btn--primary {")[1])

    def test_motion_is_switched_off_when_it_is_asked_to_be(self):
        sheet = (ROOT / "static" / "meow.css").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion: reduce", sheet)
        # And the entrance must not leave the content invisible once its animation
        # is suppressed to nothing: the *resting* state of a revealed block is
        # visible, and only the script — which refuses to run under a reduced-motion
        # preference — ever marks one as waiting.
        resting = sheet.split("[data-reveal]")[1].split("}")[0]
        self.assertIn("opacity: 1", resting)
        self.assertIn("transform: none", resting)
        motion = (ROOT / "static" / "motion.js").read_text(encoding="utf-8")
        self.assertIn("if (reduced.matches || !(\"IntersectionObserver\" in window)) return;", motion)


class EveryPublicPageSharesOneFrame(unittest.TestCase):
    """The pages a signed-out visitor can reach, and the one fact they all state.

    Written after driving the built site in a browser, for three defects that
    came out of one cause — a route that spelled the frame out by hand instead
    of spreading :func:`keep_alive._site_context`:

      * `/pricing` printed **"15 systems and commands"**, having copied six frame
        names and left `command_count` behind, and its footer lost the number
        with it;
      * `/privacy`, `/terms` and `/ai` had no counts at all — the footer fell
        back to the generic sentence — and stated *"Nothing is kept longer than
        it is needed"* where the deployment keeps 365 days;
      * `/login` and `/owner/login` answered **500**:
        `TypeError: got multiple values for keyword argument 'oauth_enabled'`,
        the moment the context supplied a flag the route also spelled out.

    None of the three is visible in a template. All three are visible in a
    served page, which is what these read instead.
    """

    #: A browser's user agent: `/` answers a `curl` UA with one health line.
    BROWSER = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/120 Safari/537.36"

    #: Every address a signed-out visitor can land on.
    PUBLIC = (
        "/",
        "/systems",
        "/start",
        "/radio",
        "/pricing",
        "/privacy",
        "/terms",
        "/ai",
        "/login",
        "/owner/login",
    )

    def setUp(self):
        import re  # noqa: PLC0415 — this file imports it where it is used

        self.re = re
        self.client = keep_alive.app.test_client()

    def _get(self, path: str):
        return self.client.get(path, headers={"User-Agent": self.BROWSER})

    def test_every_public_address_answers(self):
        """A page that raises is not a page, and two of these did."""
        for path in self.PUBLIC:
            with self.subTest(path=path):
                self.assertEqual(self._get(path).status_code, 200, path)

    def test_every_public_page_states_the_deployments_own_counts(self):
        """One sentence, ten pages, and both numbers have to be in it.

        The footer states them from the registry, so a page whose context lost
        either number cannot render this sentence at all — it falls back to the
        generic one, which is exactly what `/pricing` did.
        """
        systems = keep_alive.features.system_count()
        # The number a visitor can *type*, which is what every page of this site
        # counts: the footer and `/pricing` print it beside lists whose rows are
        # commands, and printing the picker's smaller figure there is how the
        # front door came to say "95 commands" above a hundred and ten of them.
        commands = keep_alive.features.entry_count()
        pattern = self.re.compile(rf"{systems}\s+systems and\s+{commands}\s+commands")
        for path in self.PUBLIC:
            with self.subTest(path=path):
                body = self._get(path).get_data(as_text=True)
                self.assertRegex(body, pattern, f"{path} does not state the counts")

    def test_the_sign_in_pages_space_their_headline_from_its_sentence(self):
        """Both measured **0px** between the two, in a browser, at every width.

        The gap came from `.page-head`, and the sign-in pages are not one — they
        state their own headline and sentence, so the two sat flush and the
        first line a visitor reads was a heading with its explanation welded to
        the bottom of it. Text is CSS, so the guard is a rule rather than a
        rendered box: the sheet has to say the gap is there.
        """
        sheet = (ROOT / "static" / "pages.css").read_text(encoding="utf-8")
        rule = self.re.search(r"\.auth\s+\.lede\s*\{([^}]*)\}", sheet)
        self.assertIsNotNone(rule, "nothing spaces a sign-in page's lede")
        self.assertIn("margin-top", rule.group(1))

    def test_a_mistyped_address_is_the_sites_own_page(self):
        """Not Flask's bare default, which offers a visitor nothing to click.

        This is the most common error page anybody ever sees, and it was the
        least finished page on the site: Flask's own shell from this process, a
        hand-written document full of deleted classes on the static host.
        """
        response = self._get("/this-address-is-not-a-page")
        self.assertEqual(response.status_code, 404)
        body = response.get_data(as_text=True)
        self.assertIn("nothing at that address", body)
        for way in ("/systems", "/start", "/login"):
            self.assertIn(f'href="{way}"', body, f"the 404 offers no way to {way}")
        self.assertIn("meow.css", body, "the 404 arrived unstyled")


class EverySignedInPageSharesThatSameFrame(_DashboardFixture):
    """The dashboard's eleven pages, served the way a signed-in browser gets them.

    `EveryPublicPageSharesOneFrame` above was written for exactly this defect
    class and could not see this one, because it reads the addresses a
    *signed-out* visitor can reach: `_dashboard_page` was the one served group
    that never merged :func:`keep_alive._site_context`, and every test that
    pinned the dashboard read the *template* through a fixture instead of the
    route. An unset name in a template is empty rather than an error, so nothing
    failed while all eleven pages wore a header reading **"."** where the
    wordmark goes, an empty footer title, no counts sentence, no Support-the-bot
    link, and stated *"Nothing is kept longer than it is needed"* where every
    other page on the site says the deployment keeps messages
    :func:`keep_alive._retention_days` days.

    Found by opening `/dashboard` in a browser during a playtest, which is the
    only place it was ever visible: every file a test reads was correct, and the
    render call was what was short.
    """

    def setUp(self):
        super().setUp()
        self.name = keep_alive._bot_display_name()
        self.systems = keep_alive.features.system_count()
        self.commands = keep_alive.features.entry_count()
        self.days = keep_alive._retention_days()

    def body_of(self, path: str) -> str:
        self.sign_in()
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def test_every_dashboard_page_states_the_deployments_own_name_and_counts(self):
        """One frame, eleven pages, read off the wire rather than off a fixture.

        The fixture render (`tests.test_composer.render_dashboard`) is what every
        other dashboard test uses and is deliberately *not* used here: it passes
        `bot_name` itself, so it would have gone on being green through the whole
        of this defect — the page a visitor is served is the only place the
        missing frame shows.
        """
        for _spec, _label, path, _anchor in keep_alive.DASHBOARD_PAGES:
            with self.subTest(path=path):
                body = self.body_of(path)
                self.assertRegex(
                    body,
                    rf'<span class="brand__name">{re.escape(self.name)}',
                    f"{path} is served with no wordmark in the header",
                )
                self.assertIn(
                    f'<div class="foot__title">{self.name}</div>',
                    body,
                    f"{path} is served with an empty footer title",
                )
                self.assertRegex(
                    body,
                    rf"{self.systems}\s+systems and\s+{self.commands}\s+commands",
                    f"{path} does not state the deployment's own counts",
                )
                self.assertIn(
                    f"Messages are kept {self.days} days",
                    body,
                    f"{path} states a retention the deployment does not have",
                )
                if keep_alive.KOFI_URL:
                    self.assertIn(
                        keep_alive.KOFI_URL,
                        body,
                        f"{path} lost the link that supports the bot",
                    )

    def test_the_system_cards_print_commands_a_person_can_type(self):
        """A card offered `//starboard set`, and a doubled slash is not a command.

        The registry writes every command **as a person types it**, leading slash
        included (`"/market buy"`), and the card's line prefixed one of its own —
        so all fifteen cards listed commands nobody can run, and the one command
        somebody could have copied out of the page was wrong. Nothing failed: the
        doubled slash is valid text, and this is the page an administrator reads
        to decide what a system does.

        Read off the served page rather than off the template, for the reason the
        test above gives: a source that looks right is not a page that renders
        right. The second half is the tie that keeps the fix honest — the name
        after each slash has to be a root the registry actually declares, so a
        card cannot start inventing commands either.
        """
        declared = {
            command.lstrip("/").split()[0]
            for feature in keep_alive.features.FEATURES
            for command in feature.commands
        }
        body = self.body_of("/dashboard/systems")
        blocks = re.findall(r'<p class="small mono">(.*?)</p>', body, re.DOTALL)
        self.assertGreaterEqual(len(blocks), 10, "the cards printed no commands at all")
        seen = 0
        for block in blocks:
            with self.subTest(block=block.strip()[:60]):
                # Only the tokens that *are* commands: a command is one whitespace-
                # separated word beginning with a slash, and the note after the em
                # dash is prose that may contain a slash of its own ("win/loss").
                for token in block.split():
                    if not token.startswith("/"):
                        continue
                    seen += 1
                    self.assertFalse(
                        token.startswith("//"),
                        "a card prints a command with two slashes, which is not a "
                        "command and not one a copy-paste would fix",
                    )
                    self.assertIn(
                        token.lstrip("/"),
                        declared,
                        "a card prints a command the registry does not declare",
                    )
        self.assertGreater(seen, 30, "no commands were read off the cards at all")


class TheFrameIsOneFunctionEverywhere(unittest.TestCase):
    """A cheap floor under the law above, so a *new* page group cannot skip it.

    The behavioural test can only walk the pages that exist today; this reads
    `keep_alive`'s own source and refuses a `render_template` call for any
    template served through the shared layout unless that call reaches
    `_site_context()` — directly, or merged into a dict the way the console and
    the dashboard now do. The dashboard's render was the one call in the file
    that did neither, and this is the check that would have said so the day it
    was written.
    """

    #: Templates served through `site_base.html`, one page group each.
    SERVED = (
        "dashboard.html",
        "owner.html",
        "servers.html",
        "market.html",
        "agree.html",
        "login.html",
        "owner_login.html",
        "pricing.html",
    )

    def test_every_served_group_reaches_the_shared_frame(self):
        source = (ROOT / "keep_alive.py").read_text(encoding="utf-8")
        for template in self.SERVED:
            with self.subTest(template=template):
                pattern = re.compile(
                    rf'render_template\(\s*"{re.escape(template)}",(?P<body>.*?)\n    \)',
                    re.DOTALL,
                )
                found = list(pattern.finditer(source))
                self.assertTrue(found, f"no render_template call for {template}")
                for call in found:
                    self.assertIn(
                        "_site_context(",
                        call.group("body"),
                        f"the {template} render is served without the site's own "
                        "frame — the wordmark, the footer title, the counts "
                        "sentence and the retention sentence all come from "
                        "`_site_context()`, and an unset name in a template is "
                        "empty rather than an error",
                    )


class TheHarnessServesTheRealPages(unittest.TestCase):
    """The preview must not restate a page it exists to show.

    Two defects in this file's neighbourhood came from exactly that: a harness
    that describes a route answers differently from the route.
    """

    def test_the_preview_uses_the_real_views(self):
        source = (ROOT / "scripts" / "preview_dashboard.py").read_text(encoding="utf-8")
        for name in ("home", "policy_privacy", "policy_terms", "login"):
            self.assertIn(f"keep_alive.{name}", source, f"the harness restates /{name}")

    def test_the_previews_own_refusal_escapes_the_way_production_does(self):
        """A hand-rolled escape chain here double-escaped, one line from the bug.

        The harness's 404 restated production's sentence, and where production
        calls ``_html.escape`` the copy chained ``.replace()``s with ``&`` last,
        so ``a<b`` was served as ``a&amp;lt;b``. A misspelt symbol is the only
        input that route ever reports, which means the harness was wrong on
        every use it had. Production's own route was already correct.
        """
        source = (ROOT / "scripts" / "preview_dashboard.py").read_text(encoding="utf-8")
        self.assertIn("html.escape(str(symbol), quote=True)", source)
        self.assertNotIn('.replace("&", "&amp;")', source)

    def test_the_preview_can_serve_the_shared_stylesheet(self):
        """It could not: the script runs from ``scripts/``, so Flask's default
        static folder was ``scripts/static`` and every page in the preview came
        out unstyled."""
        source = (ROOT / "scripts" / "preview_dashboard.py").read_text(encoding="utf-8")
        self.assertIn('static_folder=os.path.join(ROOT, "static")', source)

    def test_the_preview_hands_over_the_frame_it_shows(self):
        """The harness drew the site frame without ever being handed it.

        ``/dashboard`` and ``/market/<symbol>`` were rendered with the page's own
        context alone, so every name the frame owns arrived unset — and an unset
        name in a template is empty rather than an error. The wordmark came out as
        a **lone "."**, the ``<title>`` ended on its own separator, and the footer
        had no title, no counts sentence and no retention line, while all of that
        was correct on the routes themselves. It is the same defect the route was
        repaired for one level down, and it is worse in a harness, whose entire
        job is to be the thing somebody looks at to decide whether the page is
        right. Every render of a page that wears the frame merges
        ``_site_context()``, exactly as the route does.
        """
        source = (ROOT / "scripts" / "preview_dashboard.py").read_text(encoding="utf-8")
        for template in ("dashboard.html", "market.html"):
            with self.subTest(template=template):
                pattern = re.compile(
                    rf'render_template\(\s*"{re.escape(template)}",(?P<body>.*?)\n    \)',
                    re.DOTALL,
                )
                found = list(pattern.finditer(source))
                self.assertTrue(found, f"no render_template call for {template}")
                for call in found:
                    self.assertIn(
                        "keep_alive._site_context(",
                        call.group("body"),
                        f"the preview renders {template} without the frame the "
                        "route hands it, so the wordmark, the title, the counts "
                        "sentence and the retention line are all drawn from names "
                        "nobody sent",
                    )


if __name__ == "__main__":
    unittest.main()
