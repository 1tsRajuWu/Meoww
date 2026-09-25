"""The owner site's build: a proxy that has to carry the pages' own assets.

``meow-owner.pages.dev`` is not a second site with its own pages — it is a
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
            "copied into it, so they 404 on meow-owner.pages.dev and only there: "
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
        cls.dir = tempfile.mkdtemp(prefix="meow-owner-")
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

    #: Paths the console's own pages name that this host deliberately does not
    #: serve, each for a reason that is the whole point of the exception.
    #: ``/login`` is the per-server dashboard's door and it belongs on the public
    #: host: this host answers it with a 301 there, which is where a server's own
    #: administrator should be signing in — the console is the operator's, and a
    #: second door to it on this domain is the defect its own docstring records.
    HANDED_OFF = {"/login"}

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

    def test_the_builder_and_the_worker_agree_about_what_is_served(self):
        self.assertEqual(self.worker_prefixes(), list(build_owner.CONSOLE_PREFIXES))

    def test_every_path_the_console_calls_is_one_this_host_serves(self):
        prefixes = self.worker_prefixes()
        called = self.paths_the_console_calls()
        self.assertGreater(len(called), 3, "the console's own calls were not found")
        uncovered = sorted(
            path
            for path in called
            if path not in self.HANDED_OFF
            and not any(path == p or path.startswith(p + "/") for p in prefixes)
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

    def test_the_member_facing_sign_in_page_is_not_served_here(self):
        """`/login` is the second door. It is the one path that must not be here.

        A `/login/discord` prefix covers the console's own Discord button, and
        deliberately does not cover `/login` itself: proxying that page is how
        this domain came to offer a passkey form whose limits and guards nobody
        was looking at, while the console behind it was being reviewed.
        """
        prefixes = self.worker_prefixes()
        self.assertIn("/login/discord", prefixes)
        self.assertNotIn("/login", prefixes)
        # And a bare `/login` is not covered by any prefix that is there.
        self.assertFalse(
            any("/login" == p or "/login".startswith(p + "/") for p in prefixes)
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

        out = Path(tempfile.mkdtemp(prefix="meow-owner-routes-"))
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
        out = tempfile.mkdtemp(prefix="meow-owner-public-")
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
        out = tempfile.mkdtemp(prefix="meow-owner-said-")
        said = io.StringIO()
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OWNER_PUBLIC_SITE_URL", None)
                with contextlib.redirect_stdout(said):
                    build_owner.build(out, build_owner.DEFAULT_LIVE_APP)
        finally:
            shutil.rmtree(out, ignore_errors=True)
        self.assertIn("meow-bot.pages.dev", said.getvalue())
        self.assertIn("/api/console", said.getvalue())


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
        out = tempfile.mkdtemp(prefix="meow-owner-secret-")
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
