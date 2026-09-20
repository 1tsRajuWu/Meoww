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
from pathlib import Path
from unittest import mock

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
        somebody actually hit.
        """
        dashboard = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
        for name in ("dashboard.css", "motion.js", "gateway.js"):
            with self.subTest(asset=name):
                self.assertIn(f"/static/{name}", dashboard)
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

    def test_the_console_bundle_travels_with_it(self):
        bundle = self.root / "static" / "console"
        self.assertTrue((bundle / "app.js").is_file())
        self.assertTrue((bundle / "app.css").is_file())
        self.assertTrue((bundle / "build.json").is_file())

    def test_it_refuses_to_write_into_the_repository_root(self):
        with self.assertRaises(ValueError):
            build_owner.build(str(ROOT), build_owner.DEFAULT_LIVE_APP)


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
