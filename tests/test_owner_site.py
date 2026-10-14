"""The owner site's build: a proxy that has to carry the pages' own assets.

``dacatowner.pages.dev`` is not a second site with its own pages — it is a
Cloudflare Pages project whose worker reverse-proxies everything to the bot's
host, with **one** exception: ``/static/``, which it answers from the CDN. So
every page this host serves is rendered by the backend and then loaded *here*,
where the only files that exist are the ones ``scripts/build_owner.py`` copied.

That is the whole shape of the defect these guards exist for. The per-server
dashboard came back from the proxy as an unstyled heading over one black
rectangle — ``dashboard.css``, ``motion.js`` and ``gateway.js`` were never
copied, so on this host alone they 404'd while answering 200 on Render. Nothing
reported it: a missing stylesheet is not an error in any console a person looks
at, and the page it breaks is the one an operator opens *from the console*.

Two claims are checked here, and the first is the one that would have caught it:

* **Every asset a template asks for is copied** — read out of the templates
  themselves rather than listed here, because a hand-written list of asset names
  is the same kind of second copy the worker's own docstring warns about.
* **The built directory actually contains them**, plus the bundle, the worker and
  its config — a list that is right and a copier that skips a missing file would
  otherwise pass the first claim and still ship nothing.
"""

import contextlib
import io
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
import scripts.build_owner as build_owner

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"

#: Templates that no page of *this* host can render, and so make no promise
#: about it. The hub is built for its own host out of `templates/hub/`, with
#: `-hub.css` and `hub.js`, and none of its pages are reachable through the
#: owner worker.
NOT_THIS_HOST = "hub"

#: An asset url a template asks the browser for: `/static/<file>`.
STATIC_URL = re.compile(r"/static/([A-Za-z0-9_.-]+)")


def asset_urls_in_templates() -> set[str]:
    """Every `/static/<file>` the templates name, hub pages excluded."""
    found: set[str] = set()
    for path in TEMPLATES.rglob("*.html"):
        if NOT_THIS_HOST in path.relative_to(TEMPLATES).parts:
            continue
        # ``{% include "theme_picker.html" %}`` pulls a partial in at render
        # time, and a partial may link an asset; rglob already reads every
        # template of this host, so the include needs no special handling — what
        # it does need is for the partial to be a `.html` file in here, which it
        # is. Stated because the reverse (a partial outside `templates/`) would
        # silently escape this walk.
        found.update(STATIC_URL.findall(path.read_text(encoding="utf-8")))
    return found


class EveryAssetThisHostServesIsCopied(unittest.TestCase):
    """The list in the builder is a contract with the templates."""

    def test_no_template_asks_for_an_asset_the_builder_does_not_copy(self):
        copied = set(build_owner.STATIC_ASSETS)
        # `console` is the bundle directory rather than a file, and it is copied
        # by its own branch of the build.
        asked = {name for name in asset_urls_in_templates() if name != "console"}
        missing = sorted(asked - copied)
        self.assertEqual(
            missing,
            [],
            "these assets are referenced by a template this host serves but are not "
            "copied into it, so they 404 on dacatowner.pages.dev and only there: "
            + ", ".join(missing),
        )

    def test_every_name_in_the_list_is_a_file_that_exists(self):
        """A name with no file is skipped silently by the copier, which is how a
        list can look complete while shipping nothing."""
        for name in build_owner.STATIC_ASSETS:
            with self.subTest(asset=name):
                self.assertTrue(
                    (ROOT / "static" / name).exists(),
                    f"scripts/build_owner.py lists {name}, which static/ does not have",
                )

    def test_the_dashboard_wears_its_own_sheet_and_its_own_script(self):
        """Named explicitly, because this is the page that was reported broken.

        The rule above covers it, and this says *which* page it was about — so a
        future rewrite of the walk cannot quietly stop covering the one case
        somebody actually hit: the per-server dashboard arriving from this proxy
        as an unstyled heading.

        What it wears changed when the dashboard moved onto the rebuilt frame,
        and this file had been left asserting the *retired* sheet set
        (``dashboard.css``, ``motion.js``, ``gateway.js``) — three files that are
        not in the tree any more. It reads the two templates now: the frame is
        where the frame's files are, the page is where the page's own are, and
        every one of them has to be copied by this host.
        """
        frame = (TEMPLATES / "meoww" / "base.html").read_text(encoding="utf-8")
        dashboard = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
        for name in ("meoww.css", "meoww.js"):
            with self.subTest(asset=name, from_="frame"):
                self.assertIn(f"/static/{name}", frame)
        # The rail's own script is asked for by the frame that draws the rail, and
        # by nothing else — the dashboard draws none, but it wears the frame.
        self.assertIn("/static/spine.js", frame)
        for name in ("meow.css", "pages.css"):
            with self.subTest(asset=name, from_="dashboard"):
                self.assertIn(f"/static/{name}", dashboard)
        for name in ("meoww.css", "meoww.js", "spine.js", "meow.css", "pages.css"):
            with self.subTest(asset=name, copied=True):
                self.assertIn(name, build_owner.STATIC_ASSETS)


class TheBuiltDirectoryIsTheSite(unittest.TestCase):
    """The build itself, into a throwaway directory."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="dacatowner-")
        build_owner.build(cls.dir, build_owner.DEFAULT_LIVE_APP)
        cls.root = Path(cls.dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_the_worker_and_its_config_are_written(self):
        for name in ("_worker.js", "_routes.json", "_headers"):
            with self.subTest(file=name):
                self.assertTrue((self.root / name).is_file())

    def test_the_static_path_is_answered_from_the_cdn_and_nothing_else(self):
        """The worker has to *stop* proxying `/static/`, or the CDN copies would
        be shadowed by the backend's — and the two would drift."""
        worker = (self.root / "_worker.js").read_text(encoding="utf-8")
        self.assertIn('url.pathname.startsWith("/static/")', worker)
        self.assertIn("env.ASSETS.fetch(request)", worker)
        routes = (self.root / "_routes.json").read_text(encoding="utf-8")
        self.assertIn("/static/*", routes)

    def test_every_copied_asset_is_really_there(self):
        for name in build_owner.STATIC_ASSETS:
            with self.subTest(asset=name):
                self.assertTrue((self.root / "static" / name).is_file())

    def test_the_console_ships_as_pages_rather_than_a_bundle(self):
        """The operator console is the backend's own pages now, and this host carries none.

        It used to be a bundled browser app — ``static/console/app.js`` plus its
        stylesheet and its build stamp — and this law required the three files to
        travel here. The app is archived with the rest of the pre-rebuild
        surfaces (``/owner`` renders one page per entry in ``OWNER_PAGES``), so
        the old assertion could only ever fail: it was asking this build to ship
        a directory the repository no longer contains. What is left that still
        matters is the *shape* — a bundle directory that reappeared without being
        copied, or a console page asking the CDN for a file it does not answer,
        is the 404-on-one-host defect this whole file exists for.
        """
        self.assertFalse(
            (self.root / "static" / "console").exists(),
            "a console bundle is back in the build without this host learning about it",
        )
        copied = set(build_owner.STATIC_ASSETS)
        owner_pages = [
            path for path in TEMPLATES.glob("owner*.html")
        ]
        self.assertTrue(owner_pages, "the console's own pages are gone")
        for path in owner_pages:
            for name in STATIC_URL.findall(path.read_text(encoding="utf-8")):
                with self.subTest(page=path.name, asset=name):
                    self.assertIn(name, copied)

    def test_it_refuses_to_write_into_the_repository_root(self):
        with self.assertRaises(ValueError):
            build_owner.build(str(ROOT), build_owner.DEFAULT_LIVE_APP)


class TheSignInPageWorksOnAPhone(unittest.TestCase):
    """The operator's front door, at the height a phone actually leaves it.

    Signing in from a phone means the keyboard is up, and the keyboard takes
    about half the viewport. Measured at 360x380 — a small phone with the
    keyboard showing — the sign-in card is 507px tall, its Unlock button ended at
    381px, one pixel below the fold, and the link back to the public site was off
    the page entirely. Neither could be scrolled to, because the template's own
    `body { overflow: hidden }` made the document unscrollable: measured, the
    largest scroll position was **0** against a document 555px tall.

    The rule was there to stop the tilted card from producing a scrollbar, which
    is a horizontal problem — `overflow-x: hidden` is what that guard wanted, and
    it still holds. The height is the other half: `100vh` on a phone is the
    height the page would have with the browser's own chrome hidden, so a card
    centred in it sits partly under the address bar.
    """

    def setUp(self):
        self.page = (TEMPLATES / "owner_login.html").read_text(encoding="utf-8")
        self.sheet = (ROOT / "static" / "pages.css").read_text(encoding="utf-8")

    def test_the_page_borrows_the_frame_instead_of_styling_itself(self):
        """The `<style>` block these laws were written against is gone, and that is the fix.

        The page used to carry its own rules — a `body`, a tilted card, a
        `.holo-input` — and the defect recorded above happened *inside* them:
        `body { overflow: hidden }` for the tilt made the document unscrollable,
        so on a 507px-tall phone the Unlock button and the link back to the
        public site could not be reached at all. It is a page of the shared frame
        now, wearing `pages.css` with every other sign-in, so the honest laws are
        about what it borrows and what that sheet promises. A template with no
        style block cannot take scrolling away from itself again.
        """
        self.assertIn('{% extends "site_base.html" %}', self.page)
        self.assertIn("/static/pages.css", self.page)
        self.assertNotIn(
            "<style",
            self.page,
            "the sign-in page styles itself again; every rule it owns is one no "
            "other page of this host can be fixed by",
        )

    def test_the_card_can_be_reached_on_a_phone(self):
        """The three claims the old height rules stood in for, read where they live now.

        Nothing pins the card to a viewport height, the pair of sign-in columns
        collapses to one, and no rule in the sheet that lays this page out takes
        scrolling away — which is the failure the phone test was really about.
        """
        self.assertNotIn("overflow: hidden", self.page)
        self.assertNotIn(
            "100vh",
            self.sheet,
            "a viewport-height box is a card centred in a height a phone does not "
            "have with its address bar showing",
        )
        self.assertIn(
            ".auth__inner { grid-template-columns: minmax(0, 1fr); }",
            self.sheet,
            "the sign-in columns no longer collapse, so the card is off the side "
            "of a phone instead of below the headline",
        )
        body_rule = re.search(r"\nbody \{([^}]*)\}", self.sheet)
        if body_rule:
            self.assertNotIn(
                "overflow: hidden",
                body_rule.group(1),
                "the shared sheet makes every page unscrollable",
            )

    def test_the_passkey_field_is_not_asked_to_shrink(self):
        """A field below 16px is the page zooming when it takes focus, on iOS.

        The retired template named 15px on its own `.holo-input`, and the rule
        that mattered was the sheet's — so what is pinned is what a reader gets:
        this page states no size for its own field, and no sheet hands a text
        field one under 16px. A browser's own size is 16px, which is why the
        second half is written as a floor rather than as a required declaration.
        """
        self.assertNotIn("font-size", self.page)
        for name in ("meow.css", "pages.css"):
            text = (ROOT / "static" / name).read_text(encoding="utf-8")
            for selector, body in re.findall(r"([^{}]*)\{([^{}]*)\}", text, flags=re.S):
                if "input" not in selector or "font-size" not in body:
                    continue
                with self.subTest(sheet=name, selector=selector.strip()[:40]):
                    self.assertIsNone(
                        re.search(r"font-size:\s*(1[0-5](\.\d+)?)px", body),
                        "a field below 16px zooms the whole page on iOS when it "
                        "takes focus",
                    )


class TheFourSitesLeaveThePhoneItsOwnHeight(unittest.TestCase):
    """A viewport height is declared twice — `vh` first, then `dvh`.

    This is the same defect the class above was written for, in the four sheets
    that *do* need a viewport-height box: a shell whose footer sits at the bottom
    of a short page. `100vh` is the height the page would have with the browser's
    own chrome hidden, so on a phone the shell's last line is drawn under the
    address bar — and in landscape, where the window is short and the chrome is
    tall, the box is taller than the window it is drawn in. `dvh` is the height
    the page actually has.

    The two units are a definition rather than a matter of taste — the difference
    between them *is* the browser's chrome — but what it costs when only the first
    is declared has been measured here twice: a sign-in card centred in `100vh` on
    a 360x380 phone ended 1px below the fold with the link under it off the page
    entirely (the class above), and the pre-rebuild sign-in page's own fix for
    this was the same two-line pair with the same reason
    (`Deleted/pre-rebuild-2026-09-24/old/owner/owner_login.html`). A `vh` line
    with no `dvh` after it is a box that fits the screen nobody is looking at, and
    nothing in a desktop window shows it.

    The five sheets below are the published sites' viewport-height boxes: the
    public site and the hub it serves (`meoww.css`, `hub/static/hub.css`), the
    console and the member dashboard, which share one frame (`meow.css`), the
    market (`market.css`), and the marketing frame's own shell
    (`site.css` — the `site-shell` that keeps a footer at the bottom of a short
    page). `pages.css` is missing from the list on purpose — it draws no
    viewport-height box at all, which the class above already asserts.
    """

    SHEETS = (
        ROOT / "static" / "meow.css",
        ROOT / "static" / "meoww.css",
        ROOT / "static" / "market.css",
        ROOT / "static" / "site.css",
        ROOT / "hub" / "static" / "hub.css",
    )

    def test_every_viewport_height_carries_its_fallback(self):
        """Both directions: no bare `vh`, and no `dvh` a browser can drop alone.

        The first is the defect. The second is its twin — a `dvh` with no `vh`
        above it leaves a browser that does not know the unit with **no** height
        at all, so a full-height rail silently becomes a box the height of its
        own contents, which is a different page rather than a degraded one.
        """
        declaration = re.compile(r"(min-height|height):\s*100vh;")
        for path in self.SHEETS:
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                found = declaration.search(line)
                if found is not None:
                    with self.subTest(sheet=path.name, line=index + 1):
                        self.assertEqual(
                            lines[index + 1].strip(),
                            f"{found.group(1)}: 100dvh;",
                            f"{path.name}:{index + 1} sizes a box in `100vh` and "
                            "never says the height a phone actually has",
                        )
                if "100dvh" in line:
                    with self.subTest(sheet=path.name, line=index + 1, fallback=True):
                        self.assertRegex(
                            lines[index - 1],
                            declaration,
                            f"{path.name}:{index + 1} asks for `dvh` with no `vh` "
                            "above it, which is no height at all where the unit "
                            "is unknown",
                        )

    def test_no_host_sizes_a_card_to_the_viewport(self):
        """The other half of the same judgement, on the sheets not listed above.

        A viewport height is right for a *shell* — a page whose footer waits at
        the bottom — and wrong for any box a person is expected to reach, which
        is what the sign-in card was: 507px tall, centred in a height a phone
        does not have with its keyboard up. The marketing layer is in the list
        above now (`site-shell` is a shell, with its `vh`/`dvh` pair), so what
        is left here is the one sheet that draws no viewport box at all.
        """
        for sheet in (ROOT / "static" / "pages.css",):
            with self.subTest(sheet=sheet.name):
                self.assertNotIn(
                    "vh;",
                    sheet.read_text(encoding="utf-8").replace("dvh;", ""),
                    f"{sheet.name} takes a viewport height, which is a box sized "
                    "to a screen the reader may not have",
                )


class TheWorkerServesTheConsoleAndNothingElse(unittest.TestCase):
    """The shape of the host, asserted on what the console actually calls.

    This host used to be a reverse proxy of the entire application, which is a
    shape with two defects in it, both since hit for real: a page it served but
    could not dress (the dashboard, whose stylesheet was never copied here), and a
    second door to the same room (`/login`, answering on this domain with its own
    passkey form). The list in the worker is what replaced "everything" — and a
    list is only as good as what is *not* on it, so this reads the console's own
    sources for the paths it calls rather than trusting the list to be complete.
    """

    #: A path literal in a console source file, or in a template this host
    #: serves: `/api/console/live`, `/api/toggle`, `/owner/servers`.
    CALLED_PATH = re.compile(
        r"""["'`](/(?:api|owner|login|logout)[A-Za-z0-9_./-]*)"""
    )

    def worker_prefixes(self) -> list[str]:
        """The list as the worker itself declares it."""
        worker = build_owner._worker_file("backend.test", "", "public.test")
        block = re.search(
            r"const CONSOLE_PREFIXES = \[(.*?)\];", worker, flags=re.DOTALL
        )
        self.assertIsNotNone(block, "the worker no longer declares what it serves")
        return re.findall(r'"([^"]+)"', block.group(1))

    # The exception list this class used to carry — `{"/login"}`, the member
    # door the console pointed at and this host answered with a 301 — is **gone**,
    # because the console's own pages no longer name it: the outward links are
    # absolute addresses now (`hub.dashboard_url()`, `hub.marketing_url()`), which
    # is what a link between two hosts has to be. So the law below is the
    # stronger one — every path the console's pages name is served *here* — and an
    # exception that comes back has to be justified in this comment.
    def paths_the_console_calls(self) -> set[str]:
        """Every path the console's own pages name.

        This read `console/src/**/*.ts(x)` and `templates/console.html` — the
        bundled app's TypeScript and its shell. Both are archived with the rest
        of the pre-rebuild surfaces, and a law that reads a directory the
        repository does not have cannot be satisfied by any change to the site:
        it fails at ``open()`` before it can say anything about this host. The
        console is the backend's pages now, so the sources are the templates it
        actually renders. ``keep_alive`` is deliberately *not* one of them — it
        holds every route in the application, so reading it would ask why the
        owner's proxy does not serve the per-server dashboard's endpoints.
        """
        found: set[str] = set()
        sources = sorted(TEMPLATES.glob("owner*.html"))
        self.assertTrue(sources, "the console's own pages are gone")
        for path in sources:
            for raw in self.CALLED_PATH.findall(path.read_text(encoding="utf-8")):
                found.add(raw.rstrip("/"))
        return found

    #: Both pages of the console's host: `/owner` and the door to it. One
    #: surface, one reader, so one nav — and the second of the two was the one
    #: left wearing the marketing header while this law read only the first.
    CONSOLE_PAGES = ("owner.html", "owner_login.html")

    #: The file that owns the console's nav, drawn by both pages above.
    NAV = "_owner_nav.html"

    def test_the_console_does_not_wear_the_public_sites_nav(self):
        """The frame, minus the part of it that made three hosts read as one.

        These pages extend `site_base.html`, so they inherit the public site's
        marketing header: Home, Systems, Radio, and a button inviting the reader
        to add the bot to Discord — on the fleet console, and on the page where
        somebody is trying to become the operator, from a session that is not a
        Discord one. That header is what the screenshots said was confusing, and
        the fix is a block the console overrides rather than a second frame, so
        the wordmark, the theme control, the footer and the retention line all
        stay one author.

        Both pages draw the override from `_owner_nav.html` now. The door was
        left out when the console's root was fixed — the same defect, on the
        other half of the same host — and the reason the markup is not simply
        written into both is this repository's oldest recorded failure: two
        copies of one list had already drifted by the time anyone derived the
        second from the first (see `scripts/build_owner.py`'s prefix lists).
        """
        macro = (TEMPLATES / self.NAV).read_text(encoding="utf-8")
        # What the nav says: which address this is, and where the two
        # member-facing halves live.
        self.assertIn("{% macro console_nav(site_url, dashboard_url) %}", macro)
        self.assertIn(
            "<a class=\"nav__link\" href=\"/\" aria-current=\"page\">Console</a>", macro
        )
        self.assertIn('href="{{ site_url }}"', macro)
        self.assertIn('href="{{ dashboard_url }}"', macro)
        self.assertIn("theme_button()", macro)
        # ...and what it must not say: the public site's marketing links (this
        # host serves none of them) and the invite button (a console is not
        # where anyone is deciding whether to add the bot).
        self.assertNotIn("Add to Discord", macro)
        self.assertNotIn("'/systems'", macro)
        # A default typed into the template was the second copy of an address
        # this file already had a reader for; both arrive as arguments now.
        self.assertNotIn("public_site_url", macro)
        self.assertNotIn("dacat.pages.dev", macro)
        for page in self.CONSOLE_PAGES:
            with self.subTest(page=page):
                body = (TEMPLATES / page).read_text(encoding="utf-8")
                self.assertIn(f'{{% from "{self.NAV}" import console_nav %}}', body)
                self.assertIn(
                    "{% block nav %}{{ console_nav(site_url, dashboard_url) }}"
                    "{% endblock %}",
                    body,
                )
                # The page draws the nav by name; it does not carry one. A
                # second copy is the drift the macro exists to make impossible.
                self.assertNotIn("nav__link", body)
        # And it is a block the frame actually renders, or the override is a
        # comment: `site_base.html` has to offer it.
        base = (TEMPLATES / "site_base.html").read_text(encoding="utf-8")
        self.assertIn("{% block nav %}", base)
        self.assertIn("{% endblock %}", base[base.index("{% block nav %}") :])

    def test_both_pages_are_told_where_the_other_two_hosts_are(self):
        """An unset name in a template is empty, not an error.

        `href="{{ site_url }}"` with `site_url` unset renders `href=""`, which
        is a link back to this page — and that went unnoticed for as long as it
        did because the default the template used to carry was a plausible
        address no render call ever set. So both readers are asserted where
        they are actually passed, not in prose about them.
        """
        source = (ROOT / "keep_alive.py").read_text(encoding="utf-8")
        for template in self.CONSOLE_PAGES:
            with self.subTest(template=template):
                call = re.search(
                    rf'render_template\(\s*"{re.escape(template)}",(?P<body>.*?)\n    \)',
                    source,
                    flags=re.DOTALL,
                )
                self.assertIsNotNone(call, f"no render_template call for {template}")
                self.assertIn("site_url=hub.marketing_url()", call.group("body"))
                self.assertIn("dashboard_url=hub.dashboard_url()", call.group("body"))

    def test_the_builder_and_the_worker_agree_about_what_is_served(self):
        self.assertEqual(self.worker_prefixes(), list(build_owner.CONSOLE_PREFIXES))

    def test_every_path_the_console_calls_is_one_this_host_serves(self):
        prefixes = self.worker_prefixes()
        called = self.paths_the_console_calls()
        self.assertGreater(len(called), 3, "the console's own calls were not found")
        uncovered = sorted(
            path
            for path in called
            if not any(path == p or path.startswith(p + "/") for p in prefixes)
        )
        self.assertEqual(
            uncovered,
            [],
            "the bundled console calls paths this host redirects away, so those "
            f"actions would leave the console: {uncovered}",
        )

    def test_the_sign_in_flow_has_nowhere_to_dead_end(self):
        """The door is a `Location` header, so no scan of the console can see it.

        `_door_target` is the clickwrap at the door: every route that signs
        somebody in answers a redirect to `/agree?next=…` before it answers
        anything else, and that string exists in exactly one place — the
        backend's own header. Nothing in `console/src/` names it and no template
        names it, which is why `test_every_path_the_console_calls_is_one_this_host_serves`
        was green while the operator's passkey dead-ended: they were signed in
        *on this host*, sent to `/agree` here, and this worker's answer to a path
        it does not recognise is a 301 to the public site — a host that had never
        heard of them, at a page offering no passkey field.

        The session is signed in on purpose: the door is only the agreement for a
        session that has not agreed, so read anonymously this asks about
        `/dashboard` and asserts nothing about the clickwrap.
        """
        with keep_alive.app.test_request_context():
            session["authenticated"] = True
            door = urllib.parse.urlsplit(keep_alive._door_target("/owner")).path
        self.assertEqual(
            door,
            "/agree",
            "a signed-in session is no longer sent to the clickwrap first, so this "
            "test is asserting about the wrong path",
        )
        prefixes = self.worker_prefixes()
        self.assertTrue(
            any(door == prefix or door.startswith(prefix + "/") for prefix in prefixes),
            f"a fresh sign-in is redirected to {door!r} and this host answers that "
            "path with a 301 to the public site, so the operator enters the right "
            "passkey and lands on another host, signed out, at a page with no "
            "passkey field on it",
        )

    def test_no_member_door_is_served_here(self):
        """`/login` is the second door. It is the one path that must not be here.

        Proxying that page is how this domain came to offer a passkey form whose
        limits and guards nobody was looking at, while the console behind it was
        being reviewed — and it is why this host's own door is `/owner/login`
        rather than the member site's.

        `/login/discord` is *also* not here, which is the half that used to be
        wrong. The list carried it for a Discord button the console does not have
        — the copy on `owner_login.html` mentions Discord, the page offers no
        button — and a door whose landing page is `/dashboard` (where the
        callback now sends a global session) is a door *this* host answers with a
        301 to the public site. So a hand-typed start on this host ended on
        another host, signed out, at a page with no passkey field on it: the same
        dead end the `/agree` prefix was added for, arriving from the other side.
        """
        prefixes = self.worker_prefixes()
        for member_door in ("/login", "/login/discord", "/servers"):
            with self.subTest(path=member_door):
                self.assertNotIn(member_door, prefixes)
                self.assertFalse(
                    any(
                        member_door == prefix or member_door.startswith(prefix + "/")
                        for prefix in prefixes
                    ),
                    f"this host would answer the member door {member_door}",
                )
        # The door this host does have, and it is its own: the console's page.
        self.assertTrue(
            any("/owner/login" == prefix or "/owner/login".startswith(prefix + "/") for prefix in prefixes)
        )

    def test_the_root_is_the_console_and_not_a_redirect(self):
        """The door the accept button comes back through.

        Every way in on this host ends at the clickwrap, and the clickwrap's own
        answer is a redirect *back to this host's root* — ``Location: /owner``,
        which the worker rewrites to ``/``. So the last step of signing in is a
        request for ``/``, and until this was checked nothing could see what that
        request was answered with: the worker *explained* that the root is the
        console in a comment sitting **below** the guard that redirects anything
        not in ``CONSOLE_PREFIXES`` to the public site, and ``/`` is not a prefix,
        so the code was unreachable and the operator — passkey accepted, terms
        accepted — was handed to the marketing page, signed out.

        A text search cannot see the order, which is why this drives the built
        worker in a real JS engine instead: every path below is asked of the
        worker itself, and the answer is the *decision* (proxied, its own file, or
        a 301 off this host) rather than the presence of a line of source.
        """
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")

        out = Path(tempfile.mkdtemp(prefix="dacatowner-routes-"))
        try:
            build_owner.build(str(out), "backend.test", "public.test")
            worker_js = out / "_worker.js"
            harness = out / "routes.mjs"
            harness.write_text(
                "const worker = (await import('./_worker.js')).default;\n"
                "const seen = [];\n"
                "globalThis.fetch = async (url) => {\n"
                "  seen.push(url instanceof Request ? url.url : String(url));\n"
                "  return new Response('backend', { status: 200 });\n"
                "};\n"
                "const env = { ASSETS: { fetch: async () =>\n"
                "  new Response('asset', { status: 200 }) } };\n"
                "const paths = JSON.parse(process.argv[2]);\n"
                "const out = [];\n"
                "for (const path of paths) {\n"
                "  seen.length = 0;\n"
                "  const res = await worker.fetch(new Request('https://owner.test' + path), env);\n"
                "  out.push({ path, status: res.status,\n"
                "    location: res.headers.get('Location'), asked: seen[0] || null });\n"
                "}\n"
                "console.log(JSON.stringify(out));\n",
                encoding="utf-8",
            )
            paths = ["/", "/index.html", "/owner", "/agree?next=/owner", "/static/console/app.js"]
            result = subprocess.run(
                [node, str(harness), json.dumps(paths)],
                capture_output=True,
                text=True,
                cwd=str(out),
                timeout=60,
            )
        finally:
            shutil.rmtree(out, ignore_errors=True)

        self.assertEqual(result.returncode, 0, result.stderr[:800])
        routed = {item["path"]: item for item in json.loads(result.stdout)}

        # The two roots: answered by the console, and rewritten to it.
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                self.assertEqual(routed[path]["status"], 200)
                self.assertIsNone(routed[path]["location"])
                self.assertEqual(
                    urllib.parse.urlsplit(routed[path]["asked"]).path,
                    "/owner",
                    "the root of this host is not the console",
                )

        # The rest of the console, unchanged, and the one door that must stay
        # shut: a path this host does not serve still leaves for the public site.
        for path in ("/owner", "/agree?next=/owner"):
            with self.subTest(path=path):
                self.assertEqual(routed[path]["status"], 200)
                self.assertIsNone(routed[path]["location"])
        self.assertIsNone(routed["/static/console/app.js"]["asked"], "an asset went to the backend")

    def test_the_build_refuses_to_serve_a_path_it_also_redirects(self):
        """The invariant behind the test above, checkable without a JS engine.

        Every list this worker consults before it decides is compared with the
        guard itself: a path a host *serves* must not be a path its catch-all
        redirect answers, and the redirect is named once so the two cannot
        drift the way they did — the root was handled below it and therefore
        never handled at all.
        """
        worker = build_owner._worker_file("backend.test", "", "public.test")
        roots = re.search(r"const ROOT_PATHS = \[(.*?)\];", worker, flags=re.DOTALL)
        self.assertIsNotNone(roots, "the worker no longer names the paths it roots")
        self.assertEqual(re.findall(r'"([^"]+)"', roots.group(1)), ["/", "/index.html"])
        self.assertIn(
            "if (!isConsole && !isRoot) {",
            worker,
            "the catch-all redirect ignores the root paths, so a request for the "
            "root leaves this host",
        )
        self.assertLess(
            worker.index("const isRoot = ROOT_PATHS.includes"),
            worker.index("status: 301"),
            "the root is worked out after the redirect that has to respect it",
        )

    def test_everything_else_is_redirected_to_the_public_site(self):
        worker = build_owner._worker_file("backend.test", "", "public.test")
        self.assertIn('const PUBLIC_SITE_HOST = "public.test";', worker)
        self.assertIn("if (!isConsole && !isRoot) {", worker)
        self.assertIn("status: 301", worker)
        self.assertIn("target.hostname = PUBLIC_SITE_HOST", worker)
        # Not a proxy: a 301 answers without asking the backend anything, which is
        # what makes the assets impossible to get wrong for those pages.
        self.assertIn('"Location": target.toString()', worker)

    def test_the_public_site_address_comes_from_the_environment(self):
        """A build that hardcoded its own answer would move neither link."""
        out = tempfile.mkdtemp(prefix="dacatowner-public-")
        said = io.StringIO()
        try:
            with mock.patch.dict(os.environ, {"OWNER_PUBLIC_SITE_URL": "https://site.test/"}):
                with contextlib.redirect_stdout(said):
                    build_owner.build(out, build_owner.DEFAULT_LIVE_APP)
            worker = (Path(out) / "_worker.js").read_text(encoding="utf-8")
        finally:
            shutil.rmtree(out, ignore_errors=True)
        self.assertIn('const PUBLIC_SITE_HOST = "site.test";', worker)
        self.assertIn("every other path is redirected to https://site.test", said.getvalue())

    def test_the_build_says_what_this_host_does(self):
        """The contract is printed, so a deploy log answers the question."""
        out = tempfile.mkdtemp(prefix="dacatowner-said-")
        said = io.StringIO()
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OWNER_PUBLIC_SITE_URL", None)
                with contextlib.redirect_stdout(said):
                    build_owner.build(out, build_owner.DEFAULT_LIVE_APP)
        finally:
            shutil.rmtree(out, ignore_errors=True)
        self.assertIn("dacat.pages.dev", said.getvalue())
        self.assertIn("/api/console", said.getvalue())


class TheDashboardHostServesWhatTheDashboardCalls(unittest.TestCase):
    """The other host the same builder writes, held to the same law.

    `CONSOLE_PREFIXES` has had a guard since the day the operator's passkey
    dead-ended on another host. `DASHBOARD_PREFIXES` had none — it was written
    out by hand from the pages as they stood — and what that costs is
    `/api/server-settings`: the moderation log channel's form (`dashboard.html`,
    "Where the moderation record goes") posts to a path this host answers with a
    301 to the public site, a browser turns a 301 POST into a GET, and the route
    is POST-only — so the save answered 405 on another host while the page it
    left looked like a page that had saved. Measured on the deployed host: `POST
    /api/server-settings` → 301 there, 401 (proxied, i.e. served) on the public
    site's own address.

    So this reads the dashboard's own sources for the paths they name rather
    than listing the endpoints a second time, and the paths it does not serve
    have to be public *by the application's own classification* — the two lists
    `/health`'s page classification already uses — because a dashboard page
    linking a privacy policy is not a defect on this host.
    """

    #: The dashboard's own files: its page, the picker, the two doors and the
    #: agreement every session passes through. The *frame* is here too, because
    #: it renders on this host — and its nav names the public pages, which is
    #: what the other half of the rule below is for.
    SOURCES = (
        "dashboard.html",
        "servers.html",
        "login.html",
        "agree.html",
        "site_base.html",
        "_theme.html",
        "meoww/base.html",
        "meoww/_switch.html",
        "meoww/_spine.html",
    )

    #: The scripts those pages load, which is where a fetch or a form action can
    #: be written without any template naming it.
    SCRIPTS = ("meoww.js", "spine.js", "motion.js", "theme.js", "dashboard.js")

    #: A path a browser is actually sent to: a form's action, a link's href, or a
    #: url a script fetches. Deliberately not every string that starts with a
    #: slash, because these pages talk about Slack-shaped commands in backticks —
    #: `/help`, `/modlog`, `/levelsettings` — and one Jinja comment mentions
    #: `/api/*`. A law that read every literal would ask this host to serve a
    #: slash command. A templated value (`href="/dashboard{{ guild_query }}"`) is
    #: read up to the expression, which is the part this host can be held to.
    USED = re.compile(
        r"""(?:action|href|src|data-[a-z-]*url)\s*=\s*["']([^"'{}]+)["']"""
        r"""|(?:fetch|open)\s*\(\s*["'`]([^"'`{}]+)["'`]"""
    )

    #: Paths these pages name that this host must **not** serve, each for a
    #: reason that is the point of the exception. `/owner` is the console's, on
    #: the console's own host, and a dashboard page is allowed to name it — what
    #: is not allowed is a redirect sending somebody there, which is
    #: `test_the_console_is_not_a_page_this_host_serves` and the door's own laws.
    HANDED_OFF = {"/owner"}

    def worker_prefixes(self) -> list[str]:
        """The list as the *built* worker declares it, not as the table does."""
        out = tempfile.mkdtemp(prefix="dacatdashboard-routes-")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                build_owner.build_proxy(build_owner.PROXY_HOSTS["dashboard"], out)
            worker = (Path(out) / "_worker.js").read_text(encoding="utf-8")
        finally:
            shutil.rmtree(out, ignore_errors=True)
        block = re.search(r"const CONSOLE_PREFIXES = \[(.*?)\];", worker, flags=re.DOTALL)
        self.assertIsNotNone(block, "the worker no longer declares what it serves")
        return re.findall(r'"([^"]+)"', block.group(1))

    def public_by_the_apps_own_rule(self, path: str) -> bool:
        """Whether the application already classifies this path as public.

        Read from `keep_alive` rather than restated, because a second copy of the
        marketing site's page list is a second thing to keep in step — and the
        question the law asks is not "is this page on the public site" but "did
        this host promise to answer it".
        """
        if path in keep_alive.PUBLIC_EXACT:
            return True
        return any(path == prefix or path.startswith(prefix) for prefix in keep_alive.PUBLIC_PREFIXES)

    def paths_the_dashboard_calls(self) -> set[str]:
        """Every path these pages actually send a browser to.

        Read out of the attributes rather than out of prose, and reduced to the
        path so a link carrying `?guild_id=` is the path it lands on.
        """
        found: set[str] = set()
        for path in self.sources():
            text = path.read_text(encoding="utf-8")
            for match in self.USED.finditer(text):
                target = (match.group(1) or match.group(2) or "").strip()
                if not target.startswith("/") or target.startswith("//"):
                    continue  # a scheme, a host, or a protocol-relative url: not this host's
                found.add(urllib.parse.urlsplit(target).path)
        return found

    def sources(self) -> list[Path]:
        """The dashboard's own files: a missing one is a failure, not a skip."""
        pages = [TEMPLATES / name for name in self.SOURCES]
        for page in pages:
            self.assertTrue(page.is_file(), f"the dashboard's {page.name} is gone")
        return pages + [ROOT / "static" / name for name in self.SCRIPTS if (ROOT / "static" / name).is_file()]

    def test_the_builder_and_the_worker_agree_about_what_is_served(self):
        self.assertEqual(self.worker_prefixes(), list(build_owner.DASHBOARD_PREFIXES))

    def test_the_member_apps_header_offers_only_what_this_host_answers(self):
        """The header is the address's, not the brand's.

        `MEMBER_FRAME_NAV` is what the member host's header draws. Measured on
        the deployed host: the same templates this address serves were wearing
        the *public site's* four destinations, three of which this address
        answers with a 301 — `/systems`, `/start`, `/radio` (and `/pricing`) —
        so "Systems" in the header of the app sent an administrator to the
        marketing page about systems instead of the switches one rail-click
        away, and "Get started" offered onboarding to somebody already inside.
        Every item has to be a path this host's own worker serves: the same law
        the console's header is held to, against the same kind of list.
        """
        prefixes = self.worker_prefixes()
        self.assertTrue(keep_alive.MEMBER_FRAME_NAV, "the member header offers nothing")
        for _page, label, href in keep_alive.MEMBER_FRAME_NAV:
            with self.subTest(item=label):
                self.assertTrue(
                    any(href == prefix or href.startswith(prefix + "/") for prefix in prefixes),
                    f"the member host's header offers {label} at {href}, which that "
                    "host answers with a 301 to the public site",
                )

    def test_the_header_is_chosen_by_the_address_the_request_arrived_on(self):
        """One decision, asked of the address — not of the page or its template.

        The member app's four pages are the *same templates* the public address
        serves (its worker proxies `/dashboard`, `/servers`, `/login` and
        `/agree` back to the backend), so a list written into a template cannot
        be right on both: the app's two destinations are what the member address
        answers, the site's four are what the public address does. So the frames
        are handed a list by this reader — and it answers nothing at all when
        the request arrived on no address in particular, which leaves every
        offline build and every local run exactly as it was.
        """
        member_host = urllib.parse.urlsplit(keep_alive.hub.dashboard_url()).netloc
        self.assertTrue(member_host, "the member host has no address to compare with")
        # The address is read from a *signed* forwarded host: the backend is
        # reachable directly, so the header alone is a claim any caller can make
        # (see `_proxy_signed` and the dashboard's own test for the refusal).
        with mock.patch.object(keep_alive, "_trust_proxy_headers", True), \
             mock.patch.object(keep_alive, "OWNER_PROXY_SECRET", "shared-secret"):
            with keep_alive.app.test_request_context(
                headers={"X-Meow-Proxy": "shared-secret", "X-Forwarded-Host": member_host}
            ):
                self.assertEqual(
                    keep_alive._frame_nav(), {"nav_items": keep_alive.MEMBER_FRAME_NAV}
                )
            for other in ("dacat.pages.dev", "dacatowner.pages.dev"):
                with self.subTest(host=other):
                    with keep_alive.app.test_request_context(
                        headers={"X-Meow-Proxy": "shared-secret", "X-Forwarded-Host": other}
                    ):
                        self.assertEqual(keep_alive._frame_nav(), {})
        with keep_alive.app.test_request_context():
            self.assertEqual(
                keep_alive._frame_nav(),
                {},
                "a request on no particular address was dressed as one of them",
            )

    def test_the_member_apps_pages_ask_which_address_they_are_on(self):
        """And the frames are only as right as the render calls that feed them.

        A reader nothing merges is a header that stays the public site's, and an
        unset name in a template is empty rather than an error — so this reads
        the four render calls the way `tests/test_site.py` reads the shared
        frame's, rather than trusting them to keep asking.
        """
        source = (ROOT / "keep_alive.py").read_text(encoding="utf-8")
        for template in ("dashboard.html", "servers.html", "login.html", "agree.html"):
            with self.subTest(template=template):
                found = re.search(
                    rf'render_template\(\s*"{re.escape(template)}",(?P<body>.*?)\n    \)',
                    source,
                    flags=re.DOTALL,
                )
                self.assertIsNotNone(found, f"no render_template call for {template}")
                self.assertIn(
                    "_frame_nav()",
                    found.group("body"),
                    f"the {template} render never asks which address this is, so "
                    "that page wears the public site's header wherever it is served",
                )

    def test_every_path_the_dashboard_calls_is_one_this_host_answers(self):
        prefixes = self.worker_prefixes()
        named = self.paths_the_dashboard_calls()
        self.assertGreater(len(named), 10, "the dashboard's own calls were not found")
        uncovered = sorted(
            path
            for path in named
            if path not in self.HANDED_OFF
            and not any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)
            and not self.public_by_the_apps_own_rule(path)
        )
        self.assertEqual(
            uncovered,
            [],
            "the dashboard's own pages call paths this host answers with a 301 to "
            "the public site, so those actions leave the host that drew the form: "
            f"{uncovered}",
        )

    def test_the_console_is_not_a_page_this_host_serves(self):
        """The door this host must not open, and why `/owner` is not a door at all.

        `/owner` is the console's, on the console's own host: asking for it here
        is a 301 to the public site, which is another 301 to `dacatowner`, where
        the session cookie this host minted does not exist — a cookie is
        host-scoped. So a sign-in that lands there ends at a passkey form, signed
        out, and one did: the Discord callback defaulted a global session to
        `/owner` on a host that cannot answer it. Asserted on the *list* rather
        than on the redirect, because the list is what the worker consults.
        """
        prefixes = build_owner.DASHBOARD_PREFIXES
        self.assertFalse(
            any("/owner" == prefix or "/owner".startswith(prefix + "/") for prefix in prefixes),
            "the dashboard host is being asked to serve the console",
        )


class TheClickwrapWearsTheHeaderOfTheAddressItIsOn(unittest.TestCase):
    """The console's one page with no page of its own to override the nav.

    `agree.html` extends the shared frame and is served on all three addresses —
    which one it is arrived on is the only thing that can decide its header, and
    two of the three answers already existed: `_frame_nav` hands it the member
    app's list on the member host, and the public address falls through to the
    marketing list, which is right there. On the **console's** host it fell
    through to that same marketing list, so the operator's own clickwrap offered
    Home, Systems and Radio — three paths that host answers with a 301 — from a
    page drawn a hop away from the console it belongs to.

    Three things have to hold for it to be fixed and to stay fixed: the reader
    answers only on that one address, the render asks it, and the template draws
    the console's own macro rather than a second copy of it.
    """

    AGREEMENT = "agree.html"
    NAV = "_owner_nav.html"

    def test_only_the_consoles_own_address_is_answered(self):
        console_host = urllib.parse.urlsplit(keep_alive.hub.console_url()).netloc
        self.assertTrue(console_host, "the console host has no address to compare with")
        # The *machine's* address is one hop behind this one and is never what a
        # browser asked for, so a reader built on it would answer nobody.
        self.assertNotEqual(console_host, urllib.parse.urlsplit(keep_alive.hub.live_url()).netloc)
        member_host = urllib.parse.urlsplit(keep_alive.hub.dashboard_url()).netloc
        # The address arrives signed — the backend is reachable directly, so the
        # header alone is a claim any caller can make (see `_proxy_signed`).
        with mock.patch.object(keep_alive, "_trust_proxy_headers", True), \
             mock.patch.object(keep_alive, "OWNER_PROXY_SECRET", "shared-secret"):
            with keep_alive.app.test_request_context(
                headers={"X-Meow-Proxy": "shared-secret", "X-Forwarded-Host": console_host}
            ):
                self.assertEqual(
                    keep_alive._console_frame_nav(),
                    {
                        "console_frame": True,
                        "site_url": keep_alive.hub.marketing_url(),
                        "dashboard_url": keep_alive.hub.dashboard_url(),
                    },
                )
            for other in (member_host, "dacat.pages.dev"):
                with self.subTest(host=other):
                    with keep_alive.app.test_request_context(
                        headers={"X-Meow-Proxy": "shared-secret", "X-Forwarded-Host": other}
                    ):
                        self.assertEqual(
                            keep_alive._console_frame_nav(),
                            {},
                            "a host that is not the console was handed the console's "
                            "header, which would leave its own frame in place of nothing",
                        )
        with keep_alive.app.test_request_context():
            self.assertEqual(
                keep_alive._console_frame_nav(),
                {},
                "a request on no particular address was dressed as one of them",
            )

    def test_the_page_asks_which_address_it_is_on_and_draws_that_header(self):
        source = (ROOT / "keep_alive.py").read_text(encoding="utf-8")
        found = re.search(
            rf'render_template\(\s*"{self.AGREEMENT}",(?P<body>.*?)\n    \)',
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(found, "no render_template call for the clickwrap")
        self.assertIn(
            "_console_frame_nav()",
            found.group("body"),
            "the clickwrap render never asks whether it is on the console's host, so "
            "the operator's agreement wears the public site's header again",
        )
        body = (TEMPLATES / self.AGREEMENT).read_text(encoding="utf-8")
        # The console's nav is one macro, drawn by all three pages of that host
        # now — a second copy of five lines is how this repository's other
        # duplicated lists came apart (`scripts/build_owner.py`'s prefixes).
        self.assertIn(f'{{% from "{self.NAV}" import console_nav %}}', body)
        self.assertIn("console_nav(site_url, dashboard_url)", body)
        # ...and falling through to the frame it inherits everywhere else, which
        # is what keeps the member address's list and the public site's in place.
        self.assertIn("{{ super() }}", body)
        self.assertNotIn("'/systems'", body)


class TheWorkerNamesTheVisitorItForwarded(unittest.TestCase):
    """This host is a proxy of a proxy, so the chain ends at Cloudflare.

    Everything the backend can infer about where a request came from stops one
    hop short of the person: the rightmost forwarded address is an edge, not a
    visitor. That made every sign-in on this domain share one rate-limit bucket
    — five wrong guesses from anyone locked the door for everybody, and a
    stranger could do it by hand — and it is why the worker now names the
    visitor and signs the name.
    """

    def test_the_callers_own_hop_headers_are_dropped(self):
        """A client describing a chain it is not part of must not be believed."""
        worker = build_owner._worker_file("backend.test", "shhh")
        for hop in (
            "X-Forwarded-For",
            "X-Forwarded-Host",
            "X-Forwarded-Proto",
            "X-Real-IP",
            "Forwarded",
            "CF-Connecting-IP",
            "X-Meow-Client-IP",
            "X-Meow-Proxy",
        ):
            with self.subTest(header=hop):
                self.assertIn(f'"{hop}"', worker)
        self.assertIn("reqHeaders.delete(hop)", worker)

    def test_it_stamps_and_signs_the_visitor(self):
        worker = build_owner._worker_file("backend.test", "shhh")
        self.assertIn('request.headers.get("CF-Connecting-IP")', worker)
        self.assertIn('reqHeaders.set("X-Meow-Client-IP", visitorIp)', worker)
        self.assertIn('reqHeaders.set("X-Meow-Proxy", OWNER_PROXY_SECRET)', worker)
        self.assertIn('const OWNER_PROXY_SECRET = "shhh";', worker)

    def test_a_build_without_the_secret_sends_no_signature(self):
        worker = build_owner._worker_file("backend.test", "")
        self.assertIn('const OWNER_PROXY_SECRET = "";', worker)
        # The stamp is still set, and the guard is what keeps it un-signed, so
        # the backend's fallback is reached rather than a forged claim.
        self.assertIn("if (OWNER_PROXY_SECRET) {", worker)

    def test_a_quote_in_the_secret_cannot_end_the_literal(self):
        worker = build_owner._worker_file("backend.test", 'a"b\\c')
        self.assertIn('const OWNER_PROXY_SECRET = "a\\"b\\\\c";', worker)

    def _build_with(self, secret: str) -> tuple[str, str]:
        out = tempfile.mkdtemp(prefix="dacatowner-secret-")
        said = io.StringIO()
        try:
            with mock.patch.dict(os.environ, {"OWNER_PROXY_SECRET": secret}):
                with contextlib.redirect_stdout(said):
                    build_owner.build(out, build_owner.DEFAULT_LIVE_APP)
            worker = (Path(out) / "_worker.js").read_text(encoding="utf-8")
        finally:
            shutil.rmtree(out, ignore_errors=True)
        return worker, said.getvalue()

    def test_the_secret_comes_from_the_environment(self):
        worker, said = self._build_with("from-the-env")
        self.assertIn('const OWNER_PROXY_SECRET = "from-the-env";', worker)
        self.assertNotIn("OWNER_PROXY_SECRET is not set", said)

    def test_a_build_that_cannot_sign_its_stamp_says_so(self):
        """The symptom is silent: limits still work, counting everybody as one."""
        _worker, said = self._build_with("")
        self.assertIn("OWNER_PROXY_SECRET is not set", said)


if __name__ == "__main__":
    unittest.main()
