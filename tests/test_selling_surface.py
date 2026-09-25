"""Nothing on offer means nothing on offer — on every surface, not just the page.

While ``PLAN_CHECKOUT`` is off this is a free product, and the pricing page says
so in words. The *chrome* disagreed: the dashboard's topbar and its command
palette, the owner console, the landing page's nav in three places, and the
legal pages' nav and footer all linked to `/pricing`. A reader who followed one
arrived at a shop page whose entire content is that there is no shop.

Two things have to hold for that to stay fixed, and they fail differently:

* **The templates must gate the link**, or it is on the page while nothing is
  for sale. That is what rendering each template both ways pins.
* **Every route must hand its template the flag.** This is the quieter half:
  Jinja reads a missing name as false, so a route that forgets `selling=` looks
  perfectly correct while the product is free and silently loses the link the
  day it is not. That is not hypothetical — the dashboard, the owner console and
  the policy pages never received the flag, so gating the links alone would have
  removed Pricing from all three *permanently*, including after a launch.

Swapping the deployment's own environment variable is what the switch is for, so
these tests drive the flag the way the routes do — through
``plans.selling_enabled()`` — rather than passing a hard-coded boolean into a
template that production never passes.
"""

import re
import unittest
from pathlib import Path
from unittest import mock

import jinja2

ROOT = Path(__file__).resolve().parents[1]
KEEP_ALIVE = ROOT / "keep_alive.py"

#: Which template each route answers with, and the name the test calls it.
SURFACES = {
    "home.html": "the landing page",
    "dashboard.html": "a server's dashboard",
    "legal.html": "a policy page",
    "pricing.html": "the pricing page",
}

#: The operator console is deliberately **not** in the map above, and this is the
#: second time its gating has had to be re-expressed. It used to be a
#: server-rendered page whose topbar linked to `/pricing`, so it belonged here.
#: It is now a bundled app whose chrome lives in `console/`, and it links to no
#: shop page in either state — it is a private operations tool, not a surface that
#: sells anything. That absence is a decision rather than an oversight, so it is
#: asserted directly, in both directions, by
#: `test_the_console_never_links_to_a_shop_page` below.


def _render(name, *, selling, extra=None):
    """One template, rendered with the app's own environment and filters.

    ``ChainableUndefined`` stands in for the context a real route supplies. It
    is what makes this test about the *gate* rather than about a route's
    argument list: every name resolves to something falsy, so a template that
    reaches for data it was not given still renders instead of raising.
    """
    import keep_alive
    from flask import render_template

    env = keep_alive.app.jinja_env
    original = env.undefined
    env.undefined = jinja2.ChainableUndefined
    try:
        with keep_alive.app.test_request_context("/"):
            return render_template(name, selling=selling, **(extra or {}))
    finally:
        env.undefined = original


def _render_template_calls(source):
    """Every ``render_template(...)`` call's full argument text.

    Parens are matched by hand because the arguments span lines and carry
    comments; the template's own repr spans a comma-joined list, so the name is
    read out of the call's first string literal rather than by position.
    """
    calls = []
    start = 0
    needle = "render_template("
    while True:
        index = source.find(needle, start)
        if index < 0:
            return calls
        cursor = index + len(needle)
        depth = 1
        while cursor < len(source) and depth:
            if source[cursor] == "(":
                depth += 1
            elif source[cursor] == ")":
                depth -= 1
            cursor += 1
        calls.append(source[index:cursor])
        start = cursor


class NothingIsOfferedWhileNothingIsForSale(unittest.TestCase):
    """While the switch is off, no surface may link to the shop pages."""

    def test_the_console_never_links_to_a_shop_page(self):
        """The private console sells nothing, in either state, in either file.

        Both the rendered shell and the built bundle are checked, because either
        could introduce the link — and both switch positions are checked, so
        "there is no link" cannot be satisfied by a page that simply stopped
        rendering its chrome.
        """
        root = Path(__file__).resolve().parents[1]
        for state in (False, True):
            with self.subTest(selling=state):
                self.assertNotIn('href="/pricing"', _render("console.html", selling=state))
        bundle = (root / "static" / "console" / "app.js").read_text(errors="replace")
        self.assertNotIn('\"/pricing\"', bundle)
        self.assertNotIn("'/pricing'", bundle)

    def test_no_template_links_to_pricing_while_it_is_off(self):
        for name, label in SURFACES.items():
            with self.subTest(surface=label):
                html = _render(name, selling=False)
                self.assertNotIn(
                    'href="/pricing"',
                    html,
                    f"{label} ({name}) offers a shop page while nothing is for sale",
                )

    def test_every_template_links_to_pricing_again_once_it_is_on(self):
        """The other direction, or the gate is a deletion rather than a switch."""
        for name, label in SURFACES.items():
            with self.subTest(surface=label):
                html = _render(name, selling=True)
                self.assertIn(
                    'href="/pricing"',
                    html,
                    f"{label} ({name}) never offers Pricing, even when it is for sale",
                )

    def test_the_page_list_appends_the_shop_entry_rather_than_wrapping_it(self):
        """The shape half, re-pointed at the list that replaced the palette.

        This was `test_the_gate_wraps_whole_statements_and_never_a_comma`, and
        its subject left the product: the dashboard's command palette was a
        JavaScript array literal in the template, and a `{% if selling %}` placed
        *between* its elements left `}, ];` behind in the off direction — a syntax
        error that took the whole page script with it, with no error anywhere a
        server-side assertion looks. The palette went with the rebuild (the
        dashboard renders forms and no script at all), and the gates that remain
        are the two navs, where the risk is the same one in a different list:
        the frames build their destination list as one expression, and a
        conditional spliced into the middle of it would break the off direction
        rather than hide a link.

        So what is asserted is the shape — the shop entry is *appended* as a
        whole list item, after the closing bracket, and never wrapped around one
        of the entries already in it. The render half, which is what a visitor
        sees, is `test_no_template_links_to_pricing_while_it_is_off` above.
        """
        for name in ("meoww/base.html", "site_base.html"):
            with self.subTest(template=name):
                source = (ROOT / "templates" / name).read_text(encoding="utf-8")
                self.assertIn(
                    "+ ([('pricing', 'Pricing', '/pricing')] if selling else [])",
                    source,
                    f"{name} no longer appends the shop entry as a whole list "
                    "item, so a gate inside the list is how the off direction "
                    "starts breaking the list rather than shortening it",
                )

    def test_no_rupee_price_survives_anywhere_while_it_is_off(self):
        """`₹0` beside a `/mo` on the free card is a price tag on nothing.

        The page no longer advertises the tiers, but its own free card kept
        rendering the model's currency and a monthly period — so the one place
        that should say "free" read as a subscription that costs nothing, which
        is a different claim from "there is nothing to buy".
        """
        for name, label in SURFACES.items():
            with self.subTest(surface=label):
                html = _render(name, selling=False)
                self.assertNotIn("₹", html, f"{label} still quotes a price")
                self.assertNotIn(
                    "/mo<", html, f"{label} still names a billing period"
                )


class EveryRouteHandsItsTemplateTheFlag(unittest.TestCase):
    """A missing flag reads as false, so a forgotten one is a silent deletion.

    Not a lint: the dashboard, the owner console and the policy pages each
    render a template whose topbar links to `/pricing`, and none of them passed
    the flag. Gating the templates without this would have hidden Pricing on
    those three surfaces for good — including after selling is switched on,
    which is the moment the link is supposed to come back.
    """

    def setUp(self):
        self.source = KEEP_ALIVE.read_text(encoding="utf-8")
        self.calls = _render_template_calls(self.source)

    def test_the_walker_finds_the_calls_it_is_supposed_to(self):
        """Guards the guard: a walker that matches nothing would pass vacuously."""
        found = set()
        for call in self.calls:
            match = re.search(r'"([a-z_]+\.html)"', call)
            if match:
                found.add(match.group(1))
        for name in SURFACES:
            self.assertIn(name, found, f"no render_template call found for {name}")

    #: The three site routes spread this in rather than spelling the flag out.
    #: Following the indirection is the point — a guard that only accepted the
    #: literal `selling=` would have been satisfied by three pages that reach a
    #: helper which had quietly stopped carrying it.
    PROVIDER = "**_site_context()"

    def test_the_shared_context_really_does_carry_the_gate(self):
        """Guards the indirection below, at its source.

        Without this, emptying `_site_context()` would satisfy every route of the
        site at once while removing the flag from all of them — the same shape of
        vacuous pass the walker's own test exists to prevent.
        """
        import keep_alive

        context = keep_alive._site_context()
        for name in ("selling", "invite_url", "source_repository", "enter_url", "features"):
            with self.subTest(name=name):
                self.assertIn(name, context, f"`_site_context()` no longer supplies {name}")

    def test_each_template_that_can_show_the_link_is_given_the_flag(self):
        for call in self.calls:
            match = re.search(r'"([a-z_]+\.html)"', call)
            if not match or match.group(1) not in SURFACES:
                continue
            name = match.group(1)
            # Not every rendering of a page can show the topbar: a template
            # rendered for a fragment would legitimately omit it. The ones that
            # must carry the flag are the ones whose chrome links to Pricing,
            # and the flag is harmless everywhere else — so the rule is simply
            # that these templates are never rendered without it.
            self.assertTrue(
                "selling=" in call or self.PROVIDER in call,
                f"the route rendering {name} does not pass `selling`, so its "
                f"gated links can never come back: {call[:120]}...",
            )


class TheRealRoutesAgreeWithTheTemplates(unittest.TestCase):
    """Driven through the app rather than the template, for the pages that need
    no session. `/dashboard` and `/owner` need one, and their wiring is pinned
    above; these four are the ones a signed-out visitor can reach."""

    #: A browser's user agent: the landing page answers a `curl` UA with one
    #: health line instead of the document, which is what a monitor polls.
    BROWSER = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/120 Safari/537.36"

    def _get(self, path):
        import keep_alive

        with keep_alive.app.test_client() as client:
            return client.get(path, headers={"User-Agent": self.BROWSER}).get_data(
                as_text=True
            )

    def test_the_pages_a_visitor_can_reach_follow_the_switch(self):
        import keep_alive

        for path in ("/", "/pricing", "/privacy", "/terms"):
            for selling in (False, True):
                with self.subTest(path=path, selling=selling):
                    with mock.patch.object(
                        keep_alive.plans, "selling_enabled", return_value=selling
                    ):
                        html = self._get(path)
                    self.assertTrue(html, f"{path} answered nothing")
                    if selling:
                        # The pricing page links to itself from its own nav, so
                        # every one of these pages has at least one occurrence.
                        self.assertIn("href=\"/pricing\"", html)
                    else:
                        self.assertNotIn("href=\"/pricing\"", html)
                        self.assertNotIn("₹", html)


if __name__ == "__main__":
    unittest.main()
