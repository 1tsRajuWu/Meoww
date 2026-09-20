"""The operator console: who may open it, and what it is allowed to say.

``/owner`` used to be four server-rendered pages, and the suite that guarded it
asserted on markup — section ids, row classes, the inline script's element
lookups. It is now one HTML shell plus a bundled app, so those assertions had
nothing left to hold: the fleet is not in the HTML at all, it arrives from
``/api/console/fleet``. What replaces them is the guarantee that actually
mattered, applied to where the data now comes from.

Three things are checked here and they are deliberately different kinds of claim:

* **Access.** Both the shell and every API route refuse a session that is not the
  operator's, and the refusal is asserted on the *body*, not only the status — a
  403 that still handed back server names would pass a status-code test. This is
  the claim ``/owner`` has always been about, because it is the only surface in
  this project whose scope is the whole fleet.
* **The bundle matches its sources.** ``static/console/`` is committed (the host
  has no guaranteed Node), which this repository otherwise refuses to do with
  generated output. The stamp is what keeps that honest: it is a hash of the
  sources, recomputed here, so an edited-but-unrebuilt console fails the suite
  rather than shipping a copy that is quietly one commit behind.
* **The numbers are the registry's.** Counts are cross-checked against
  ``features`` and ``_fleet_rows()``, never against a literal — the lesson the
  old dashboard tests learned when a hard-coded "2 / 3" outlived its registry.
"""

import hashlib
import json
import re
import unittest
from pathlib import Path

import keep_alive
from tests.test_composer import GUILD_ID
from tests.test_dashboard import _DashboardFixture

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "console"
BUNDLE = ROOT / "static" / "console"

CONSOLE_ROUTES = (
    "/api/console/overview",
    "/api/console/fleet",
    "/api/console/systems",
    "/api/console/ops",
    "/api/console/changes",
)


def _source_hash() -> tuple[str, int]:
    """The console's sources, hashed exactly as ``console/scripts/stamp.mjs`` does.

    Sorted ``relpath + NUL + bytes`` pairs. Written out on both sides rather than
    exported from one, because the two languages cannot share a function and the
    format is four lines: if this drifts from the JavaScript, the stamp test fails
    — loudly, and for the right reason.
    """
    sources = ["src", "package.json", "tsconfig.json", "vite.config.ts", "index.html"]
    files: list[Path] = []
    for entry in sources:
        path = CONSOLE / entry
        files.extend(sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path])
    pairs = sorted((p.relative_to(CONSOLE).as_posix(), p) for p in files)

    digest = hashlib.sha256()
    for rel, path in pairs:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
    return digest.hexdigest(), len(pairs)


class TheBundleMatchesItsSources(unittest.TestCase):
    """The one thing committing built output requires, checked rather than hoped."""

    def test_the_stamp_exists_and_parses(self):
        stamp = BUNDLE / "build.json"
        self.assertTrue(stamp.exists(), "static/console/build.json is missing — run `npm run build`")
        self.assertTrue(json.loads(stamp.read_text())["hash"])

    def test_the_stamp_is_the_current_hash_of_the_sources(self):
        recorded = json.loads((BUNDLE / "build.json").read_text())
        recomputed, count = _source_hash()
        self.assertEqual(
            recorded["hash"],
            recomputed,
            "console/ has changed since the bundle was built, so static/console/ is stale. "
            "Run `cd console && npm run build` and commit the result.",
        )
        self.assertEqual(recorded["sources"], count)

    def test_the_bundle_actually_contains_a_bundle(self):
        """Not a placeholder: the app and its stylesheet are both real files."""
        self.assertGreater((BUNDLE / "app.js").stat().st_size, 50_000)
        self.assertGreater((BUNDLE / "app.css").stat().st_size, 5_000)

    def test_every_theme_chrome_colour_is_that_themes_own_background(self):
        """The address bar has to match the page it sits above.

        `theme.ts` carries one hex per theme for `<meta name="theme-color">`, and
        the colours themselves live in `styles.css` under `[data-theme=...]`. Two
        files holding one value each is how a swatch ends up recolouring the page
        while the browser chrome above it stays the previous shade — the same
        class of defect the public site already shipped once, where a script
        rewrote every page's declaration with a fourth colour matching nobody's
        background.
        """
        script = (CONSOLE / "src" / "lib" / "theme.ts").read_text(encoding="utf-8")
        css = (CONSOLE / "src" / "styles.css").read_text(encoding="utf-8")
        pairs = re.findall(
            r'\{\s*id:\s*"([a-z]+)",\s*label:\s*"[^"]+",\s*hex:\s*"(#[0-9a-f]{6})"',
            script,
        )
        self.assertGreaterEqual(len(pairs), 4, "the theme list was not found in theme.ts")
        for theme_id, hex_value in pairs:
            with self.subTest(theme=theme_id):
                block = css.split(f'[data-theme="{theme_id}"]', 1)[1].split("}", 1)[0]
                self.assertIn(f"--bg: {hex_value}", block)

    def test_the_bundle_carries_no_stray_entry_point(self):
        """Nothing in `static/console/` may be a second way into the app.

        `console/index.html` is the entry `vite dev` uses, and with it as the
        build's *input* Vite copied it into the bundle — where `static/` served it
        to anyone. Opening it loaded `app.js` without the `console-boot` payload
        Flask renders, so the app had no CSRF token and no operator and painted an
        empty page with an error in the console. The build starts from
        `src/main.tsx` now, and this is what says so: only the two assets and the
        stamp belong in here.
        """
        found = sorted(p.name for p in BUNDLE.iterdir() if p.is_file())
        self.assertEqual(found, ["app.css", "app.js", "build.json"])
        # …and the reason it does not: the build starts from the module. Asserted
        # as the input it declares rather than as the absence of a word — an
        # earlier version of this rule forbade the string `index.html`, which the
        # comment explaining the fix contains, so it failed on its own
        # documentation. A test that forbids a word forbids the paragraph about
        # why the word is gone.
        config = (CONSOLE / "vite.config.ts").read_text(encoding="utf-8")
        self.assertIn('input: "src/main.tsx"', config)

    def test_nothing_is_loaded_from_another_origin(self):
        """The page runs under `script-src 'self'`, so a CDN import would not load.

        Checked on the built file rather than on the config, because the config is
        a claim about the build and this is the build.
        """
        bundle = (BUNDLE / "app.js").read_text(errors="replace")
        for origin in ("cdn.jsdelivr.net", "unpkg.com", "esm.sh", "fonts.googleapis.com"):
            with self.subTest(origin=origin):
                self.assertNotIn(origin, bundle)


class WhoMayOpenIt(_DashboardFixture):
    """The scope test, applied to the shell *and* to the data behind it."""

    def assertNothingLeaked(self, response, extra=()):
        body = response.get_data(as_text=True)
        for guild in (self.guild, self.other):
            self.assertNotIn(guild.name, body)
            self.assertNotIn(str(guild.id), body)
        for token in extra:
            self.assertNotIn(token, body)

    def test_no_session_is_sent_to_sign_in(self):
        response = self.client.get("/owner")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))
        self.assertNothingLeaked(response)

    def test_a_one_time_link_never_opens_it(self):
        self.sign_in(guild_id=GUILD_ID, scope="guild")
        response = self.client.get("/owner")
        self.assertEqual(response.status_code, 302)
        self.assertNothingLeaked(response)

    def test_a_discord_sign_in_never_opens_it(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["csrf_token"] = "csrf-token"
            session["auth_scope"] = "user"
            session["admin_guild_ids"] = [GUILD_ID, self.other.id]
        self.assertEqual(self.client.get("/owner").status_code, 302)

    def test_the_operator_gets_it(self):
        self.sign_in(scope="global")
        response = self.client.get("/owner")
        self.assertEqual(response.status_code, 200)

    def test_every_page_url_serves_the_shell(self):
        """The app owns `/owner/*`, so a deep link is a route, not a 404.

        Somebody will paste `/owner/ops` into a chat and somebody else will open
        it. If only `/owner` were routed, that link would be the whole console
        failing for exactly one address.
        """
        self.sign_in(scope="global")
        for path in ("/owner", "/owner/servers", "/owner/systems", "/owner/ops", "/owner/changes"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_every_console_api_route_refuses_a_one_time_link(self):
        """`_require_auth_api` would have allowed this: the link is a real sign-in.

        The difference between the dashboard and this surface is scope, not
        credentials — so the refusal is asserted by *scope*, and on the body.
        """
        self.sign_in(guild_id=GUILD_ID, scope="guild")
        for path in CONSOLE_ROUTES + (f"/api/console/server/{GUILD_ID}",):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 403, path)
                self.assertNothingLeaked(response, extra=("server", "fleet"))

    def test_every_console_api_route_refuses_a_signed_out_request(self):
        for path in CONSOLE_ROUTES + (f"/api/console/server/{GUILD_ID}",):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401, path)


class TheShellItServes(_DashboardFixture):
    """What the one HTML file has to carry, since it carries everything else."""

    def setUp(self):
        super().setUp()
        self.sign_in(scope="global")
        self.page = self.client.get("/owner").get_data(as_text=True)

    def test_it_loads_the_built_app_and_its_stylesheet(self):
        self.assertIn("/static/console/app.js", self.page)
        self.assertIn("/static/console/app.css", self.page)

    def test_the_assets_are_stamped_so_a_new_build_is_not_cached_forever(self):
        self.assertIn(f"app.js?v={keep_alive._console_asset_stamp()}", self.page)

    def test_the_csrf_token_is_reachable_without_an_inline_script(self):
        """The token has to be handed to the app, and a JSON block is the way.

        The session cookie is `HttpOnly`, so the bundle can never read the token
        itself. Putting it in a `<script>` would spend this page's `unsafe-inline`
        allowance on a value the browser should never execute.
        """
        self.assertIn('id="console-boot"', self.page)
        self.assertIn('type="application/json"', self.page)
        self.assertNotIn("csrfToken =", self.page)

    def test_the_boot_payload_is_valid_json_and_names_the_pages(self):
        start = self.page.index('id="console-boot"')
        payload = self.page[start:].split(">", 1)[1].split("</script>", 1)[0]
        boot = json.loads(payload)
        self.assertTrue(boot["csrfToken"])
        self.assertEqual([page["id"] for page in boot["pages"]], [p[0] for p in keep_alive.OWNER_PAGES])
        self.assertIn("gateway", boot)

    def test_the_gateway_state_is_server_rendered_rather_than_guessed(self):
        """So the first paint is true, before the app has fetched anything."""
        start = self.page.index('id="console-boot"')
        boot = json.loads(self.page[start:].split(">", 1)[1].split("</script>", 1)[0])
        self.assertEqual(boot["gateway"], keep_alive._badge_state())

    def test_an_unbuilt_checkout_says_so_instead_of_rendering_nothing(self):
        with _no_bundle():
            page = self.client.get("/owner").get_data(as_text=True)
        self.assertIn("Console not built", page)
        self.assertNotIn("/static/console/app.js", page)


class TheConsoleApi(_DashboardFixture):
    """The data the app draws, checked against the functions that produce it."""

    def setUp(self):
        super().setUp()
        self.sign_in(scope="global")

    def get(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        payload = response.get_json()
        self.assertTrue(payload["success"], path)
        return payload

    def test_the_overview_counts_are_the_registrys(self):
        """Cross-checked against the source of truth, never against a literal.

        The fleet's own sums are checked against the *fleet endpoint* rather than
        against ``_fleet_rows()`` called from the test: that helper reads the
        session, so calling it outside a request would be testing a different
        situation than the one the page is in. Two endpoints agreeing is the
        stronger claim anyway — it is what the console actually shows.
        """
        totals = self.get("/api/console/overview")["totals"]
        fleet = self.get("/api/console/fleet")["servers"]
        self.assertEqual(totals["servers"], len(fleet))
        self.assertEqual(totals["members"], sum(row["members"] for row in fleet))
        self.assertEqual(totals["on"], sum(row["on"] for row in fleet))
        self.assertEqual(totals["systems"], keep_alive.features.system_count())
        self.assertEqual(totals["switchable"], len(keep_alive.AVAILABLE_MODULES))
        self.assertEqual(totals["commands"], keep_alive.features.command_count())
        # The difference between the two system counts must be exactly the
        # always-on set, because that is the arithmetic the console prints.
        self.assertEqual(
            totals["systems"] - totals["switchable"],
            len(keep_alive.features.always_on_names()),
        )

    def test_the_health_blocks_are_the_modules_own_and_carry_the_key_the_app_reads(self):
        """Three self-readings, quoted exactly as their modules report them.

        This was a real defect found by looking at the rendered page rather than
        at the code: the console read `verdict`/`state` out of these blocks, and
        none of the three modules answers with either word — so all three rows
        printed an em dash on a card that otherwise looked fine.
        """
        health = self.get("/api/console/overview")["health"]
        self.assertEqual(health["site"], keep_alive.site_guard.health_json())
        self.assertEqual(health["config"], keep_alive.config_audit.health_json())
        self.assertEqual(health["uptime"], keep_alive.uptime_status.snapshot())
        for name, block in health.items():
            with self.subTest(block=name):
                self.assertIn("status", block, f"`{name}` has no `status` for the page to read")

    def test_the_pages_read_the_status_key_rather_than_one_they_invented(self):
        """The app's side of the same contract, checked where it is written.

        Python cannot see which key a `.tsx` file reads, and the failure mode —
        an em dash where a reading should be — is invisible to every other test
        here. Two files, one assertion each.
        """
        for name in ("Overview.tsx", "Ops.tsx"):
            source = (CONSOLE / "src" / "pages" / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                for block in ("site", "config"):
                    self.assertIn(f"health.{block}.status", source)

    def test_a_server_picture_is_our_own_path_or_nothing(self):
        """`src` is an attribute the browser *acts on*, so the rule sits at the edge.

        Nothing on screen changes today: every icon here is built by the server
        as `/api/image?...`, our own origin. What this pins is that the two
        payloads carrying one put it through the app's own rule in `api.ts`
        rather than each page trusting whatever it was handed — because
        `javascript:` in `src` is a script, and `img-src 'self'` does not save a
        page from the URL its own API gave it. Same shape as the status-key test
        above: a contract Python cannot execute, checked where it is written.
        """
        source = (CONSOLE / "src" / "api.ts").read_text(encoding="utf-8")
        self.assertIn("function drawableImage", source)
        # Both icon-carrying endpoints, not only the one that looks riskier.
        for call in ("fleet: async () => {", "server: async (id: number) => {"):
            with self.subTest(endpoint=call):
                self.assertIn(call, source)
        self.assertEqual(source.count("icon: drawableImage("), 2)
        # A leading slash is ours; two of them are somebody else's host.
        self.assertIn('!path.startsWith("//")', source)

    def test_an_address_that_is_not_a_page_says_so(self):
        """The router answered every unmatched path with the **Overview** page.

        Found by typing one: `/owner/nonsense` rendered a screen that looked
        correct at an address that was not, and the rail's `aria-current`
        correctly highlighted nothing — so the only evidence that this was not a
        page was a highlight nobody reads. It also disagreed with the rest of the
        console, where an unknown *server* already answered "No such server".

        The rule is that `/owner` is the Overview and nothing else falls through
        to it, which Python can only check by reading the router.
        """
        source = (CONSOLE / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertIn('return { kind: "missing" as const }', source)
        self.assertIn(
            'if (path === "" || path === "/") return { kind: "overview" as const }',
            source,
            "the Overview is no longer the one address it belongs to",
        )
        page = (CONSOLE / "src" / "pages" / "Missing.tsx").read_text(encoding="utf-8")
        self.assertIn("Not a page here", page)
        self.assertIn('to="/"', page, "the page offers no way back to the console")

    def test_the_overview_carries_every_figure_window_at_once(self):
        """Switching period is a redraw, so a slow host cannot stall the control."""
        figures = self.get("/api/console/overview")["figures"]
        self.assertTrue(figures["periods"])
        for period in figures["periods"]:
            with self.subTest(period=period["id"]):
                self.assertIn(period["id"], figures["tiles"])

    def test_the_fleet_carries_what_the_list_draws_and_is_ordered_by_people(self):
        servers = self.get("/api/console/fleet")["servers"]
        self.assertTrue(servers, "the fixture has servers to list")
        for row in servers:
            with self.subTest(server=row["name"]):
                for field in ("id", "name", "icon", "nick", "members", "on", "total", "known", "off"):
                    self.assertIn(field, row)
                self.assertLessEqual(row["on"], row["total"])
        self.assertEqual(
            [row["members"] for row in servers],
            sorted((row["members"] for row in servers), reverse=True),
            "the fleet is scanned from the biggest server down",
        )

    def test_a_servers_page_offers_exactly_the_switchable_systems(self):
        payload = self.get(f"/api/console/server/{GUILD_ID}")
        self.assertEqual(
            [system["id"] for system in payload["systems"]],
            [module["id"] for module in keep_alive.AVAILABLE_MODULES],
        )
        self.assertEqual(payload["always_on"], list(keep_alive.features.always_on_names()))
        self.assertEqual(payload["server"]["id"], GUILD_ID)

    def test_a_server_the_bot_is_not_in_is_a_404_rather_than_an_empty_page(self):
        response = self.client.get("/api/console/server/999999999")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()["success"])

    def test_the_matrix_covers_every_server_and_system(self):
        payload = self.get("/api/console/systems")
        fleet = self.get("/api/console/fleet")["servers"]
        self.assertEqual(len(payload["systems"]), len(keep_alive.AVAILABLE_MODULES))
        self.assertEqual(
            [server["id"] for server in payload["servers"]],
            [row["id"] for row in fleet],
            "the matrix's columns must be the same servers, in the same order",
        )
        for guild_id, systems in payload["matrix"].items():
            with self.subTest(guild=guild_id):
                self.assertEqual(len(systems), len(keep_alive.AVAILABLE_MODULES))
        self.assertEqual(payload["unreadable"], [])

    def test_the_ops_tail_redacts_anything_that_looks_like_a_secret(self):
        """The ring is written by every part of the bot, so this is the boundary.

        The assertion is that the **value** is gone, not that it was rewritten in
        a particular way: a redaction that left the secret in place beside a
        marker would pass a test that only looked for the marker.
        """
        with _planted_log_line("DISCORD_TOKEN=abc123def456"):
            joined = "\n".join(self.get("/api/console/ops")["logs"])
        self.assertIn("DISCORD_TOKEN=[redacted]", joined)
        self.assertNotIn("abc123def456", joined)

    def test_a_changelog_entry_is_quoted_rather_than_summarised(self):
        payload = self.get("/api/console/changes")
        entries = payload["entries"]
        if not entries:
            self.skipTest("this checkout has no changelog to quote")
        self.assertTrue(entries[0]["version"])
        self.assertTrue(entries[0]["body"] or entries[0]["lead"])


class TogglingFromTheConsole(_DashboardFixture):
    """The switch is the point of the whole surface, so it is driven here."""

    def setUp(self):
        super().setUp()
        self.sign_in(scope="global")
        with self.client.session_transaction() as session:
            session["csrf_token"] = "csrf-token"

    def test_a_switch_saves_and_is_reported_back(self):
        module = keep_alive.AVAILABLE_MODULES[0]["id"]
        response = self.client.post(
            "/api/toggle",
            json={"guild_id": GUILD_ID, "module": module, "enabled": False},
            headers={"X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertFalse(keep_alive.modules.is_enabled(GUILD_ID, module))

    def test_a_switch_without_the_token_is_refused(self):
        module = keep_alive.AVAILABLE_MODULES[0]["id"]
        response = self.client.post(
            "/api/toggle",
            json={"guild_id": GUILD_ID, "module": module, "enabled": True},
        )
        self.assertEqual(response.status_code, 403)

    def test_a_bulk_switch_saves_every_available_system(self):
        response = self.client.post(
            "/api/toggle-all",
            json={"guild_id": GUILD_ID, "enabled": True},
            headers={"X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(response.status_code, 200)
        states = keep_alive.modules.get_states(GUILD_ID)
        for module in keep_alive.AVAILABLE_MODULES:
            with self.subTest(module=module["id"]):
                self.assertTrue(states.get(module["id"], False))


class _no_bundle:
    """Pretend the app was never built, for the unbuilt-shell test."""

    def __enter__(self):
        self.saved = keep_alive._console_built
        keep_alive._console_built = lambda: False
        return self

    def __exit__(self, *exc):
        keep_alive._console_built = self.saved
        return False


class _planted_log_line:
    """Put one line into the process's log ring, then restore it."""

    def __init__(self, line: str):
        self.line = line

    def __enter__(self):
        self.saved = keep_alive.LOG_RING
        ring = type(keep_alive.LOG_RING)(maxlen=50)
        ring.append(self.line)
        keep_alive.LOG_RING = ring
        return self

    def __exit__(self, *exc):
        keep_alive.LOG_RING = self.saved
        return False


if __name__ == "__main__":
    unittest.main()
