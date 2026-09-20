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
"""
import os
import re
import unittest

from jinja2 import Environment, FileSystemLoader, select_autoescape

from utils import market as market_module

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "templates", "market_live.html")


def render_page(symbol="BTC"):
    """Render the shipped template exactly as the ``/market/<symbol>`` route does."""
    instrument = market_module.BY_SYMBOL[symbol]
    env = Environment(
        loader=FileSystemLoader(os.path.join(ROOT, "templates")),
        autoescape=select_autoescape(["html"]),
    )
    # Handed over because the template asks the *app* for it: `/static/*` is
    # cached immutable for a year, so the stylesheet link carries a token that
    # changes with the file. A bare environment has no such global, and the
    # page would fail to render here while working in production.
    import keep_alive

    env.globals["asset_version"] = keep_alive.asset_version
    return env.get_template("market_live.html").render(
        symbol=instrument.symbol,
        name=instrument.name,
        kind=instrument.kind,
        periods=list(market_module.PERIODS.items()),
        default_period=market_module.DEFAULT_PERIOD,
    )


def inline_script(html):
    """The page's own JavaScript, without the chart library's ``<script src>``."""
    blocks = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert blocks, "the page ships no inline script"
    return blocks[-1]


class TestMarketPageRenders(unittest.TestCase):
    def test_route_context_is_enough(self):
        html = render_page()
        self.assertIn("BTC", html)
        self.assertIn(market_module.BY_SYMBOL["BTC"].name, html)
        for period in market_module.PERIODS.values():
            self.assertIn(period.label, html)

    def test_both_apis_are_called(self):
        script = inline_script(render_page())
        self.assertIn("/api/market/quotes", script)
        self.assertIn("/api/market/candles", script)

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
        urls = re.findall(r'<script src="([^"]+)"', html)
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
        script = inline_script(render_page())
        # The quote poll is issued by the bootstrap, not from inside the chart
        # setup, so prices are already in flight while a fallback CDN is tried.
        body = script.split("async function startChart()", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("loadQuotes", body)
        self.assertIn("loadQuotes();", script)

    def test_chart_library_absence_is_checked(self):
        script = inline_script(render_page())
        self.assertIn('typeof LightweightCharts === "undefined"', script)

    def test_authoring_a_chart_is_guarded(self):
        script = inline_script(render_page())
        self.assertIn("try {", script)
        self.assertIn("} catch (error) {", script)
        # A failed chart must be reported in the chart card, not drawn as nothing.
        self.assertIn("chart-fallback", render_page())
        self.assertIn("chartNotice(", script)

    def test_a_cdn_that_never_answers_is_a_deadline_not_a_wait(self):
        """A blocked CDN fails; a hung one would otherwise stall the page."""
        script = inline_script(render_page())
        loader = script.split("function loadScript(", 1)[1].split("\n}", 1)[0]
        self.assertIn("setTimeout(", loader)
        self.assertIn("clearTimeout(", loader)

    def test_a_hung_request_cannot_say_loading_forever(self):
        """The page's fetch has a timeout, so "Loading live prices…" is temporary."""
        script = inline_script(render_page())
        self.assertIn("AbortController", script)
        self.assertIn("controller.abort()", script)
        self.assertNotIn('fetch("/api/market/quotes")', script)

    def test_the_chart_is_drawn_from_data_that_was_fetched_anyway(self):
        """Period move is computed before any drawing call, so it survives no chart."""
        script = inline_script(render_page())
        stats = script.index('el("periodMove").textContent')
        draw = script.index("candleSeries.setData(candles)")
        self.assertLess(stats, draw)


if __name__ == "__main__":
    unittest.main()
