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
    "owner.html": "the owner console",
    "legal.html": "a policy page",
    "pricing.html": "the pricing page",
}


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

    def test_the_command_palette_does_not_offer_a_page_that_has_nothing_on_it(self):
        """The palette is a script literal, so it is in the gated template too.

        It is the one money link that is not an `<a>` — a route name pushed into
        a JS array — so the `href` assertion above cannot see it and it needs its
        own. It was the last one left after the topbar's button was gated.
        """
        off = _render("dashboard.html", selling=False)
        self.assertNotIn('window.location.href = "/pricing"', off)
        on = _render("dashboard.html", selling=True)
        self.assertIn('window.location.href = "/pricing"', on)

    def test_gating_a_js_array_entry_cannot_leave_a_dangling_comma(self):
        """The risk this gate introduced, in the one place it could have killed the page.

        The palette's entries are a JavaScript array literal, so gating one with
        `{% if selling %}` puts a conditional comma in the middle of the page's
        own script. Get it wrong in the *off* direction and the array ends
        `}, ];` — a syntax error that takes the whole script with it, which on
        this page means no composer, no switches, no palette and no error
        anywhere a server-side test looks. The rendered array is therefore
        checked both ways, and this assertion was written before the browser
        check that duplicated it: `node --check` on every inline block of the
        rendered page, which passes in both states.
        """
        for selling in (False, True):
            with self.subTest(selling=selling):
                html = _render("dashboard.html", selling=selling)
                array = re.search(
                    r"const actions = \[(.*?)\n\s*\];", html, re.S
                )
                self.assertIsNotNone(array, "the palette's action array moved")
                body = array.group(1)
                self.assertNotRegex(
                    body,
                    r",\s*$",
                    "the action array ends with a comma, which is a syntax error "
                    "in every browser",
                )
                self.assertIn("Choose another server", body)
                if selling:
                    self.assertIn("Plans and pricing", body)
                else:
                    self.assertNotIn("Plans and pricing", body)

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

    def test_each_template_that_can_show_the_link_is_given_the_flag(self):
        for call in self.calls:
            match = re.search(r'"([a-z_]+\.html)"', call)
            if not match or match.group(1) not in SURFACES:
                continue
            name = match.group(1)
            # Not every rendering of a page can show the topbar: a template
            # rendered for a fragment would legitimately omit it. The two that
            # must carry the flag are the ones whose chrome links to Pricing,
            # and the flag is harmless everywhere else — so the rule is simply
            # that these templates are never rendered without it.
            self.assertIn(
                "selling=",
                call,
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
