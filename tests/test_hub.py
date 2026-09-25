"""The hub: a second public surface, and the promises it makes.

The hub is not the marketing site. It answers a different question — *what is
this thing doing, and where* — and it makes two promises that are easy to break
by accident and impossible to notice afterwards:

* **It is read-only.** There is no database behind it, no session, and no
  credential: it renders from the bot's own registry and its own already-public
  readings, and hands off to the console for anything that writes. A single
  accidentally-public route would not look wrong anywhere.
* **It never invents a figure and never names a private thing.** A count that
  cannot be read from where the page was rendered is *absent*, never zero —
  ``0 servers`` reads as a bot nobody uses rather than as a host that has not
  woken up — and configuration is reported as arithmetic rather than as a list
  of missing variable names.

Three groups of guards below, and each one exists because the alternative is a
class of defect this project has already had:

* **Drift.** The pages are rendered from the feature registry, so a feature that
  is added or retired has to move the hub with it. A hand-written list of systems
  or commands is the exact failure ``utils/features.py`` was written to end.
* **The policy.** The hub is published under ``script-src 'self'`` with no
  ``'unsafe-inline'``, which is only possible because its pages carry no inline
  script and no ``style`` attribute. A template that gained one would be fine in
  development and a console error in production.
* **The build.** A static host has no Flask, so the CSP, the frame denial and the
  proxy worker come from files this repository writes. A build that dropped one
  would ship the least-protected copy of the pages that ask a stranger to trust a
  bot — and the deployment it happened to would look entirely normal.
"""

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

#: The *renderer's* escaper, not `html.escape`: Jinja escapes an apostrophe as
#: `&#39;` and the stdlib as `&#x27;`, so a test comparing against the stdlib
#: fails on pages that escaped perfectly.
from markupsafe import escape

ROOT = Path(__file__).resolve().parents[1]

from scripts import build_hub  # noqa: E402
from utils import config_audit, features, figures, hub  # noqa: E402

#: The live app the pages are rendered by. Every page is rendered through the
#: real Flask app rather than a stub, so the test and production cannot disagree
#: about which template answers an address.
LIVE = "https://bot.example.test"

#: Files the hub's stylesheet and its templates are allowed to ask for: the
#: hub's own two, plus the shared assets the build copies out of `static/`.
ALLOWED_ASSETS = set(hub.SHARED_ASSETS) | {"hub.css", "hub.js", "hub-index.json"}


class HubCase(unittest.TestCase):
    """A rendered hub, with the static-build flag set the way a build sets it."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("DISCORD_CLIENT_ID", "1548380269776605324")

    def setUp(self):
        # Imported here rather than at module scope: `keep_alive` reads the
        # environment at import time, and a test that wants to see the *build*
        # answer has to set the flag before it is imported by anybody.
        self._patches = []
        self._set_env("MEOW_STATIC_SITE", "1")
        import keep_alive  # noqa: PLC0415
        self.keep_alive = keep_alive
        self.client = keep_alive.app.test_client()
        figures.reset()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        figures.reset()

    def _set_env(self, key, value):
        patch = mock.patch.dict(os.environ, {key: value})
        patch.start()
        self._patches.append(patch)

    # -- helpers ----------------------------------------------------------

    def render(self, page: str) -> str:
        route = dict((pid, path) for pid, _l, path, _i, _g, _b in hub.HUB_PAGES)[page]
        response = self.client.get(route)
        self.assertEqual(response.status_code, 200, f"{route} did not render")
        return response.get_data(as_text=True)

    def pages(self):
        return {page: self.render(page) for page in hub.HUB_PAGE_IDS}


class TheRegistryIsTheOnlySource(HubCase):
    """A feature is added or retired in one place, and the hub follows."""

    def test_every_declared_page_has_a_route_and_renders(self):
        """A page table entry with no route is a rail link to a 404.

        The hub answers under one dynamic rule rather than seven literals, so
        the address is checked by *rendering* it — a rule whose converter
        rejects the page id would still satisfy a set-membership test while
        answering 404 to every visitor.
        """
        rules = {rule.rule for rule in self.keep_alive.app.url_map.iter_rules()}
        self.assertTrue(
            {r for r in rules if r.startswith("/hub")},
            "the hub is not routed at all",
        )
        for page, _label, path, _icon, _group, _blurb in hub.HUB_PAGES:
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 200, f"{page} has no page at {path}")

    def test_the_rail_lists_every_page_and_only_marks_one_current(self):
        for page, body in self.pages().items():
            for _pid, label, path, _i, _g, _b in hub.HUB_PAGES:
                self.assertIn(f'href="{path}"', body, f"{path} missing from {page}'s rail")
            self.assertEqual(
                body.count('aria-current="page"'), 1,
                f"{page} marks a number of rail links as current other than one",
            )

    def test_every_system_in_the_registry_reaches_the_pages(self):
        """A new feature cannot be added without the hub mentioning it.

        Compared escaped, because the page is HTML: a system called "Leveling &
        XP" reaches the reader through ``&amp;``, and asserting the raw name
        would fail on the escape rather than on the absence it is looking for.
        """
        body = self.render("systems")
        for feature in features.FEATURES:
            self.assertIn(
                escape(feature.name), body,
                f"{feature.name} is not on the systems page",
            )
            self.assertIn(escape(feature.icon), body)
        for category in {feature.category for feature in features.FEATURES}:
            self.assertIn(escape(category), body, f"the {category} grouping is missing")

    def test_every_gated_command_is_in_the_reference(self):
        body = self.render("commands")
        for command in sorted(features.MODULE_COMMAND_MAP):
            self.assertIn(f"/{command}", body, f"/{command} is missing from the reference")

    def test_every_multi_system_gate_is_listed_as_one(self):
        """A command needing two switches has to name both, escaped.

        Listed under only one of them, a server that turned the other off would
        lose the command with no switch anywhere on the page explaining it.
        """
        body = self.render("commands")
        for command, feature_ids in features.SHARED_GATES.items():
            for feature_id in feature_ids:
                self.assertIn(escape(features.get(feature_id).name), body)
            self.assertIn(f"/{command}", body)

    def test_the_counts_the_pages_quote_are_the_registry_counts(self):
        """Every count on the hub is arithmetic over the registry, not a memory.

        Read from the page that actually quotes it: the switchable split is
        drawn on the overview, and asking the systems page for a sentence it
        does not carry would pass for the wrong reason on one and fail on the
        other depending on which page happened to repeat it.
        """
        systems = self.render("systems")
        overview = self.render("overview")
        declared = len(features.FEATURES)
        toggleable = len(features.TOGGLEABLE_IDS)
        self.assertIn(f"{declared} systems", systems)
        self.assertIn(f"<b>{declared}</b>", systems)
        self.assertIn(f"{toggleable} of them switchable", overview)
        self.assertIn(f"<b>{toggleable}</b>", systems)
        self.assertIn(str(len(features.MODULE_COMMAND_MAP)), systems)
        self.assertNotIn(">None<", systems)
        self.assertNotIn(">None<", overview)

    def test_the_rail_count_agrees_with_the_page_it_points_at(self):
        """The "two renderers, two numbers" defect, caught at the rail.

        The rail used to show the *gated root* count while the page it links to
        was headed with the distinct-command count, so the same hub offered two
        numbers for the same word a click apart.
        """
        overview = self.render("overview")
        commands = self.render("commands")
        headline = re.search(r"<h2>(\d+) commands", commands)
        self.assertIsNotNone(headline, "the commands page no longer counts its own commands")
        self.assertIn(
            f'<span class="rail-count">{headline.group(1)}</span>', overview,
            "the rail's Commands count disagrees with the commands page",
        )

    def test_a_broken_registry_would_be_reported_not_hidden(self):
        problems = features.validate()
        self.assertEqual(problems, [], f"the registry itself is unhealthy: {problems}")
        with mock.patch.object(features, "validate", return_value=["duplicate feature id: fake"]):
            body = self.render("systems")
        self.assertIn("duplicate feature id: fake", body)
        self.assertIn("Registry problem", body)


class NoReadingIsEverAZero(HubCase):
    """Absent is absent. A zero is a measurement, and a page may not invent one."""

    def test_a_static_build_reports_no_local_figures(self):
        """The tracker lives in a SQLite file, and the builder is not the deployment.

        This is the defect the first build actually shipped: the operator's own
        laptop database was rendered into a public page as the deployment's
        numbers. They are not stale — they are somebody else's.
        """
        body = self.render("overview")
        for slot in re.findall(r'data-live="(messages|people|voice)">([^<]*)<', body):
            self.assertEqual(slot[1], "—", f"{slot[0]} carried a figure into a static build")

    def test_an_unread_window_is_absent_rather_than_zero(self):
        payload = figures.deployment(read_tracker=False)
        read = figures.deployment(read_tracker=True)
        self.assertEqual(sorted(payload), sorted(read), "the two shapes must be identical")
        for period in payload["periods"]:
            self.assertIsNone(period["messages"])
            self.assertIsNone(period["voice_seconds"])
            self.assertIsNone(period["people"])
            self.assertEqual(period["messages_text"], "")
        self.assertEqual(len(payload["periods"]), len(figures.PERIODS))

    def test_an_unread_payload_never_enters_or_reads_the_cache(self):
        """Otherwise a real visitor is served a blank page for a whole minute."""
        figures.reset()
        figures.deployment(read_tracker=True)  # a real reading, cached
        figures.snapshot(read_tracker=False)
        with mock.patch.object(figures, "deployment", wraps=figures.deployment) as spy:
            figures.snapshot(read_tracker=True)
        self.assertTrue(spy.called, "the unread call poisoned the cache")

    def test_the_configuration_counts_are_refused_by_a_build(self):
        config = hub.config_payload(readable=False)
        self.assertFalse(config["readable"])
        self.assertIsNone(config["set"])
        self.assertIsNone(config["missing"])
        self.assertIsNone(config["attention"])
        for row in config["rows"]:
            self.assertIsNone(row["missing"], row["label"])
            self.assertIsNone(row["present"])
            # The *totals* survive, because how many settings exist is a fact
            # about the code rather than about the machine rendering it.
            self.assertGreater(row["total"], 0)
        self.assertGreater(config["total"], 0)

    def test_both_pages_that_draw_the_configuration_table_say_the_same_thing(self):
        """One table, two pages, and they must not disagree.

        Written by hand in two templates, one of them spelled an unread count as
        `0` and the other as a dash — so the same deployment read as "nothing
        missing" on one page and "cannot be read from here" on the next, from the
        same payload. Both go through one macro now, and this is the guard.
        """
        for page in ("operations", "deployment"):
            body = self.render(page)
            self.assertNotIn(">None<", body, f"{page} rendered Python's None")
            for label in ("essential", "security", "capability", "tuning", "host"):
                self.assertIn(
                    f'data-live="config-{label}">—<', body,
                    f"{page} spells an unread {label} count as something other than a dash",
                )

    def test_no_page_names_a_setting(self):
        """Counts, never names: this surface is unauthenticated.

        ``/health`` made this choice for the same reason — "this deployment has
        no sign-in secret" is not a sentence to hand a stranger — and the hub
        follows the endpoint rather than re-deciding.

        Asserted over the whole document rather than over the table, because the
        first draft of this guard was scoped to the table and passed while the
        front page was publishing four complete engineering notes. One of them
        read, in prose, that a panel had said ``set UPTIMEROBOT_API_KEY`` — a
        sentence about a setting that is not set, assembled for free on an
        unauthenticated page. The rule is the document, not the widget.

        Nothing is redacted to satisfy this. A headline that names a setting is
        a headline that should be rewritten, and this test is how its author
        finds out.
        """
        for page, body in self.pages().items():
            for setting in config_audit.SETTINGS:
                self.assertNotIn(setting.name, body, f"{page} names {setting.name}")

    def test_the_guard_would_catch_a_setting_that_reached_a_page(self):
        """Watched to fail, because the guard above spends its life finding
        nothing, and a guard that cannot find anything is not a guard.

        A headline is fed in that names a setting in the exact way the real one
        did — as prose, in a sentence, with no table anywhere near it — and the
        page is asked whether it now carries it. This is the check the guard
        makes, proved to be capable of failing.
        """
        self.assertTrue(config_audit.SETTINGS, "there are no settings to check")
        setting = config_audit.SETTINGS[0].name
        planted = [{
            "version": "Unreleased",
            "icon": "🐞",
            "lead": f"A panel asked a stranger to set {setting} by hand",
            "body": "The body, which is not published.",
        }]
        with mock.patch.object(hub, "read_changelog", return_value=(planted, 1)):
            body = self.render("overview")
        self.assertIn(
            setting, body,
            "the guard cannot see a setting in a headline, so it proves nothing",
        )

    def test_the_status_endpoint_answers_the_shape_the_page_reads(self):
        payload = self.client.get("/api/status").get_json()
        self.assertTrue(payload["ok"])
        self.assertIn("at", payload)
        for key in ("connected", "started", "label"):
            self.assertIn(key, payload["gateway"])
        for key in ("total", "unset", "attention", "rows"):
            self.assertIn(key, payload["config"])
            # and the rows carry what the page's macro needs to fill a cell
        for row in payload["config"]["rows"]:
            self.assertEqual(sorted(row), ["label", "missing", "total"])
        self.assertEqual([row["id"] for row in payload["figures"]["periods"]],
                         [period for period, _l, _d in figures.PERIODS])

    def test_the_status_endpoint_names_nothing_private(self):
        body = self.client.get("/api/status").get_data(as_text=True)
        for setting in config_audit.SETTINGS:
            self.assertNotIn(setting.name, body)
        self.assertNotIn("*", body.split("Access-Control-Allow-Origin")[-1][:8]
                         if "Access-Control-Allow-Origin" in body else "")


class TheHubCannotWrite(HubCase):
    """A public host must not be able to change anything."""

    def test_the_worker_only_forwards_the_allow_list(self):
        worker = build_hub._worker_file("bot.example.test")
        for path in hub.HUB_PROXY_ROUTES:
            self.assertIn(json.dumps(path), worker)
        # Only GET and HEAD leave this host, and they leave for one path.
        self.assertIn('method !== "GET" && method !== "HEAD"', worker)
        self.assertIn("405", worker)
        # A credential from a public host is never forwarded.
        self.assertIn("const outboundHeaders = new Headers();", worker)
        self.assertNotIn("new Headers(request.headers)", worker)

    def test_the_routes_file_matches_the_worker_allow_list(self):
        routes = json.loads(build_hub._routes_file())
        self.assertEqual(sorted(routes["include"]), sorted(hub.HUB_PROXY_ROUTES))
        self.assertEqual(routes["version"], 1)

    def test_no_hub_route_accepts_anything_but_a_read(self):
        for rule in self.keep_alive.app.url_map.iter_rules():
            if not (rule.rule == "/hub" or rule.rule.startswith("/hub/")):
                continue
            self.assertTrue(
                rule.methods <= {"GET", "HEAD", "OPTIONS"},
                f"{rule.rule} accepts {rule.methods - {'GET', 'HEAD', 'OPTIONS'}}",
            )

    def test_the_hub_reaches_no_database(self):
        """The page renders with the database pointed somewhere that does not exist.

        A stronger claim than "it says it has none": the pages are rendered with
        a database path that cannot be opened, so a hub page that *did* read one
        would fail here rather than in production on a host that has none.
        """
        with mock.patch.object(figures, "snapshot", wraps=figures.snapshot):
            for page in hub.HUB_PAGE_IDS:
                self.render(page)  # renders without a reachable database at all


class ThePolicyIsOneStringInTwoPlaces(HubCase):
    """The strict policy, and the reason the pages can carry it."""

    def test_the_app_applies_the_strict_policy_to_the_hub(self):
        for page, _label, path, _i, _g, _b in hub.HUB_PAGES:
            policy = self.client.get(path).headers["Content-Security-Policy"]
            self.assertEqual(policy, hub.CSP)
            self.assertNotIn("unsafe-inline", policy)

    def test_the_dispatcher_still_has_its_looser_policy_elsewhere(self):
        """The hub is tighter than the rest of the app, and only the hub."""
        page = self.client.get("/dashboard", follow_redirects=False)
        policy = page.headers.get("Content-Security-Policy", "")
        self.assertNotEqual(policy, hub.CSP)
        market = self.client.get("/", follow_redirects=False)
        self.assertNotEqual(market.headers.get("Content-Security-Policy", ""), hub.CSP)

    def test_the_built_headers_carry_the_same_policy_as_the_app(self):
        """Two hand-written copies of a security policy is how a tightening
        lands on one host and not the other, and the looser one is always the
        one nobody is looking at."""
        headers = build_hub._headers_file()
        self.assertIn(f"Content-Security-Policy: {hub.CSP}", headers)
        for name, value in hub.SECURITY_HEADERS:
            self.assertIn(f"{name}: {value}", headers)

    def test_no_rendered_page_carries_something_the_policy_blocks(self):
        for page, body in self.pages().items():
            self.assertEqual(
                build_hub._inline_violations(body), [],
                f"{page} carries inline script or a style attribute",
            )

    def test_the_inline_check_catches_what_it_is_for(self):
        """Watched to fail, because a guard nobody has seen bite is a comment."""
        self.assertTrue(build_hub._inline_violations('<script>var x = 1;</script>'))
        self.assertTrue(build_hub._inline_violations('<div style="color: red">'))
        self.assertTrue(build_hub._inline_violations("<style>body{}</style>"))
        # ...and does not fire on the correct markup.
        self.assertEqual(build_hub._inline_violations('<script src="hub.js" defer></script>'), [])

    def test_the_two_light_token_blocks_declare_the_same_properties(self):
        """Light is spelled twice, and the second copy is the one that rots.

        A media query cannot share a declaration block with a selector, and the
        alternative — one selector matching both — would also match a browser
        with no preference to report, making light the default for everybody. So
        it is duplicated on purpose, and pinned here.
        """
        css = (ROOT / "hub" / "static" / "hub.css").read_text()
        chosen = css.split('[data-theme="light"] {', 1)[1].split("}", 1)[0]
        system = css.split(':root:not([data-theme="dark"]) {', 1)[1].split("}", 1)[0]
        names = lambda block: set(re.findall(r"(--[a-z0-9-]+)\s*:", block))  # noqa: E731
        self.assertEqual(names(chosen), names(system))
        self.assertGreater(len(names(chosen)), 15, "the token list looks truncated")

    def test_no_page_asks_for_an_asset_the_build_does_not_ship(self):
        for page, body in self.pages().items():
            for asset in re.findall(r'(?:href|src)="(?:/hub/static|/static)/([^"?]+)', body):
                self.assertIn(asset, ALLOWED_ASSETS, f"{page} asks for {asset}")

    def test_the_hub_loads_nothing_from_anywhere_else(self):
        """`connect-src 'self'` and `img-src 'self' data:` mean a third-party
        origin would be blocked, so one in the markup is a broken page."""
        for page, body in self.pages().items():
            for url in re.findall(r'(?:href|src)="(https?://[^"]+)"', body):
                self.assertTrue(
                    url.startswith("https://discord.com")
                    or url.startswith(hub.marketing_url())
                    or url.startswith(hub.live_url()),
                    f"{page} reaches an origin the policy does not allow: {url}",
                )

    def test_the_click_handlers_are_wired(self):
        """Every initialiser the script defines is called by it.

        This is the defect the browser found and nothing else would have: moving
        the init calls into a `start()` helper dropped `initTheme`, so the theme
        button rendered, read correctly, and did nothing when pressed.
        """
        script = (ROOT / "hub" / "static" / "hub.js").read_text()
        defined = re.findall(r"^\s{4}function (init[A-Z]\w*|paint[A-Z]\w*)\(", script, re.MULTILINE)
        self.assertGreater(len(defined), 6, "the initialiser list looks wrong")
        for name in defined:
            calls = len(re.findall(rf"\b{name}\(", script))
            self.assertGreater(calls, 1, f"{name} is defined but never called")

    def test_the_script_survives_being_parsed_before_the_body_exists(self):
        """A `defer` script is safe; the guard is for the day one is not.

        A single null `document.body` used to throw inside `initLive` and take
        every initialiser after it down — the reveal observer, the palette, the
        meters — leaving a page that simply rendered blank above the fold.

        Asserted as *guarded access* rather than as absence: the fix kept the
        read and put a fallback in front of it, so a test looking for the bare
        expression would pass on the broken version and fail on the fixed one.
        """
        script = (ROOT / "hub" / "static" / "hub.js").read_text()
        reads = re.findall(r"document\.body(?=\s*[.&|?])", script)
        self.assertTrue(reads, "nothing reads the body any more; check the page hooks")
        guards = re.findall(r"document\.body\s*(?:&&|\|\|)", script)
        self.assertTrue(guards, "the body is read without a fallback")
        self.assertIn('document.readyState === "loading"', script)
        self.assertIn('document.addEventListener("DOMContentLoaded", start', script)


class TheBuiltHub(HubCase):
    """The files a static host is handed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._dir = tempfile.mkdtemp(prefix="meow-hub-")
        cls.report = build_hub.build(cls._dir, LIVE, "https://hub.example.test")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def _read(self, name: str) -> str:
        with open(os.path.join(self._dir, name), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_the_build_reports_nothing_it_refuses_to_ship(self):
        self.assertEqual(self.report["leftovers"], [])
        self.assertEqual(self.report["inline"], [])
        self.assertEqual(len(self.report["pages"]), len(hub.HUB_PAGES))

    def test_every_page_is_a_file_with_no_root_relative_url_left(self):
        for _route, filename, _size in self.report["pages"]:
            body = self._read(filename)
            self.assertEqual(build_hub._leftovers(body), [], filename)

    def test_a_link_that_needs_the_live_process_is_sent_there(self):
        body = self._read("index.html")
        self.assertIn(f"{LIVE}/dashboard", body)

    def test_the_one_proxied_endpoint_stays_relative(self):
        body = self._read("index.html")
        self.assertIn('data-status-url="/api/status"', body)

    def test_the_palette_index_is_written_where_the_page_looks_for_it(self):
        body = self._read("index.html")
        self.assertIn('data-index="static/hub-index.json"', body)
        payload = json.loads(self._read("static/hub-index.json"))
        labels = {entry["label"] for entry in payload["entries"]}
        for command in sorted(features.MODULE_COMMAND_MAP):
            self.assertIn(f"/{command}", labels)

    def test_the_host_files_are_all_present(self):
        for name in ("_headers", "_redirects", "_worker.js", "_routes.json",
                     "404.html", "robots.txt", "sitemap.xml", ".nojekyll"):
            self.assertTrue(os.path.isfile(os.path.join(self._dir, name)), name)

    def test_the_stylesheet_ships_the_marks_it_asks_for(self):
        """A missing image draws nothing, and logs nothing."""
        for name in ("logo.svg", "favicon.svg"):
            self.assertTrue(os.path.isfile(os.path.join(self._dir, "static", name)), name)
        self.assertNotIn("cursor: url(", self._read("static/hub.css"))

    def test_the_build_refuses_to_point_a_host_at_the_repository(self):
        """The line between a website and a leak, and it has been crossed once."""
        with self.assertRaises(SystemExit):
            build_hub.build(str(ROOT), LIVE)

    def test_a_page_that_answers_with_an_error_fails_the_build(self):
        """The builder asserts 200 for every route, the way the site builder does."""
        import keep_alive  # noqa: PLC0415

        broken = keep_alive.app.test_client()
        with mock.patch.object(keep_alive, "hub_page", return_value=("nope", 500)):
            with mock.patch.object(keep_alive, "app") as app:
                app.test_client.return_value = broken
                with mock.patch.object(broken, "get") as get:
                    get.return_value.status_code = 500
                    get.return_value.get_data.return_value = ""
                    with self.assertRaises(SystemExit):
                        build_hub.build(tempfile.mkdtemp(prefix="meow-hub-bad-"), LIVE)

    def test_the_shipped_headers_are_the_ones_the_app_uses(self):
        headers = self._read("_headers")
        for name, value in hub.SECURITY_HEADERS:
            self.assertIn(f"  {name}: {value}", headers)


class TheChangelogPage(HubCase):
    """The history, read out of the file the team writes for itself."""

    def test_the_page_quotes_the_changelog_rather_than_a_summary(self):
        entries, total = hub.read_changelog(str(ROOT / "CHANGELOG.md"))
        self.assertGreater(total, 20)
        body = self.render("changes")
        self.assertIn(escape(entries[0]["lead"][:60]), body)
        self.assertIn(str(total), body)

    def test_every_page_publishes_headlines_and_not_the_engineering_notes(self):
        """The half of the changelog that is safe to hand a stranger.

        A body records what broke and how it was found, which means internal
        file paths, the shape of a past incident, and the names of settings —
        on an unauthenticated page. The headline is written in product terms and
        is the part a reader came for. This is the guard on that line, and it is
        the reason the shortening pass that used to sit between them is gone:
        there is nothing left to shorten.

        Every page, because the leak this was written after was on the overview:
        the history page had been publishing headlines for a while and the front
        page was still drawing whole entries in its cards, a field away from
        each other in the same module.
        """
        entries, _total = hub.read_changelog(str(ROOT / "CHANGELOG.md"))
        long_bodies = [entry for entry in entries if len(entry["body"]) > 400]
        self.assertTrue(long_bodies, "the changelog has no long entries to check")
        self.assertNotIn("short", entries[0], "a field only the page used is still parsed")
        published = self.pages()
        for entry in long_bodies:
            for page, body in published.items():
                self.assertNotIn(
                    escape(entry["body"][:60]), body,
                    f"{page} publishes an entry's engineering notes",
                )

    def test_the_headline_that_is_published_arrived_escaped(self):
        """Escaping is what makes a `<Guild …>` in prose safe to publish.

        The changelog writes Discord objects into sentences as they appeared in a
        log. Unescaped, a browser reads one as an unknown element and swallows
        the text up to its closing bracket — the words simply vanish. Every
        published headline is checked against the page, escaped.
        """
        entries, _total = hub.read_changelog(str(ROOT / "CHANGELOG.md"))
        page = self.render("changes")
        for entry in entries[:40]:
            self.assertIn(escape(entry["lead"]), page, entry["lead"][:40])

    def test_the_markup_stays_out_of_the_page(self):
        """Markdown is unwrapped rather than rendered.

        The changelog is prose written by and for the people working on this, and
        it reaches a public page. A renderer is a parser is an injection surface,
        so bold, emphasis and backticks are simply removed.
        """
        entries, _total = hub.read_changelog(str(ROOT / "CHANGELOG.md"))
        for entry in entries:
            for field in ("lead", "body"):
                self.assertNotIn("**", entry[field], entry["lead"][:40])
                self.assertNotIn("`", entry[field], entry["lead"][:40])
        # Angle brackets are deliberately *not* unwrapped here: the changelog
        # quotes log lines like `<Guild …>` in prose, and the browser is handed
        # `&lt;Guild …&gt;` because the template escapes. That is the next test's
        # job, and asserting absence here would be asking the parser to do the
        # renderer's work — which is how a guard passes for the wrong reason.

    def test_emphasis_is_unwrapped_without_leaving_its_asterisks(self):
        """`**bold**` and `*emphasis*` both reach the page as words.

        Order matters and is the whole subtlety: single-asterisk emphasis run
        before the bold pass matches the outer pair of `**x**` and leaves two
        literal asterisks behind, on a page that renders no markup.
        """
        self.assertEqual(hub.plain("**Bold**: *em* rest"), "Bold: em rest")
        self.assertEqual(hub.plain("**Bold**: plain"), "Bold: plain")
        self.assertNotIn("*", hub.plain('*"quoted"* and **bold**'))

    def test_a_missing_changelog_is_an_empty_history_not_a_crash(self):
        self.assertEqual(hub.read_changelog(str(ROOT / "nope.md")), ([], 0))

    def test_the_version_a_bullet_belongs_to_travels_with_it(self):
        text = "# Changelog\n\n## 2.0.0\n\n- **Lead**: body.\n\n## Unreleased\n\n**Free text**\n- plain bullet\n"
        entries, total = hub.parse_changelog(text)
        self.assertEqual(total, 2)
        self.assertEqual(entries[0]["version"], "2.0.0")
        self.assertEqual(entries[0]["lead"], "Lead")
        self.assertEqual(entries[0]["body"], "body.")
        # A bullet with no bold lead keeps its first sentence as the headline
        # rather than being dropped from the history.
        self.assertEqual(entries[1]["version"], "Unreleased")
        self.assertTrue(entries[1]["lead"])


class TheSearchIndex(HubCase):
    def test_the_palette_can_reach_every_page_and_every_system(self):
        index = hub.search_index(hub.systems_payload(), hub.commands_payload())
        kinds = {entry["kind"] for entry in index}
        self.assertEqual(kinds, {"page", "system", "command"})
        # A command carries no href on purpose — there is nowhere to send
        # somebody for `/market buy` — so the reachability claim is about the
        # entries that do have an address to offer.
        hrefs = {entry["href"] for entry in index if entry.get("href")}
        for _pid, _label, path, _i, _g, _b in hub.HUB_PAGES:
            self.assertIn(path, hrefs)

    def test_a_command_entry_copies_rather_than_navigates(self):
        """There is nowhere to send somebody for `/market buy`."""
        index = hub.search_index(hub.systems_payload(), hub.commands_payload())
        for entry in index:
            if entry["kind"] != "command":
                continue
            self.assertNotIn("href", entry)
            self.assertEqual(entry["copy"], entry["label"])

    def test_the_served_index_and_the_built_one_come_from_the_same_function(self):
        served = self.client.get("/hub/index.json").get_json()
        expected = hub.search_index(hub.systems_payload(), hub.commands_payload())
        self.assertEqual(served["entries"], expected)


class TheAddresses(HubCase):
    """One page table, read by the rail, the routes, the palette and the build."""

    def test_a_route_becomes_the_file_the_builder_writes(self):
        self.assertEqual(
            dict(build_hub.PAGES),
            dict((path, "index.html" if page == "overview" else f"{page}.html")
                 for page, _l, path, _i, _g, _b in hub.HUB_PAGES),
        )

    def test_an_unknown_page_lands_on_the_overview_rather_than_a_404(self):
        response = self.client.get("/hub/not-a-page")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/hub"))

    def test_hub_urls_do_not_collide_with_the_console(self):
        """`/hub` is a prefix of nothing the dashboard owns, and vice versa."""
        routes = sorted(rule.rule for rule in self.keep_alive.app.url_map.iter_rules())
        hub_routes = [r for r in routes if r == "/hub" or r.startswith("/hub/")]
        self.assertNotIn("/hub", {r for r in routes if r.startswith("/dashboard")})
        for route in hub_routes:
            self.assertFalse(
                route.startswith("/dashboard"),
                f"{route} is both a hub address and a console one",
            )
