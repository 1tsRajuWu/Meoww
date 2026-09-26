"""The market page must survive its only third-party request failing.

Reported bug: ``/market/BTC`` rendered the header, the period buttons and four
empty cards — "Loading live prices…", a blank chart, an empty symbol picker and
an empty watchlist. The deployed API was healthy (``/api/market/quotes`` answers
in ~1s); the page was dead because its chart library is loaded from a CDN with a
blocking ``<script>``, and when that one request did not arrive, the reference
error inside ``initChart()`` killed the rest of the same inline script. Nothing
below the chart ever ran, so the page could not even say what went wrong.

An empty rectangle is indistinguishable from "the market has no prices", which is
the one thing a price page must never say. These tests pin the properties that
made the failure loud instead of silent.

The page has since grown a shell (trading-site pacing: a rail of instruments, a
chart, a ticket, the readings) and its behaviour moved out of an inline
``<script>`` into ``static/market.js``, which is cached, reviewable and pinned by
content token like every other first-party script. The properties below are the
same ones: prices never wait on the chart, the chart's absence is stated in
words, the shared motion layer is driven rather than re-implemented, and the only
third-party URL is version-pinned. ``page_script()`` follows the page's own
``<script src>`` list rather than reading a filename, so a page that quietly
stopped loading its script fails here instead of passing on the strength of a
file nobody links.
"""
import os
import re
import unittest

from jinja2 import Environment, FileSystemLoader, select_autoescape

from utils import market as market_module

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render_page(symbol="BTC"):
    """Render the shipped template exactly as the ``/market/<symbol>`` route does.

    The context comes from ``keep_alive.market_context`` — the route's own
    function — so the page cannot be rendered here with more than production
    hands it. A bare Jinja environment is deliberate: it has no Flask filters and
    no app globals, which is how a template that reaches for one is caught here
    rather than in production.
    """
    import keep_alive

    instrument = market_module.BY_SYMBOL[symbol]
    env = Environment(
        loader=FileSystemLoader(os.path.join(ROOT, "templates")),
        autoescape=select_autoescape(["html"]),
    )
    # Handed over because the template asks the *app* for it: `/static/*` is
    # cached immutable for a year, so every asset link carries a token that
    # changes with the file. A bare environment has no such global, and the page
    # would fail to render here while working in production.
    env.globals["asset_version"] = keep_alive.asset_version
    env.globals["brand"] = keep_alive.brand
    return env.get_template("market.html").render(**keep_alive.market_context(instrument))


def script_urls(html):
    return re.findall(r'<script src="([^"]+)"', html)


def first_party(html, kind):
    """Every first-party asset of one kind the page loads, concatenated.

    Read through the page's own links rather than from a filename, so a page that
    quietly stopped loading an asset fails here instead of passing on the
    strength of a file nobody links.
    """
    pattern = r'<script src="([^"]+)"' if kind == "script" else r'<link[^>]+href="([^"]+)"'
    urls = re.findall(pattern, html)
    local = [u for u in urls if not u.startswith(("http://", "https://", "//"))]
    assert local, f"the page loads no first-party {kind}"
    parts = []
    for url in local:
        path = os.path.join(ROOT, url.lstrip("/").split("?", 1)[0])
        assert os.path.exists(path), f"{url} is linked but does not exist"
        with open(path, encoding="utf-8") as handle:
            parts.append(handle.read())
    return "\n".join(parts)


def page_script(html=None):
    """The page's own JavaScript: every first-party script it loads, concatenated."""
    return first_party(render_page() if html is None else html, "script")


def page_styles(html=None):
    """The page's own CSS: every first-party stylesheet it loads, concatenated."""
    return first_party(render_page() if html is None else html, "style")


def function_body(script, header):
    """The body of one function, found by balancing braces.

    The tests below care about what a *function* does, and the old
    ``split("\n}", 1)`` cut ran to the end of the file whenever a closing brace
    happened to sit at column zero — which is how a bootstrap call ended up
    inside the chart function it was being contrasted with.
    """
    start = script.index(header) + len(header)
    open_at = script.index("{", start)
    depth = 0
    for index in range(open_at, len(script)):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[open_at : index + 1]
    raise AssertionError(f"{header} is never closed")


class TestMarketPageRenders(unittest.TestCase):
    def test_route_context_is_enough(self):
        html = render_page()
        self.assertIn("BTC", html)
        self.assertIn(market_module.BY_SYMBOL["BTC"].name, html)
        for period in market_module.PERIODS.values():
            self.assertIn(period.label, html)

    def test_the_board_is_rendered_by_the_server(self):
        """The rail is on the page before any script runs.

        A visitor with a blocked or broken script still gets the instruments and
        the link to each one, which is the difference between a market that is
        empty and a market that failed to load.
        """
        html = render_page()
        for symbol in ("BTC", "AAPL", "SPY"):
            with self.subTest(instrument=symbol):
                self.assertIn(f'data-instrument="{symbol} ', html)
                self.assertIn(f'href="/market/{symbol}"', html)

    def test_the_rail_folds_every_instrument_and_opens_only_the_pages_own(self):
        """Seventy-two rows in one flat list ran the rail the whole length of the page.

        Reported: the instrument list was a single run that reached past the
        bottom of the viewport and kept going, so a reader on a phone scrolled
        about two thousand pixels of prices to reach the watchlist — a rail that
        reads as a progress bar down the side of the page rather than as a list.
        Each registry group is a `<details>` now and the group holding *this*
        page's instrument is the one that arrives open.

        Asserted as relations rather than as a shape, because a fold is a claim
        about the list: the count in a summary has to be the number of rows
        actually inside it (a count nobody can check against its own list is the
        worst version of this control), every instrument the registry declares
        has to still be on the page (a fold that drops one is a broker that
        stopped listing it), and the open fold has to be the one containing the
        symbol the page is about. The bound on the open fold's height is the whole
        point of it, so it is read out of the page's own stylesheet.
        """
        for symbol in ("BTC", "AAPL"):
            with self.subTest(symbol=symbol):
                html = render_page(symbol)
                folds = re.findall(
                    r'<details class="mk-group"([^>]*)>\s*'
                    r'<summary class="mk-group-title">\s*'
                    r'<span class="mk-group-name">([^<]+)</span>\s*'
                    r'<span class="mk-group-count">(\d+)</span>\s*</summary>\s*'
                    r'(<div class="mk-list">.*?)</details>',
                    html,
                    re.DOTALL,
                )
                self.assertGreater(len(folds), 1, "the rail is one list again, not folds")

                total = 0
                opened = []
                for attrs, label, count, body in folds:
                    rows = len(re.findall(r'data-instrument="', body))
                    total += rows
                    with self.subTest(group=label):
                        self.assertEqual(
                            int(count), rows, f"{label} says {count} and holds {rows}"
                        )
                    if "open" in attrs:
                        opened.append(label)
                self.assertEqual(
                    total,
                    len(market_module.INSTRUMENTS),
                    "an instrument the registry declares is missing from the rail",
                )
                self.assertEqual(
                    opened,
                    [
                        label
                        for _attrs, label, _count, body in folds
                        if f'data-instrument="{symbol} ' in body
                    ],
                    "the fold that arrived open is not the one this page is about",
                )

                block = re.search(r"\.mk-list\s*\{(.*?)\}", page_styles(html), re.DOTALL)
                self.assertIsNotNone(block, "the rail's list is no longer styled")
                self.assertRegex(
                    block.group(1), r"max-height:\s*\S+", "an unbounded fold is a page again"
                )
                self.assertRegex(block.group(1), r"overflow-y:\s*auto")

    def test_both_apis_are_called(self):
        script = page_script()
        self.assertIn("/api/market/quotes", script)
        self.assertIn("/api/market/candles", script)

    def test_it_uses_the_shared_motion_layer(self):
        """This page had none of it, which is why it read as a wall of numbers.

        The same declarative layer the landing page and the dashboard drive —
        `data-reveal`, `data-stagger`, `data-hover` — plus the drawer pair. The
        pairings are what is checked, not the attributes: a `data-drawer` whose
        id names an element that does not exist is a panel that never opens, and
        it looks like a styling choice rather than a broken control.
        """
        html = render_page()
        self.assertIn("/static/motion.js", html)
        for hook in ("data-reveal", "data-stagger", "data-hover"):
            with self.subTest(hook=hook):
                self.assertIn(hook, html)

        targets = re.findall(r'data-drawer="([^"]+)"', html)
        self.assertTrue(targets, "the page declares no drawer")
        for target in targets:
            with self.subTest(drawer=target):
                self.assertRegex(html, 'id="%s"' % re.escape(target))
                self.assertRegex(
                    html,
                    r'data-drawer="%s"[^>]*aria-expanded="false"' % re.escape(target),
                )
        # The drawer pair is wired from the script rather than left as markup.
        self.assertIn("MeowAnim.panel", page_script(html))

    def test_external_scripts_are_pinned(self):
        """An unpinned URL could change the library under a live page.

        Two kinds of pin, because there are two kinds of script and the rule was
        written when this page had only one of them. A **third-party CDN** is
        pinned by the version in its own URL — the only lever that URL offers —
        and a **first-party asset** is pinned by the content token
        ``asset_version()`` derives from the file, which is the stronger promise:
        it cannot be forgotten on an edit. The old assertion demanded the semver
        of both, so the first same-origin script this page gained failed a test
        about CDN versioning while being *better* pinned than the CDNs it sat
        beside. Spelled out here rather than loosened, so an unpinned URL of
        either kind still fails.
        """
        html = render_page()
        urls = script_urls(html)
        self.assertTrue(urls)
        third_party = [u for u in urls if u.startswith(("http://", "https://", "//"))]
        self.assertTrue(third_party, "the chart libraries are no longer loaded from a CDN")
        for url in third_party:
            self.assertRegex(url, r"@\d+\.\d+\.\d+", f"{url} is not version-pinned")
        local = [u for u in urls if u not in third_party]
        self.assertTrue(local, "this page loads no first-party script")
        for url in local:
            self.assertRegex(url, r"^/static/[\w.-]+\?v=[0-9a-f]+$", f"{url} is not content-pinned")


class TestChartFailureIsLoud(unittest.TestCase):
    """The chart is optional; the numbers are not."""

    def test_prices_do_not_wait_on_the_chart(self):
        script = page_script()
        # The quote poll is issued by the bootstrap, not from inside the chart
        # setup, so prices are already in flight while a fallback CDN is tried.
        body = function_body(script, "async function startChart()")
        self.assertNotIn("loadQuotes", body)
        self.assertIn("loadQuotes();", script)

    def test_chart_library_absence_is_checked(self):
        script = page_script()
        self.assertIn('typeof LightweightCharts === "undefined"', script)

    def test_authoring_a_chart_is_guarded(self):
        script = page_script()
        self.assertIn("try {", script)
        self.assertIn("} catch (error) {", script)
        # A failed chart must be reported in the chart card, not drawn as nothing —
        # and the notice's class must be one the page's own stylesheet styles, or
        # the explanation arrives as unstyled text somewhere in the layout.
        self.assertIn("mk-chart-fallback", script)
        self.assertIn(".mk-chart-fallback", page_styles())
        self.assertIn("chartNotice(", script)

    def test_a_cdn_that_never_answers_is_a_deadline_not_a_wait(self):
        """A blocked CDN fails; a hung one would otherwise stall the page."""
        loader = function_body(page_script(), "function loadScript(")
        self.assertIn("setTimeout(", loader)
        self.assertIn("clearTimeout(", loader)

    def test_a_hung_request_cannot_say_loading_forever(self):
        """The page's fetch has a timeout, so "Loading live prices…" is temporary."""
        script = page_script()
        self.assertIn("AbortController", script)
        self.assertIn("controller.abort()", script)
        self.assertNotIn('fetch("/api/market/quotes")', script)

    def test_the_chart_is_drawn_from_data_that_was_fetched_anyway(self):
        """The readings are written before any drawing call, so they survive no chart.

        Same data, same request: what makes the numbers robust is the *order* —
        the move, the source, the prints and the meter are all painted from the
        series before the chart is handed anything, and the drawing call is
        behind an early return for the case where there is no chart at all.
        """
        body = function_body(page_script(), "async function loadCandles()")
        printed = body.index("periodMove.textContent")
        drawn = body.index("candleSeries.setData(candles)")
        self.assertLess(printed, drawn)
        self.assertIn("if (!candleSeries || !volumeSeries || !chartObj) return;", body)


class ThePrintsTableIsReachableOnAPhone(unittest.TestCase):
    """A table of figures has a min-content floor; a wrapper is how you reach it.

    Measured at 320x740 on the deployed host: the five columns of `#history` — a
    monospace figure with 8px of padding on each side, five times, under
    uppercase headings — come to **358px inside the 278px panel** they sit in.
    `width: 100%` cannot go under that floor, so the table ran past its panel
    while the page's own `body { overflow-x: hidden }` clipped the two columns
    hanging over it: the Close column was absent, with no scrollbar and nothing
    on the page to say why. The document's own `scrollWidth` stayed at the
    viewport's, so nothing reported it — which is the shape of every defect this
    file is about.

    Read on the rendered page and on that page's own stylesheets together,
    because the fix is the pair: a wrapper in the markup, and one declaration for
    it. The same rule `.own-table__scroll` (the console) and `.table-wrap` (the
    hub) already follow.
    """

    def test_the_table_has_a_scroller_of_its_own(self):
        page = render_page()
        self.assertRegex(
            page,
            r'<div class="mk-prints-scroll">\s*<table class="mk-prints">',
            "the prints table is no longer inside the element that scrolls it",
        )
        self.assertIn(
            ".mk-prints-scroll { overflow-x: auto; }",
            page_styles(),
            "nothing scrolls the wrapper the prints table sits in, so the last "
            "columns of it are reachable at no width",
        )


if __name__ == "__main__":
    unittest.main()
