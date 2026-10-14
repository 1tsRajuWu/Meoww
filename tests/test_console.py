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
import io
import json
import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

import keep_alive
from tests.support import agree_to_the_policies, agreed_next_url
from tests.test_composer import CHANNEL_ID, GUILD_ID, _Bot, _Channel, _Guild, _Member, _VoiceChannel
from tests.test_dashboard import CSRF, _DashboardFixture

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "console"
BUNDLE = ROOT / "static" / "console"

CONSOLE_ROUTES = (
    "/api/console/overview",
    "/api/console/fleet",
    "/api/console/live",
    "/api/console/systems",
    "/api/console/commands",
    "/api/console/economy",
    "/api/console/ops",
    "/api/console/access",
    "/api/console/changes",
)

#: The console routes that *write*, so they are POSTs the access tests below
#: cannot reach with a GET.
CONSOLE_WRITE_ROUTES = (
    "/api/console/economy/adjust",
    "/api/console/access/revoke",
    # The operator's controls over a server as a whole, and the one that has to be
    # on this list rather than merely tested somewhere: **leaving a server is not
    # reversible from here** — an invite is the only way back, and a `block` even
    # refuses that — so the assertion that a server-scoped sign-in cannot reach it
    # is the assertion that a link minted for one server cannot make the bot
    # abandon it. It was left off when the route was added, which is the shape of
    # gap this tuple exists to make loud.
    "/api/console/server-action",
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
        self.assertIn("/owner/login", response.headers.get("Location", ""))
        self.assertNothingLeaked(response)

    def test_owner_login_success_with_passkey(self):
        with mock.patch.object(keep_alive, "ADMIN_PASSKEY", "secret-passkey"):
            with self.client.session_transaction() as session:
                session["csrf_token"] = "csrf-token"
            response = self.client.post(
                "/owner/login",
                data={"passkey": "secret-passkey", "csrf_token": "csrf-token"},
            )
            self.assertEqual(response.status_code, 302)
            # The door is the clickwrap now: a passkey opens it and the terms are
            # shown before the fleet is. The destination is carried, not lost.
            location = response.headers.get("Location", "")
            self.assertTrue(location.startswith("/agree"), location)
            self.assertEqual(agreed_next_url(location), "/owner")
            self.assertEqual(
                agree_to_the_policies(self.client, location).headers.get("Location"),
                "/owner",
            )
            # Following redirect to /owner now serves 200
            follow = self.client.get("/owner")
            self.assertEqual(follow.status_code, 200)

    def test_owner_login_renders_csrf_token_and_submits_cleanly(self):
        with mock.patch.object(keep_alive, "ADMIN_PASSKEY", "secret-passkey"):
            get_res = self.client.get("/owner/login")
            self.assertEqual(get_res.status_code, 200)
            html = get_res.get_data(as_text=True)
            import re
            match = re.search(r'name="csrf_token" value="([^"]+)"', html)
            self.assertIsNotNone(match, "owner_login.html must contain non-empty csrf_token")
            token = match.group(1)
            self.assertTrue(len(token) > 10)

            post_res = self.client.post(
                "/owner/login",
                data={"passkey": "secret-passkey", "csrf_token": token},
            )
            self.assertEqual(post_res.status_code, 302)
            # The passkey is the door; the agreement is what it opens onto. Noted
            # here because this is the test that reads the form's own token out of
            # the page rather than planting one in the session.
            location = post_res.headers.get("Location", "")
            self.assertTrue(location.startswith("/agree"), location)
            self.assertEqual(agreed_next_url(location), "/owner")

    def test_owner_login_failure_with_wrong_passkey(self):
        with mock.patch.object(keep_alive, "ADMIN_PASSKEY", "secret-passkey"):
            with self.client.session_transaction() as session:
                session["csrf_token"] = "csrf-token"
            response = self.client.post(
                "/owner/login",
                data={"passkey": "wrong-passkey", "csrf_token": "csrf-token"},
            )
            self.assertEqual(response.status_code, 401)
            self.assertIn("Invalid admin passkey", response.get_data(as_text=True))

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

    def _owner_candidate_session(self):
        """A session that signed in through Discord as the bot's owner.

        This is what the callback produces for ``OWNER_USER_ID``: authenticated,
        identified, and still short of the console.
        """
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["csrf_token"] = CSRF
            session["auth_scope"] = "user"
            session["owner_candidate"] = True
            session["discord_user"] = {"id": "5", "name": "R A J", "avatar": ""}

    def test_the_owners_own_sign_in_does_not_open_the_console(self):
        """The account says *who this is*. The passkey decides what it may do.

        The callback used to hand the operator scope to whichever session matched
        ``OWNER_USER_ID``, which is precisely why entering the owner site asked
        for no pass: the button on the sign-in page was a complete sign-in, and
        anybody holding that Discord session had the fleet.
        """
        self._owner_candidate_session()
        response = self.client.get("/owner")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/owner/login", response.headers.get("Location", ""))
        self.assertNothingLeaked(response)

    def test_the_sign_in_page_says_the_passkey_is_still_wanted(self):
        self._owner_candidate_session()
        body = self.client.get("/owner/login").get_data(as_text=True)
        self.assertEqual(self.client.get("/owner/login").status_code, 200)
        self.assertIn("passkey", body)
        self.assertIn("the console still wants the passkey", body)

    def test_the_passkey_raises_that_session_to_operator(self):
        self._owner_candidate_session()
        with mock.patch.object(keep_alive, "ADMIN_PASSKEY", "secret-passkey"):
            response = self.client.post(
                "/owner/login",
                data={"passkey": "secret-passkey", "csrf_token": CSRF},
            )
        self.assertEqual(response.status_code, 302)
        door = response.headers.get("Location", "")
        self.assertEqual(agreed_next_url(door), "/owner")
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("auth_scope"), "global")
        # The passkey raised the scope *and* the agreement stands between it and
        # the fleet: accepting is what turns the last redirect into a 200.
        self.assertEqual(
            agree_to_the_policies(self.client, door).headers.get("Location"), "/owner"
        )
        self.assertEqual(self.client.get("/owner").status_code, 200)

    def test_every_page_url_serves_the_shell(self):
        """The app owns `/owner/*`, so a deep link is a route, not a 404.

        Somebody will paste `/owner/ops` into a chat and somebody else will open
        it. If only `/owner` were routed, that link would be the whole console
        failing for exactly one address.
        """
        self.sign_in(scope="global")
        for path in (
            "/owner",
            "/owner/servers",
            "/owner/systems",
            "/owner/commands",
            "/owner/ops",
            "/owner/changes",
        ):
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
        for path in CONSOLE_WRITE_ROUTES:
            with self.subTest(path=path):
                response = self.client.post(path, json={"guild_id": str(GUILD_ID)})
                self.assertEqual(response.status_code, 403, path)
                self.assertNothingLeaked(response, extra=("server", "fleet"))

    def test_every_console_api_route_refuses_a_signed_out_request(self):
        for path in CONSOLE_ROUTES + (f"/api/console/server/{GUILD_ID}",):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401, path)
        for path in CONSOLE_WRITE_ROUTES:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}).status_code, 401, path)


class ThePayloadBoundaryKnowsEveryEndpoint(unittest.TestCase):
    """`console/src/lib/payload.ts` is keyed by path, so the paths are a contract.

    The console checks every payload against a schema before a page sees it — the
    bundle is committed and served from a CDN, so a browser can be running last
    week's build against this week's server, and a missing key should arrive as a
    sentence rather than as a crash inside a `.map()`. A table like that has two
    ways to be wrong, and both are silent:

    * a schema for a path the app does not serve, which is dead weight that looks
      like coverage;
    * a route with no schema, which is a payload passing the boundary unchecked —
      the exact gap the table exists to close.
    """

    def schemas(self) -> set[str]:
        text = (CONSOLE / "src" / "lib" / "payload.ts").read_text(encoding="utf-8")
        return set(re.findall(r'^\s*"(/api/[^"]+)":\s*\{', text, flags=re.MULTILINE))

    def test_the_table_was_found(self):
        self.assertGreater(len(self.schemas()), 4, "no schemas were parsed out of payload.ts")

    def test_every_schema_names_a_route_this_app_serves(self):
        """A schema may name a route, or the family a parameterised route is in.

        `/api/console/server` is not a route and is not meant to be: it is the
        family `/api/console/server/<int:guild_id>` belongs to, and the runtime
        lookup matches a concrete path against it. Everything else must be an
        endpoint this app actually serves.
        """
        exact: set[str] = set()
        families: set[str] = set()
        for rule in keep_alive.app.url_map.iter_rules():
            families.add(rule.rule.split("<")[0].rstrip("/"))
            if "<" not in rule.rule:
                exact.add(rule.rule)
        unknown = sorted(
            path
            for path in self.schemas()
            if path not in exact
            and path not in families
            and not any(family == path for family in families)
        )
        self.assertEqual(
            unknown,
            [],
            f"payload.ts describes endpoints nothing serves: {unknown}",
        )

    def test_every_console_family_has_a_schema(self):
        """Exact or by family, the same rule `schemaFor` applies at runtime."""
        schemas = self.schemas()
        families = {
            rule.rule.split("<")[0].rstrip("/")
            for rule in keep_alive.app.url_map.iter_rules()
            if rule.rule.startswith("/api/console/")
        }
        uncovered = sorted(
            family
            for family in families
            if family not in schemas
            and not any(candidate.startswith(family + "/") for candidate in schemas)
        )
        self.assertEqual(
            uncovered,
            [],
            "these console endpoints answer payloads the boundary cannot check: "
            + ", ".join(uncovered),
        )


# --------------------------------------------------------------------------- #
# RETIRED — `TheShellItServes`, and where what it held now lives.
#
# Six tests stood here and every one of them was a claim about the archived
# React console's shell: that `/owner` loaded `static/console/app.js` and its
# stylesheet, that both carried a build stamp, that the CSRF token reached the
# app through a `console-boot` JSON block rather than an inline script, that the
# boot payload named every page and carried the gateway's own state, that an
# unbuilt checkout said so, and that the app's not-a-page screen did not restate
# the size of the rail. `/owner` serves server-rendered pages again — by request,
# because the bundled app was reported as a console that looked like the
# dashboard and could not be driven from a keyboard — so five of them were
# claims about a file no route serves, and the sixth read a `.tsx` inside an
# archive that is not part of the build.
#
# What they protected is not dropped, and it is worth naming where it went:
# every page in `OWNER_PAGES` answering with its own heading and lede, a write
# that is a form carrying its CSRF token, a refusal happening *before* anything
# is done, and `/owner` refusing anybody but the operator are all pinned in
# `tests/test_owner_console.py`, against the pages that are actually served. The
# bundle-stamp claims stay in this file, under `TheBundleMatchesItsSources`,
# which still reads the archive it was written for.
# --------------------------------------------------------------------------- #

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
        # The config block is the public half *plus* `named`, which is the one
        # thing `/health` refuses to say. Compared key by key rather than whole,
        # so a field the module stops reporting fails here instead of passing as
        # an extra key nobody notices.
        for key, value in keep_alive.config_audit.health_json().items():
            with self.subTest(config_key=key):
                self.assertEqual(health["config"].get(key), value)
        self.assertEqual(health["config"]["named"], keep_alive.config_audit.attention_json())
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

    def test_the_config_card_can_name_what_is_missing(self):
        """The word "attention" must arrive with the list it is about.

        `config_audit.health_json()` counts and deliberately does not name: it is
        what `/health` serves, and "this deployment has no sign-in secret" is not
        a sentence to hand a stranger. Both console payloads sit behind the
        operator's sign-in, so both carry the names as well — without which the
        card reports a number, sends its reader somewhere else, and leaves the
        settings that are costing them features unnamed in the place they were
        already looking. The page's side of that is checked where it is written.
        """
        for path in ("/api/console/overview", "/api/console/ops"):
            payload = self.get(path)
            with self.subTest(path=path):
                named = payload["health"]["config"]["named"]
                self.assertEqual(named, keep_alive.config_audit.attention_json())
                for item in named:
                    with self.subTest(setting=item.get("name")):
                        self.assertIn("cost", item)
                        # Presence only. The module never reads a value, secret
                        # or not, and a page that carried one would be the worse
                        # defect by a distance.
                        self.assertNotIn("value", item)

        for name in ("Overview.tsx", "Ops.tsx"):
            source = (CONSOLE / "src" / "pages" / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertIn("ConfigAttention", source)

    def test_the_reticle_is_an_instrument_that_follows_rather_than_a_second_pointer(self):
        """Nothing is drawn *on* the point, in the sheet or in the script.

        Two pointers at one coordinate is not a flourish, it is a smear: the
        browser's arrow owns the point (it is what decides what a click hits), and
        a decoration centred there lands half over it. The paw went first; the dot
        and the ring that replaced it went when this pass counted what they cost —
        the sheet hid both in every style the picker offers, neither was ever
        given a width or a height, and the loop still wrote their position every
        frame. What a reader sees is the chosen entity, and the weight of a move
        is carried by the layers that trail on a spring beside it.
        """
        css = (CONSOLE / "src" / "styles.css").read_text(encoding="utf-8")
        for gone in ("cursor__paw", "meow-cursor-dot", "meow-cursor-ring"):
            with self.subTest(part=gone):
                self.assertNotIn(gone, css, f"{gone} is back on the point")
        source = (CONSOLE / "src" / "app" / "Cursor.tsx").read_text(encoding="utf-8")
        for gone in ("meow-cursor-dot", "meow-cursor-ring", "cursor__ring"):
            with self.subTest(part=gone):
                self.assertNotIn(gone, source, f"{gone} is built again")
        self.assertIn("blobStretch", source, "the layer no longer reacts to how fast it is moving")
        self.assertIn("Math.atan2", source, "the stretch is not aligned with the direction of travel")
        # The loop has to be able to stop: a settled page must not hold a frame
        # per animation tick for the life of the session.
        self.assertIn("const settled", source)
        self.assertIn("cancelAnimationFrame", source)

    def test_the_cursor_cannot_paint_before_the_pointer_moves(self):
        """A cursor at rest is a decoration parked on the page.

        Two halves that have to agree: the stylesheet hides it, and the move
        handler is the only thing that reveals it — so a page that has just
        loaded, or one reached with the keyboard, is clean. The element carries
        an inline transform from its first frame either way, which is exactly why
        the hiding has to be the stylesheet's job rather than a `useState(false)`
        somebody can invert.

        Also pinned here: `cursor: none` appears nowhere. The ring is a
        *companion* to the pointer, not a replacement for it, and a page that
        hides the real cursor loses text selection and resize handles.
        """
        css = (CONSOLE / "src" / "styles.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.cursor \{[^}]*opacity: 0")
        self.assertNotIn("cursor: none", css)

        source = (CONSOLE / "src" / "app" / "Cursor.tsx").read_text(encoding="utf-8")
        move = source.split("const onMove", 1)[1].split("const onDown", 1)[0]
        self.assertIn('opacity = "1"', move)
        # Two bounds the live page taught, both asserted where they are written:
        # a pointer coordinate outside the window is a leave rather than a
        # position (an automation harness parks the mouse at x=-9933, and the
        # first draft chased it across the viewport), and `event.target` is only
        # sometimes an Element — on the document it has no `.closest`, and the
        # unguarded call threw a TypeError out of the handler on every such move.
        self.assertIn("window.innerWidth", move)
        self.assertIn("instanceof Element", source)

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
        # The signature is asserted rather than only the call: `id` is a
        # **string** because a Discord id cannot survive a JavaScript number.
        for call in ("fleet: async () => {", "server: async (id: string) => {"):
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
        self.assertEqual(payload["server"]["id"], str(GUILD_ID))

    def test_a_server_the_bot_is_not_in_is_a_404_rather_than_an_empty_page(self):
        response = self.client.get("/api/console/server/999999999")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()["success"])

    def test_a_servers_page_on_a_deployment_that_can_see_none_says_which(self):
        """A deep link into a console that has no servers is not a wrong address.

        The rail's `Servers` page draws an empty fleet as an empty list, so a
        link to one row of it cannot answer "No such server" — there is no list
        for the id to be missing from yet.
        """
        with mock.patch.object(keep_alive, "BOT_INSTANCE", None):
            with mock.patch.object(keep_alive.guild_directory, "read_all", return_value=[]):
                response = self.client.get(f"/api/console/server/{GUILD_ID}")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Discord", response.get_json()["error"])

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

    def test_every_id_crosses_to_the_app_as_text(self):
        """The defect that made the whole console look broken, pinned.

        A Discord id is 18-19 digits and a JavaScript number is exact to about
        16, so an id sent as a JSON *number* comes back as a different server:
        ``String(Number("1542278896148422676"))`` is ``"1542278896148422700"``.
        Three symptoms, one cause, and all three were on screen at once — every
        link in the fleet opened **"No such server"**, the systems matrix looked
        its own string keys up with a rounded number and painted **"?" in every
        cell**, and the per-server page was unreachable from the list that
        offered it. So: every id in every console payload is text, it is exactly
        itself, and the matrix's column keys are the very strings the app was
        handed.
        """
        fleet = self.get("/api/console/fleet")["servers"]
        systems = self.get("/api/console/systems")
        server = self.get(f"/api/console/server/{GUILD_ID}")["server"]
        ids = [row["id"] for row in fleet]
        for row_id in ids + [server["id"]] + [column["id"] for column in systems["servers"]]:
            with self.subTest(id=row_id):
                self.assertIsInstance(row_id, str, "an id crossed as a number")
                # Round-tripping through the boundary has to be the identity,
                # which is exactly what a double cannot promise.
                self.assertEqual(keep_alive._console_id(int(row_id)), row_id)
        self.assertEqual(
            sorted(systems["matrix"]),
            sorted(ids),
            "the matrix is keyed by something other than the ids the app looks up",
        )

    def test_the_commands_page_is_the_registry_and_what_discord_was_told(self):
        """One row per command, availability by the publisher's own rule.

        Cross-checked against `command_scope` rather than against a copy of it:
        the page has to agree with what actually decides a server's picker, or
        it is a second opinion about the same question.
        """
        payload = self.get("/api/console/commands")
        names = [row["name"] for row in payload["commands"]]
        self.assertEqual(len(names), len(set(names)), "a command is listed twice")
        # Grouped by system, so the rows read like the registry they come from.
        systems_in_order = [row["system"] or "~" for row in payload["commands"]]
        self.assertEqual(systems_in_order, sorted(systems_in_order))
        for name in keep_alive.features.ROOT_COMMANDS:
            with self.subTest(command=name):
                self.assertIn(name, names)

        fleet = self.get("/api/console/fleet")["servers"]
        self.assertEqual(
            [server["id"] for server in payload["servers"]],
            [row["id"] for row in fleet],
            "the columns must be the servers the fleet lists, in the same order",
        )
        switchable = {module["id"] for module in keep_alive.AVAILABLE_MODULES}
        for row in payload["commands"]:
            with self.subTest(command=row["name"]):
                # A system column is a real switchable system or nothing at all.
                self.assertIn(row["system"], switchable | {""})
                if not row["declared"]:
                    # The only way to have a row nobody declared is to have read
                    # it out of Discord's tree.
                    self.assertIs(row["published"], True)
                for server in payload["servers"]:
                    try:
                        states = keep_alive.modules.get_states(int(server["id"]))
                    except Exception:  # pragma: no cover - the fixture reads fine
                        continue
                    available = keep_alive.command_scope.is_available(row["name"], states)
                    with self.subTest(server=server["name"]):
                        self.assertEqual(
                            server["name"] in row["off_in"],
                            not available,
                            f"{row['name']} disagrees with command_scope in {server['name']}",
                        )

        self.assertEqual(payload["totals"]["declared"], len(keep_alive.features.ROOT_COMMANDS))
        self.assertEqual(payload["totals"]["commands"], keep_alive.features.command_count())
        self.assertEqual(payload["always_on"], list(keep_alive.features.always_on_names()))

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


class _VoiceState:
    """One entry of ``Guild.voice_states``, as the gateway sends it.

    Deliberately *not* carrying ``.user``: the payload the gateway sends holds a
    user id and a channel id and nothing else, so a stand-in that helpfully
    included the member would hide the one case the page has to get right — a
    voice state the member cache cannot resolve.
    """

    def __init__(self, user_id, channel, **flags):
        self.user_id = user_id
        self.channel = channel
        self.user = None
        self.mute = flags.get("mute", False)
        self.deaf = flags.get("deaf", False)
        self.self_mute = flags.get("self_mute", False)
        self.self_deaf = flags.get("self_deaf", False)
        self.self_stream = flags.get("self_stream", False)
        self.self_video = flags.get("self_video", False)
        self.suppress = flags.get("suppress", False)


class TheLivePage(_DashboardFixture):
    """What one server is *doing* — and what the page refuses to invent.

    This is the only console page whose readings come from caches that are
    legitimately empty (a cold gateway, the Presence intent off, no tracker
    tables), so most of what is checked here is the *refusal*: a reading that was
    never taken has to arrive as absent, with the page saying which one it was,
    rather than as a confident zero about somebody's server.
    """

    def setUp(self):
        super().setUp()
        self.sign_in(scope="global")

    def get(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        payload = response.get_json()
        self.assertTrue(payload["success"], path)
        return payload

    def gateway(self, *, statuses=False):
        """A server with two voice rooms, one of them occupied."""
        talk = _Channel(CHANNEL_ID, "general", category="Chat")
        room = _VoiceChannel(555005, "General Voice", category="Voice")
        quiet = _VoiceChannel(555006, "Empty Room", category="Voice")
        here = _Member(333001, "raj", "RAJ")
        away = _Member(333002, "ash", "Ash")
        ghost = 333003  # in the room, but the member cache has never seen them
        members = [here, away]
        if statuses:
            here.status = SimpleNamespace(name="online")
            away.status = SimpleNamespace(name="dnd")
        guild = _Guild(
            GUILD_ID,
            "Meow Server",
            [talk],
            members=members,
            voice_channels=[room, quiet],
        )
        guild.member_count = 3
        # The one list the page reads. A real guild carries text, voice and
        # category channels together here, which is why the fake has to as well.
        guild.channels = [talk, room, quiet]
        guild.voice_states = {
            here.id: _VoiceState(here.id, room, self_stream=True),
            ghost: _VoiceState(ghost, room, self_mute=True),
        }
        bot = _Bot([guild])
        # The shape a real deployment has: the roster always, presence only when
        # it was asked for and granted (see the two tests below).
        bot.intents = SimpleNamespace(members=True, presences=statuses)
        bot.latency = 0.05
        return bot, guild, room

    def payload(self, *, statuses=False, guild_id=None):
        bot, guild, room = self.gateway(statuses=statuses)
        with mock.patch.object(keep_alive, "BOT_INSTANCE", bot):
            data = self.get(
                f"/api/console/live?guild_id={guild_id}" if guild_id else "/api/console/live"
            )
        return data, guild, room

    def test_it_lists_every_channel_and_who_is_sitting_in_the_voice_ones(self):
        data, guild, room = self.payload()
        by_name = {channel["name"]: channel for channel in data["channels"]}
        self.assertEqual(sorted(by_name), ["Empty Room", "General Voice", "general"])
        self.assertEqual(by_name["General Voice"]["kind"], "voice")
        self.assertEqual(by_name["Empty Room"]["people"], [])
        self.assertEqual(data["totals"]["in_voice"], 2)
        self.assertEqual(data["totals"]["voice"], 2)
        self.assertEqual(data["read"]["channels"], True)

        people = {person["id"]: person for person in by_name["General Voice"]["people"]}
        self.assertEqual(people[str(333001)]["name"], "RAJ")
        self.assertIs(people[str(333001)]["streaming"], True)
        self.assertIs(people[str(333001)]["muted"], False)

    def test_a_voice_state_the_cache_cannot_name_is_an_id_rather_than_a_name(self):
        """Without the Members intent the gateway knows *that* somebody is in a
        room and not who — so the row carries the id it has."""
        data, _guild, _room = self.payload()
        room = next(ch for ch in data["channels"] if ch["name"] == "General Voice")
        nameless = [person for person in room["people"] if person["id"] == "333003"]
        self.assertEqual(len(nameless), 1)
        self.assertEqual(nameless[0]["name"], "")
        self.assertIs(nameless[0]["muted"], True)

    def test_every_id_crosses_as_text(self):
        data, _guild, _room = self.payload()
        for channel in data["channels"]:
            with self.subTest(channel=channel["name"]):
                self.assertIsInstance(channel["id"], str)
                self.assertEqual(keep_alive._console_id(int(channel["id"])), channel["id"])
                for person in channel["people"]:
                    self.assertIsInstance(person["id"], str)
        self.assertIsInstance(data["server"]["id"], str)

    def test_presence_is_absent_rather_than_zero_when_nobody_can_say(self):
        """The intent is privileged, so this is the *normal* state.

        This is the bug that shipped, and it is why the guard is the **intent**
        rather than the attribute: without the Presence intent Discord does not
        leave ``Member.status`` off the object, it answers ``offline`` for every
        member — so the first version of this page printed "1 online, 35 offline"
        for a server with people in it, the one online being the bot, whose
        presence its own process sets. Five figures, none of them a reading.
        """
        without, _guild, _room = self.payload()
        self.assertIsNone(without["presence"])
        self.assertIs(without["read"]["presence"], False)
        # And the rest of the page is unaffected: the voice roster comes from
        # voice states, which need no privileged intent at all.
        self.assertEqual(without["totals"]["in_voice"], 2)
        self.assertEqual(without["read"]["voice"], True)

        with_status, _guild, _room = self.payload(statuses=True)
        self.assertEqual(with_status["read"]["presence"], True)
        self.assertEqual(with_status["presence"]["online"], 1)
        self.assertEqual(with_status["presence"]["dnd"], 1)
        self.assertEqual(with_status["presence"]["total"], 2)

    def test_presence_also_needs_the_members_intent(self):
        """Discord's own rule: presences arrive for the members being tracked."""
        bot, _guild, _room = self.gateway(statuses=True)
        bot.intents = SimpleNamespace(members=False, presences=True)
        with mock.patch.object(keep_alive, "BOT_INSTANCE", bot):
            data = self.get("/api/console/live")
        self.assertIsNone(data["presence"], "presence without members is not a reading")
        self.assertIs(data["read"]["presence"], False)

    def test_the_message_counts_are_the_trackers_own_window(self):
        """Today is one UTC day and the week is today plus six — the same ranges
        ``/activity week`` means, read out of the same two tables."""
        bot, _guild, _room = self.gateway()
        today = keep_alive.figures.period_start("today")
        stale = "2001-01-01"
        conn = keep_alive.db.connect()
        try:
            conn.execute(
                "INSERT INTO activity_messages (guild_id, user_id, channel_id, date, count) "
                "VALUES (?, ?, ?, ?, ?)",
                (GUILD_ID, 333001, CHANNEL_ID, today, 7),
            )
            conn.execute(
                "INSERT INTO activity_messages (guild_id, user_id, channel_id, date, count) "
                "VALUES (?, ?, ?, ?, ?)",
                (GUILD_ID, 333001, CHANNEL_ID, stale, 400),
            )
            conn.execute(
                "INSERT INTO activity_voice (guild_id, user_id, channel_id, date, seconds) "
                "VALUES (?, ?, ?, ?, ?)",
                (GUILD_ID, 333001, 555005, today, 3600),
            )
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(keep_alive, "BOT_INSTANCE", bot):
            data = self.get("/api/console/live")

        self.assertIs(data["read"]["tracker"], True)
        general = next(ch for ch in data["channels"] if ch["name"] == "general")
        self.assertEqual(general["messages_today"], 7)
        self.assertEqual(general["messages_week"], 7, "a row from 2001 landed in this week")
        self.assertEqual(data["totals"]["messages_today"], 7)
        room = next(ch for ch in data["channels"] if ch["name"] == "General Voice")
        self.assertEqual(room["voice_seconds_week"], 3600)
        self.assertEqual([talker["name"] for talker in data["talkers"]], ["RAJ"])

    def test_no_tracker_tables_is_reported_rather_than_counted_as_zero(self):
        """A deployment with no `bot.db` — every build machine — has no schema.

        The flag is the whole point: the page prints an em dash where the reading
        is absent, so a zero here would be a number nobody measured about
        somebody's server. This is the case `utils/figures` already documents.
        """
        bot, _guild, _room = self.gateway()
        conn = keep_alive.db.connect()
        try:
            conn.execute("DROP TABLE activity_messages")
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(keep_alive, "BOT_INSTANCE", bot):
            data = self.get("/api/console/live")
        self.assertIs(data["read"]["tracker"], False)
        self.assertEqual(data["talkers"], [])

    def test_a_server_the_bot_is_not_in_is_a_404(self):
        response = self.client.get("/api/console/live?guild_id=999999999")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()["success"])

    def test_a_deployment_that_can_see_no_server_says_that_rather_than_404(self):
        """The defect a playtest hit: ``No such server`` about the *deployment*.

        A process with no bot in it and no directory written yet answered
        "That reading failed. No such server" for this page — a claim about a
        server that was in Discord the whole time — while the Fleet page drew the
        same emptiness as an empty list. Two surfaces, one state, two answers:
        the page gets the gateway's own sentence now, and a status that says
        *come back in a moment* rather than *that address is wrong*.
        """
        with mock.patch.object(keep_alive, "BOT_INSTANCE", None):
            with mock.patch.object(keep_alive.guild_directory, "read_all", return_value=[]):
                response = self.client.get("/api/console/live")
        self.assertEqual(response.status_code, 503)
        said = response.get_json()["error"]
        self.assertIn("Discord", said)
        self.assertNotIn("No such server", said)

    def test_the_listing_cap_counts_what_it_does_not_list(self):
        """A big server must not turn into a megabyte of JSON, and the page has
        to be able to say how many channels it is not showing."""
        bot, guild, _room = self.gateway()
        extra = [_Channel(CHANNEL_ID + 100 + i, f"channel-{i}") for i in range(5)]
        guild.text_channels = list(guild.text_channels) + extra
        guild.channels = list(guild.channels) + extra
        with mock.patch.object(keep_alive, "LIVE_CHANNEL_LIMIT", 3):
            with mock.patch.object(keep_alive, "BOT_INSTANCE", bot):
                data = self.get("/api/console/live")
        self.assertEqual(len(data["channels"]), 3)
        self.assertEqual(data["exceeded"], 5)

    def test_the_page_says_a_reading_is_absent_rather_than_printing_a_zero(self):
        """The app's half of the contract, checked where it is written: Python
        cannot see which branch a `.tsx` file takes, and a page that ignored the
        flags would render a confident zero from the same payload."""
        source = (CONSOLE / "src" / "pages" / "Live.tsx").read_text(encoding="utf-8")
        for flag in ("tracker", "voice", "channels"):
            with self.subTest(flag=flag):
                self.assertIn(
                    f'fromFlag(read, "{flag}"',
                    source,
                    f"Live.tsx draws a figure without asking whether `{flag}` was read",
                )
        self.assertIn("<Figure", source)
        # …and the mark itself comes from the one component that draws it, so a
        # page cannot print a different dash, or a zero, for the same absence.
        ui = (CONSOLE / "src" / "ui.tsx").read_text(encoding="utf-8")
        self.assertIn("NOT_READ", ui)
        self.assertIn("stat--unread", ui)
        self.assertIn("title={read.ok ? undefined : read.because}", ui)
        # …and it is a page of the console, not a file nothing routes to.
        app = (CONSOLE / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertIn('path === "/live"', app)
        self.assertIn("<Live />", app)
        self.assertIn("live", [page[0] for page in keep_alive.OWNER_PAGES])


class TheEconomyPage(_DashboardFixture):
    """The one console control that moves money, and the readings behind it.

    Its assertions are about the *table* as much as about the response: a page
    that reported "+500" while writing nothing, or that wrote while reporting a
    clamped zero, would pass a test that only read the JSON. So each write is
    checked twice — once in the answer, once in the database the bot's own
    commands read — and every refusal is checked for the number that caused it.
    """

    def setUp(self):
        super().setUp()
        self.sign_in(scope="global")

    # -- the database, read the way the page's own module reads it ---------- #

    def seed(self, user_id, wallet=0, bank=0, guild_id=GUILD_ID):
        conn = keep_alive.db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) "
                "VALUES (?, ?, ?, ?)",
                (user_id, guild_id, wallet, bank),
            )
            conn.commit()
        finally:
            conn.close()

    def held(self, user_id, guild_id=GUILD_ID):
        conn = keep_alive.db.connect()
        try:
            row = conn.execute(
                "SELECT wallet, bank FROM economy WHERE user_id = ? AND guild_id = ?",
                (user_id, guild_id),
            ).fetchone()
            return (int(row[0]), int(row[1])) if row else (0, 0)
        finally:
            conn.close()

    def get(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_json()

    def post(self, body, path="/api/console/economy/adjust"):
        return self.client.post(path, json=body, headers={"X-CSRF-Token": CSRF})

    # -- readings ----------------------------------------------------------- #

    def test_the_totals_are_the_tables_own_including_the_bank_it_forgot_before(self):
        """Three figures, because a banked fee is not a burned one.

        `utils/meowfee.py` banks the fee rather than destroying it, so coins that
        have left every member's balance are still in the server. A page that
        summed wallets and called it "in circulation" would print a total that
        does not add up — which is why `meow_bank` is a column of its own and is
        checked against the real table here.
        """
        self.seed(5001, wallet=250, bank=750)
        self.seed(5002, wallet=1000, bank=0)
        conn = keep_alive.db.connect()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS meow_bank (guild_id INTEGER PRIMARY KEY, coins INTEGER NOT NULL DEFAULT 0)")
            conn.execute(
                "INSERT OR REPLACE INTO meow_bank (guild_id, coins) VALUES (?, ?)",
                (GUILD_ID, 44),
            )
            conn.commit()
        finally:
            conn.close()

        payload = self.get("/api/console/economy")
        totals = payload["totals"]
        self.assertEqual(totals["wallet"], 1250)
        self.assertEqual(totals["bank"], 750)
        self.assertEqual(totals["circulation"], 2000)
        self.assertEqual(totals["meow_bank"], 44)
        self.assertEqual(totals["accounts"], 2)
        self.assertEqual(payload["server"]["id"], str(GUILD_ID))

    def test_the_richest_list_is_net_worth_order_with_ids_as_text(self):
        self.seed(6001, wallet=10, bank=1000)
        self.seed(6002, wallet=500, bank=0)
        self.seed(6003, wallet=1, bank=1)
        top = self.get("/api/console/economy")["top"]
        self.assertEqual([row["user_id"] for row in top], ["6001", "6002", "6003"])
        for row in top:
            with self.subTest(member=row["user_id"]):
                self.assertIsInstance(row["user_id"], str, "a member id crossed as a number")
                self.assertEqual(row["net"], row["wallet"] + row["bank"])

    def test_the_fee_column_follows_the_servers_own_switch(self):
        """Absent means on — `meowfee.is_enabled`'s rule, read without an event loop."""
        self.assertTrue(self.get("/api/console/economy")["fee"]["enabled"])

        conn = keep_alive.db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
                (GUILD_ID, keep_alive.meowfee.SETTING_KEY, json.dumps(False)),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertFalse(self.get("/api/console/economy")["fee"]["enabled"])

    def test_an_unknown_server_is_a_404_rather_than_the_biggest_one(self):
        response = self.client.get("/api/console/economy?guild_id=999999999")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()["success"])

    # -- writes ------------------------------------------------------------- #

    def test_a_credit_lands_in_the_table_the_bot_reads(self):
        self.seed(7001, wallet=100, bank=0)
        response = self.post(
            {"guild_id": str(GUILD_ID), "user_id": "7001", "action": "wallet_add", "amount": 500}
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual((payload["wallet"], payload["bank"]), (600, 0))
        self.assertEqual(self.held(7001), (600, 0))
        # And the reading the page draws from agrees with the write it just made.
        reading = keep_alive.db.read_guild_economy_sync(GUILD_ID)
        self.assertEqual(reading["wallet"], 600)

    def test_a_credit_creates_the_row_for_a_member_who_had_none(self):
        response = self.post(
            {"guild_id": str(GUILD_ID), "user_id": "7002", "action": "bank_add", "amount": 25}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.held(7002), (0, 25))

    def test_removing_more_than_holds_is_refused_and_names_the_balance(self):
        """The defect this project has paid for twice, in the console's words.

        `MAX(0, wallet + amount)` confirmed a removal that never happened, and a
        second simultaneous removal could "succeed" too; a conditional debit is
        the fix everywhere else, and a refusal has to say *what refused it* —
        "could not save" on a page holding the number is the worse answer.
        """
        self.seed(7003, wallet=250, bank=0)
        response = self.post(
            {"guild_id": str(GUILD_ID), "user_id": "7003", "action": "wallet_remove", "amount": 10_000}
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("250", response.get_json()["error"])
        self.assertEqual(self.held(7003), (250, 0))

    def test_a_removal_that_fits_takes_exactly_what_was_asked(self):
        self.seed(7004, wallet=250, bank=900)
        response = self.post(
            {"guild_id": str(GUILD_ID), "user_id": "7004", "action": "bank_remove", "amount": 900}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.held(7004), (250, 0))

    def test_set_writes_exact_balances_and_leaves_the_one_it_was_not_given(self):
        self.seed(7005, wallet=5, bank=5)
        response = self.post(
            {"guild_id": str(GUILD_ID), "user_id": "7005", "action": "set", "wallet": 42}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.held(7005), (42, 5))
        response = self.post(
            {"guild_id": str(GUILD_ID), "user_id": "7005", "action": "set", "bank": 7}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.held(7005), (42, 7))

    def test_the_member_id_must_be_an_id_and_the_amount_a_whole_number(self):
        cases = (
            ({"user_id": "<@7006>", "action": "wallet_add", "amount": 5}, 400),
            ({"user_id": "", "action": "wallet_add", "amount": 5}, 400),
            ({"user_id": "7006", "action": "wallet_add", "amount": 0}, 400),
            ({"user_id": "7006", "action": "wallet_add", "amount": -5}, 400),
            ({"user_id": "7006", "action": "nonsense", "amount": 5}, 400),
            ({"user_id": "7006", "action": "set"}, 400),
            ({"user_id": "7006", "action": "wallet_add", "amount": 10 ** 15}, 400),
        )
        for body, status in cases:
            with self.subTest(body=body):
                response = self.post({"guild_id": str(GUILD_ID), **body})
                self.assertEqual(response.status_code, status)
                self.assertFalse(response.get_json()["success"])
                self.assertNotIn("7006", [row["user_id"] for row in self.get("/api/console/economy")["top"]])

    def test_a_write_without_the_csrf_token_is_refused(self):
        self.seed(7007, wallet=100, bank=0)
        response = self.client.post(
            "/api/console/economy/adjust",
            json={"guild_id": str(GUILD_ID), "user_id": "7007", "action": "wallet_add", "amount": 500},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.held(7007), (100, 0), "a refused write still moved coins")

    def test_the_change_is_written_to_the_output_tail(self):
        """An out-of-band balance change has to be accountable afterwards.

        The ring `/owner/ops` shows is the one place a person can see that it
        happened and who did it, so the line is asserted for the actor and the
        numbers rather than merely for existing.
        """
        self.seed(7008, wallet=0, bank=0)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as captured:
            response = self.post(
                {"guild_id": str(GUILD_ID), "user_id": "7008", "action": "wallet_add", "amount": 7}
            )
        self.assertEqual(response.status_code, 200)
        line = captured.getvalue()
        self.assertIn("[ECONOMY]", line)
        self.assertIn("wallet_add", line)
        self.assertIn("7008", line)


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


class RepublishingFromTheConsole(_DashboardFixture):
    """The operator's one fix for a command list Discord cached, pressed for real.

    ``bot.py`` sets ``bot.resync_commands = resync_guild_commands``, and that is
    a **plain function**: it queues the work on the bot's own loop and returns
    whether it managed to. This endpoint looked the attribute up itself and
    ``await``-ed it, so the await raised `TypeError` inside its own ``except``
    and the button answered *"Discord refused the republish (TypeError)"* on
    every press — a sentence that names Discord for a mistake nobody else made.
    A hook that *is* awaitable would have passed, which is why the stand-in here
    is shaped like the real one rather than like a convenient one.
    """

    def setUp(self):
        super().setUp()
        self.sign_in(scope="global")
        self.asked: list[int] = []
        self.bot.resync_commands = lambda guild_id: self.asked.append(guild_id) or True

    def republish(self, guild_id=GUILD_ID):
        return self.client.post(
            "/api/console/server-action",
            json={"guild_id": guild_id, "action": "resync"},
            headers={"X-CSRF-Token": CSRF},
        )

    def test_the_button_asks_the_bot_to_republish_that_server(self):
        response = self.republish()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.asked, [GUILD_ID])
        self.assertIn("republish", response.get_json()["said"])

    def test_a_queue_that_did_not_happen_is_reported_as_a_refusal(self):
        self.bot.resync_commands = lambda _guild_id: False
        response = self.republish()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["success"])

    def test_a_bot_with_no_hook_at_all_is_refused_rather_than_crashing(self):
        del self.bot.resync_commands
        response = self.republish()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["success"])

    def test_the_form_the_page_renders_is_the_one_that_was_driven(self):
        """Read from the served page, so a renamed action moves this test with it."""
        body = self.client.get("/owner").get_data(as_text=True)
        form = next(
            (f for f in re.findall(r"<form\b.*?</form>", body, re.S)
             if "/api/console/server-action" in f),
            None,
        )
        self.assertIsNotNone(form, "the console's Republish form is not on the page")
        action = re.search(r'name="action" value="([^"]+)"', form)
        self.assertIsNotNone(action, "the form names no action")
        self.assertEqual(action.group(1), "resync")


class TheConsoleSchemaIsWhatTheServerAnswers(_DashboardFixture):
    """A payload the page cannot read is a click that quietly does nothing.

    ``console/src/lib/payload.ts`` is the boundary every answer crosses before a
    page sees it, and it is typed — so a field the server sends as a JSON
    *number* under a schema that says string is not a cosmetic mismatch. The
    check throws, the caller's ``catch`` runs, and whatever that click was going
    to do next is skipped. That is precisely how *Voice Company* could be on in
    the database, on in this console, and absent from Discord: ``/api/toggle``
    answered ``guild_id`` as a number, the schema said string, and the click that
    would have read the publish stamp — the one thing that says whether Discord
    was told — died in the boundary check and showed "the console and the bot are
    out of step" instead.

    So the shape is read out of the TypeScript and compared against the server's
    own answer, field by field, rather than pinned by hand in two places.
    """

    PY_TYPES = {
        "string": str,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }

    def setUp(self):
        super().setUp()
        self.sign_in(scope="global")
        with self.client.session_transaction() as session:
            session["csrf_token"] = "csrf-token"

    def declared(self, path: str) -> dict[str, str]:
        """The schema ``payload.ts`` declares for one path, as {field: type}."""
        text = (CONSOLE / "src" / "lib" / "payload.ts").read_text(encoding="utf-8")
        block = re.search(
            r'"' + re.escape(path) + r'":\s*\{(.*?)\}', text, flags=re.S
        )
        self.assertIsNotNone(block, f"payload.ts declares no schema for {path}")
        return dict(re.findall(r'(\w+):\s*"(\w+)"', block.group(1)))

    def _assert_matches(self, path: str, payload: dict) -> None:
        for field, declared in self.declared(path).items():
            with self.subTest(path=path, field=field):
                self.assertIn(field, payload, f"{path} does not answer {field!r}")
                expected = self.PY_TYPES[declared]
                self.assertIsInstance(
                    payload[field],
                    expected,
                    f"{path} answers {field!r} as {type(payload[field]).__name__}; "
                    f"the console's schema says {declared!r}, and a mismatch throws "
                    "before the page can use it",
                )
                if declared == "boolean":
                    self.assertIs(type(payload[field]), bool)

    def test_the_single_switch_answers_what_its_schema_declares(self):
        module = keep_alive.AVAILABLE_MODULES[0]["id"]
        payload = self.client.post(
            "/api/toggle",
            json={"guild_id": GUILD_ID, "module": module, "enabled": True},
            headers={"X-CSRF-Token": "csrf-token"},
        ).get_json()
        self._assert_matches("/api/toggle", payload)
        self.assertIsInstance(payload["guild_id"], str, "a snowflake crosses as text")
        self.assertIn("republish", payload)

    def test_the_bulk_switch_answers_what_its_schema_declares(self):
        payload = self.client.post(
            "/api/toggle-all",
            json={"guild_id": GUILD_ID, "enabled": False},
            headers={"X-CSRF-Token": "csrf-token"},
        ).get_json()
        self._assert_matches("/api/toggle-all", payload)
        self.assertIsInstance(payload["guild_id"], str)

    def test_a_republish_nobody_could_hear_is_reported_as_such(self):
        """The switch saves either way; the *picker* only changes if the bot heard.

        A deploy is exactly when somebody toggles something — and a booting bot
        has no tree to republish from, so the honest answer is "saved, and the
        command list was not re-sent", which the console turns into a warning
        instead of a green toast about a command that cannot be typed yet.
        """
        module = keep_alive.AVAILABLE_MODULES[0]["id"]
        with mock.patch.object(keep_alive, "BOT_INSTANCE", None):
            payload = self.client.post(
                "/api/toggle",
                json={"guild_id": GUILD_ID, "module": module, "enabled": True},
                headers={"X-CSRF-Token": "csrf-token"},
            ).get_json()
        self.assertTrue(payload["success"])
        self.assertIs(payload["republish"], False)
        self.assertTrue(keep_alive.modules.is_enabled(GUILD_ID, module))

    def test_the_live_reading_answers_what_its_schema_declares(self):
        """The endpoint this console draws the most of, and the last one checked.

        Every card on the Live page reads a block of this one payload, so a field
        the server stops sending is eleven quiet blanks rather than one loud
        failure — the original complaint in this class, one endpoint over.
        """
        payload = self.client.get("/api/console/live").get_json()
        self._assert_matches("/api/console/live", payload)
        self.assertIsInstance(payload["servers"], list)


class ThePreviewHarnessShowsAConsole(_DashboardFixture):
    """The harness the README points at must be able to show this console.

    Found by running it to look at this pass's own work. Every `/api/console/*`
    route is `keep_alive`'s real view and every one of them lists servers through
    `BOT_INSTANCE` — which the harness never had, so the Fleet page listed
    nothing, the Systems matrix had no columns, and the Commands and Economy
    pages had no server to be about. A preview of an empty console is worse than
    no preview: it looks like the page is broken.
    """

    def load(self):
        """Load the harness as its own module, restoring the database path.

        The file repoints `utils.db` at its own copy on import, which the
        dashboard's tests already live with — this fixture's database is set in
        `setUp`, so it is put back here rather than left to the next test.
        """
        import importlib.util

        saved = keep_alive.db.DB_PATH
        spec = importlib.util.spec_from_file_location(
            "preview_dashboard_for_console_test",
            os.path.join(ROOT, "scripts", "preview_dashboard.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        keep_alive.db.DB_PATH = saved
        return module

    def test_the_stub_gateway_carries_the_servers_the_harness_lists(self):
        module = self.load()
        bot = module._PreviewBot()
        self.assertEqual([guild.id for guild in bot.guilds], [entry["id"] for entry in module.GUILDS])
        self.assertTrue(bot.is_ready())
        self.assertEqual(len(bot.tree.get_commands()), len(module.PREVIEW_COMMANDS))
        guild = bot.get_guild(module.GUILDS[0]["id"])
        self.assertEqual(guild.member_count, module.GUILDS[0]["members"])
        # _fleet_rows reads a nickname and the economy page reads the richest
        # list's names, so a lookup that answers nothing would empty both.
        self.assertIsNotNone(guild.get_member(module.MEMBERS[0]["id"]))

    def test_importing_it_does_not_install_the_gateway(self):
        """The suite imports this file; a gateway must not arrive with it."""
        module = self.load()
        self.assertIs(module.keep_alive.BOT_INSTANCE, self.bot)

    def test_a_switch_in_a_preview_hears_the_republish_it_asks_for(self):
        """The harness gateway must answer the hook the real gateway answers.

        Found by flipping a system in a preview: every switch came back with
        *"saved, but the command list was not republished — the bot may be
        restarting"*, because ``_PreviewBot`` had no ``resync_commands`` and
        ``keep_alive`` reads a missing hook as a process nobody can hear. That is
        the honest answer during a boot and a lie about this harness — and it hid
        the success path, which is the whole reason the harness exists.
        """
        module = self.load()
        bot = module._PreviewBot()
        guild_id = module.GUILDS[0]["id"]
        with mock.patch.object(keep_alive, "BOT_INSTANCE", bot):
            self.assertTrue(keep_alive._resync_commands(guild_id))
        # …and a server the gateway does not hold still reads as unhearable,
        # rather than the hook claiming every id it is handed.
        with mock.patch.object(keep_alive, "BOT_INSTANCE", bot):
            self.assertFalse(keep_alive._resync_commands(0))

    def test_a_switch_in_a_preview_answers_the_fields_the_page_reads(self):
        """The harness's own ``/api/toggle`` carries `republish` and `publish_at`.

        The console types that answer as having both, and words its toast off the
        first one: absent, every switch in a preview said *"saved, but the command
        list was not republished — the bot may be restarting"*, which is the
        warning path reported by a gateway that has never restarted.
        """
        module = self.load()
        payload = {
            "guild_id": module.GUILDS[0]["id"],
            "module": module.MODULES[0]["id"],
            "enabled": True,
        }
        with mock.patch.object(keep_alive, "BOT_INSTANCE", module._PreviewBot()):
            with keep_alive.app.test_request_context(
                "/api/toggle", method="POST", json=payload
            ):
                answered = module.api_toggle().get_json()
        self.assertTrue(answered["success"], answered)
        self.assertIs(answered["republish"], True, answered)
        self.assertIn("publish_at", answered)

    def test_the_seeded_economy_is_written_once(self):
        module = self.load()
        first = module.seed_economy()
        self.assertEqual(first, len(module.GUILDS) * len(module.MEMBERS))
        self.assertEqual(module.seed_economy(), 0, "a second run re-wrote balances")

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
