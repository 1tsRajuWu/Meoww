"""The dashboard has to answer, and answer only for the right server.

Three defects found by driving every route with a stand-in gateway are pinned
here:

1. ``/dashboard`` answered **500** whenever the gateway latency was not a real
   number — ``nan`` until the first heartbeat is acknowledged, ``inf`` while the
   socket is rebuilding. ``nan`` is truthy, so the ``if latency:`` guard around
   ``round()`` never caught it, and the page an administrator opens *because*
   something looks wrong was the page that crashed.
2. A **guild-scoped** sign-in link could end up driving every other server the
   bot is in: ``dashboard()`` wrote the ``0`` fallback of a failed lookup back
   into the session, which made the next request look unscoped.
3. Our own ``Content-Security-Policy`` blocked the market chart's only library,
   so the chart could never draw on any deployment and the card blamed the CDN.

The fakes live in ``tests.test_composer``; both modules drive the same Flask app.
"""
import asyncio
import json
import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock

import keep_alive
from tests.support import TempDatabaseMixin
from utils import figures
from tests.test_composer import (
    CHANNEL_ID,
    GUILD_ID,
    MEMBER_ID,
    ROLE_ID,
    _Bot,
    _Channel,
    _Guild,
    _Member,
    _Role,
    _VoiceChannel,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")
CSRF = "csrf-token"
#: Any ``.js`` a template asks the browser to load, however it asks for it — a
#: ``<script src>`` tag or a URL string handed to a loader.
EXTERNAL_JS = re.compile(r"https://([A-Za-z0-9.\-]+)/[^\"'\s)]*\.js")


def make_bot(*, guilds=None, started=True):
    guild = _Guild(
        GUILD_ID,
        "Meow Server",
        [_Channel(CHANNEL_ID, "general", category="Chat")],
        roles=[_Role(ROLE_ID, "Members")],
        members=[_Member(MEMBER_ID, "raj")],
    )
    guild.member_count = 4
    other = _Guild(999002, "Other Server", [_Channel(999901, "general")])
    other.member_count = 1
    bot = _Bot([guild] + list(guilds or []) + [other])
    bot._has_started = started
    bot.latency = 0.05
    return bot, guild, other


def page_href(page_id: str) -> str:
    """One page of the dashboard, by its id, as the route that renders it.

    The dashboard is seven pages now. A test that wants the composer, the queue or
    the status panel has to ask for the page that owns it — and has to read the
    path out of `DASHBOARD_PAGES` rather than type it, so a page that moves does
    not leave the suite quietly asserting against a URL nothing serves.
    """
    return next(
        path for candidate, _label, path, _anchor in keep_alive.DASHBOARD_PAGES
        if candidate == page_id
    )


class _DashboardFixture(TempDatabaseMixin, unittest.TestCase):
    """A signed-in session against a fake gateway, with a throwaway database.

    Test-free on purpose (the same split ``_ComposerFixture`` uses): a base class
    that carries tests re-runs every one of them in each subclass, and the suite
    then counts re-collections as passes.
    """

    def setUp(self):
        self.use_temp_database("dashboard.db")
        asyncio.run(self._setup_db())

        self.keep_alive = keep_alive
        self.bot, self.guild, self.other = make_bot()
        keep_alive._login_attempts.clear()
        keep_alive._send_attempts.clear()
        keep_alive._passkey_attempts.clear()
        self._patched = mock.patch.object(keep_alive, "BOT_INSTANCE", self.bot)
        self._patched.start()
        # The landing page's figures are cached for a minute in `utils.figures`,
        # and the cache is process-wide while this fixture's gateway and database
        # are both per-test. A reading left behind by the previous test is a
        # reading about a database that no longer exists, so it goes here, beside
        # the other caches this fixture already invalidates.
        figures.reset()
        self.client = keep_alive.app.test_client()

    async def _setup_db(self):
        from utils import db, modules

        await db.setup()
        modules.invalidate()

    def tearDown(self):
        from utils import modules

        self._patched.stop()
        modules.invalidate()
        figures.reset()
        self.release_temp_database()

    def sign_in(self, *, guild_id=GUILD_ID, scope="guild"):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["csrf_token"] = CSRF
            session["auth_scope"] = scope
            if guild_id is not None:
                session["guild_id"] = guild_id

    def session_state(self):
        with self.client.session_transaction() as session:
            return session.get("auth_scope"), session.get("guild_id")

    def api(self, method, url, body=None):
        headers = {"X-CSRF-Token": CSRF}
        if method == "GET":
            return self.client.get(url, headers=headers)
        return self.client.post(url, json=body or {}, headers=headers)


class DashboardCase(_DashboardFixture):
    """The dashboard's own behaviours: the page, the toggles, the API guards."""

    # ── the page must load ───────────────────────────────────────────────
    def test_the_page_answers_while_the_gateway_has_no_real_latency(self):
        self.sign_in()
        for latency in (float("nan"), float("inf"), -1.0, None):
            self.bot.latency = latency
            response = self.client.get("/dashboard")
            self.assertEqual(response.status_code, 200, f"latency={latency!r} broke the page")

    def test_a_real_latency_is_shown(self):
        self.sign_in()
        self.bot.latency = 0.041
        body = self.client.get("/dashboard").data
        self.assertIn(b"41", body, "latency value should appear")
        self.assertIn(b"ms", body, "ms unit should appear")

    def test_an_unknown_latency_reads_as_unknown_not_as_zero(self):
        self.sign_in()
        self.bot.latency = float("nan")
        body = self.client.get("/dashboard").data
        self.assertNotIn(b"0 ms", body, "a missing heartbeat is not a 0 ms ping")
        self.assertIn(b"Not answering", body, "it should say the ping is unknown")

    def test_the_ping_helper_survives_a_bot_with_no_latency_attribute(self):
        self.sign_in()
        delattr(self.bot, "latency")
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_an_offline_bot_still_renders_the_page(self):
        self.bot._has_started = False
        self.bot.latency = float("nan")
        self.sign_in()
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    # ── scoping ──────────────────────────────────────────────────────────
    def test_a_scoped_link_cannot_reach_another_server(self):
        """The link was minted for a server the bot has since left."""
        self.sign_in(guild_id=123456789)

        page = self.client.get(f"/dashboard?guild_id={GUILD_ID}")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"Other Server", page.data, "another server's name leaked")
        self.assertIn(b"Unknown server", page.data)

        self.assertEqual(
            self.session_state(), ("guild", 123456789),
            "a failed lookup must not rewrite the session",
        )

        # 403 rather than 400: the write is refused for *scope* now, in a
        # sentence that names the reason, instead of falling back to "no server
        # available" — the same refusal either way, and this one is readable.
        toggled = self.api("POST", "/api/toggle", {"guild_id": GUILD_ID, "module": "fun", "enabled": False})
        self.assertEqual(toggled.status_code, 403)
        self.assertIn("do not administer", toggled.get_json()["error"])

        channels = self.api("GET", f"/api/channels?guild_id={GUILD_ID}")
        self.assertEqual(channels.status_code, 400)

        # The composer's write is refused for scope too, and for the same reason:
        # a message must never be posted by a session that named a server it
        # does not run, however the refusal is worded.
        sent = self.api(
            "POST",
            "/api/send-message",
            {"guild_id": GUILD_ID, "channel_ids": [CHANNEL_ID], "content": "hi"},
        )
        self.assertEqual(sent.status_code, 403)
        self.assertIn("do not administer", sent.get_json()["error"])
        self.assertEqual(self.guild.text_channels[0].sent, [], "nothing may be posted")

    def test_loading_the_page_does_not_unpin_a_scoped_session(self):
        """The exact regression: one page load used to demote the scope."""
        self.sign_in(guild_id=123456789)
        self.client.get(f"/dashboard?guild_id={GUILD_ID}")
        scope, guild_id = self.session_state()
        self.assertEqual(scope, "guild")
        self.assertEqual(guild_id, 123456789, "the 0 fallback was persisted")

    def test_a_scoped_session_still_works_for_its_own_server(self):
        self.sign_in()
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        toggled = self.api("POST", "/api/toggle", {"guild_id": GUILD_ID, "module": "fun", "enabled": False})
        self.assertTrue(toggled.get_json()["success"])
        self.assertEqual(str(toggled.get_json()["guild_id"]), str(GUILD_ID))

    # ── a switch is also a command-set change ────────────────────────────

    def test_a_switch_asks_the_bot_to_republish_that_servers_commands(self):
        """The commands are registered per server, so hiding depends on this call.

        Turning a module off is what removes its commands from *this* server's
        picker — there is no permission bit that could do it — so a switch that
        flipped without asking the bot to republish would leave the commands on
        offer and the dashboard's own scope panel wrong.
        """
        self.sign_in()
        asked: list[int] = []
        self.bot.resync_commands = lambda gid: asked.append(gid) or True

        self.api("POST", "/api/toggle", {"guild_id": GUILD_ID, "module": "fun", "enabled": False})
        self.assertEqual(asked, [GUILD_ID])

        self.api("POST", "/api/toggle-all", {"guild_id": GUILD_ID, "enabled": False})
        self.assertEqual(asked, [GUILD_ID, GUILD_ID])

    def test_a_bot_with_no_republish_hook_does_not_break_the_switch(self):
        """The preview harness and the tests' stand-in gateway have no bot loop."""
        self.sign_in()
        self.assertFalse(hasattr(self.bot, "resync_commands"))
        response = self.api(
            "POST", "/api/toggle", {"guild_id": GUILD_ID, "module": "fun", "enabled": False}
        )
        self.assertTrue(response.get_json()["success"])

    def test_a_republish_that_raises_does_not_fail_the_switch(self):
        """The switch is already saved; a refused republish is the next boot's job."""
        self.sign_in()

        def explode(_gid):
            raise RuntimeError("no loop")

        self.bot.resync_commands = explode
        response = self.api(
            "POST", "/api/toggle", {"guild_id": GUILD_ID, "module": "fun", "enabled": False}
        )
        self.assertTrue(response.get_json()["success"])

    def test_a_scoped_session_with_no_server_at_all_is_refused(self):
        self.sign_in(guild_id=None)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        # A session that may act on nothing names a server: refused for scope,
        # which is the same answer as before with a reason attached.
        refused = self.api(
            "POST", "/api/toggle", {"guild_id": GUILD_ID, "module": "fun", "enabled": True}
        )
        self.assertEqual(refused.status_code, 403)
        self.assertIn("do not administer", refused.get_json()["error"])

    def test_an_unscoped_session_may_choose_a_server(self):
        self.sign_in(scope="global")
        page = self.client.get("/dashboard?guild_id=999002")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Other Server", page.data)
        self.assertEqual(self.session_state()[1], 999002)

    def test_a_guild_id_that_is_not_a_number_is_ignored(self):
        self.sign_in(scope="global", guild_id=None)
        page = self.client.get("/dashboard?guild_id=../../etc/passwd")
        self.assertEqual(page.status_code, 200)
        self.assertIn(
            self.session_state()[1], (GUILD_ID, 999002),
            "a junk guild_id falls back to a server the bot is really in",
        )


class OverviewLive(_DashboardFixture):
    """The Overview's own panel, on the page nobody opens *to configure* anything.

    The page answered four questions about the **deployment** — ping, systems,
    members, commands — and none about the server, so an administrator opening the
    page they open first learned how fast the gateway was and nothing about the
    room they were about to walk into. It read as empty because it was.

    The panel reads the gateway, so these pin both halves: what it says when it
    could read, and what it says when it could not. A cold process reporting zero
    people in voice would be inventing a measurement, which is the one thing a
    status panel may never do.
    """

    def _with_voice(self, *, members, name="Lobby"):
        voice = _VoiceChannel(7001, name)
        voice.members = members
        guild = _Guild(
            GUILD_ID,
            "Meow Server",
            [_Channel(CHANNEL_ID, "general", category="Chat")],
            roles=[_Role(ROLE_ID, "Members")],
            members=[_Member(MEMBER_ID, "raj")],
            voice_channels=[voice],
        )
        guild.member_count = 4
        voice.guild = guild
        bot = _Bot([guild])
        bot._has_started = True
        bot.latency = 0.05
        with mock.patch.object(keep_alive, "BOT_INSTANCE", bot):
            return keep_alive._overview_live(GUILD_ID)

    def test_a_room_and_its_people_are_read_off_the_gateway(self):
        raj = _Member(MEMBER_ID, "raj")
        raj.voice = SimpleNamespace(self_stream=False)
        live = self._with_voice(members=[raj])
        self.assertTrue(live["read"])
        self.assertEqual(live["people"], 1)
        self.assertEqual([room["name"] for room in live["rooms"]], ["Lobby"])
        self.assertTrue(live["rooms"][0]["alone"], "one person in a room is the reading")

    def test_the_bot_is_not_one_of_the_people(self):
        """Counting the bot would make every room with only the bot in it "occupied"."""
        me = _Member(4242, "Meow")
        me.bot = True
        me.voice = SimpleNamespace(self_stream=False)
        live = self._with_voice(members=[me])
        self.assertEqual(live["people"], 0)
        self.assertEqual(live["rooms"], [])

    def test_a_room_is_flagged_live_rather_than_described(self):
        someone = _Member(MEMBER_ID, "raj")
        someone.voice = SimpleNamespace(self_stream=True)
        live = self._with_voice(members=[someone])
        self.assertTrue(live["rooms"][0]["live"])

    def test_a_cold_process_says_it_could_not_read_rather_than_showing_zero(self):
        with mock.patch.object(keep_alive, "BOT_INSTANCE", None):
            live = keep_alive._overview_live(GUILD_ID)
        self.assertFalse(live["read"])
        self.assertEqual(live["people"], 0)
        self.assertTrue(live["why"], "an unread panel owes its reader a sentence")

    def test_the_page_draws_both_panels_with_a_reading_in_them(self):
        """The two panels an administrator actually opens this page for.

        A read that found nothing is a *reading* and says so: the fix for an empty
        Overview is not to invent a number, it is to draw the two panels and let
        them say what they found — and the cold-process sentence above is asserted
        on the helper, where the branch lives.
        """
        self.sign_in()
        body = self.client.get("/dashboard").data.decode()
        self.assertIn("In voice right now", body)
        self.assertIn("The player", body)
        self.assertIn("Nobody is in a voice channel. That is the reading, not a failure.", body)
        self.assertNotIn("Nothing was read for this render.", body, "this render did read")

    def test_the_four_destinations_are_offered_with_this_session_server(self):
        """The panel is navigation, so a link that dropped the server would move the
        reader to another server's page — the exact failure the rail already guards."""
        self.sign_in()
        body = self.client.get(f"/dashboard?guild_id={GUILD_ID}").data.decode()
        for path in ("members", "server", "composer", "schedule"):
            self.assertIn(f'href="/dashboard/{path}?guild_id={GUILD_ID}"', body)


class CacheHeaders(TempDatabaseMixin, unittest.TestCase):
    """A cached page is indistinguishable from a feature that never shipped.

    The dashboard HTML went out with no cache directive at all, so a browser was
    free to answer the next visit from its own copy — and a rewritten UI looked
    like it had never been deployed, with no error anywhere to explain it.

    Every response the app serves goes through the one hook this pins, which is
    why the throwaway database matters even here: rendering the page reads module
    state, and no test in this suite may touch a file inside the project.
    """

    def setUp(self):
        self.use_temp_database("cache-headers.db")
        self.client = keep_alive.app.test_client()

    def tearDown(self):
        self.release_temp_database()

    def test_pages_are_never_cached(self):
        for path in ("/login", "/dashboard"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(
                    response.headers.get("Cache-Control"), "no-store", path
                )

    def test_a_page_fetched_with_a_session_is_not_cached_either(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["auth_scope"] = "global"
            session["csrf_token"] = "t"
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_a_response_that_asked_for_its_own_cache_keeps_it(self):
        """The public market JSON asks for a short cache on purpose — short
        enough that the page is live, long enough that a wall of tabs is not a
        wall of requests — and the blanket rule must not take it away.

        Checked against the hook itself rather than through the two endpoints:
        asking them for a board reads and writes a database, and this is the
        mechanism, not the market, that the change could break.
        """
        for own in ("public, max-age=5", "public, max-age=30"):
            with self.subTest(asked_for=own):
                response = keep_alive.app.response_class("{}", mimetype="application/json")
                response.headers["Cache-Control"] = own
                with keep_alive.app.test_request_context("/api/market/quotes"):
                    keep_alive.set_security_headers(response)
                self.assertEqual(response.headers["Cache-Control"], own)

    def test_a_response_without_an_opinion_is_not_cacheable(self):
        response = keep_alive.app.response_class("<h1>hi</h1>", mimetype="text/html")
        with keep_alive.app.test_request_context("/anything"):
            keep_alive.set_security_headers(response)
        self.assertEqual(response.headers["Cache-Control"], "no-store")


class SignInLink(unittest.TestCase):
    """The link has to name the process that minted the token in memory.

    Run from the development Mac, every link used to point at the Render URL —
    whose process had never seen the token — so `/dashboard` handed out links
    that answered "that sign-in link has expired or already been used", and
    nothing had expired.
    """

    def test_an_explicit_url_wins(self):
        with mock.patch.dict(os.environ, {"DASHBOARD_BASE_URL": "https://x.example/"}, clear=True):
            self.assertEqual(keep_alive.dashboard_base_url(), "https://x.example")

    def test_render_supplies_its_own_url_without_configuration(self):
        env = {"RENDER": "true", "RENDER_EXTERNAL_URL": "https://meow.onrender.com/"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(keep_alive.dashboard_base_url(), "https://meow.onrender.com")

    def test_anywhere_else_points_at_the_local_port(self):
        with mock.patch.dict(os.environ, {"PORT": "9000"}, clear=True):
            self.assertEqual(keep_alive.dashboard_base_url(), "http://localhost:9000")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(keep_alive.dashboard_base_url(), "http://localhost:8080")


class DeploymentConfig(unittest.TestCase):
    """The dashboard's credentials have to be in the deploy manifest.

    ``render.yaml`` declared the Discord token and the backup bucket and nothing
    else, so on Render there was no ``FLASK_SECRET_KEY`` (a new signing key every
    boot: every deploy and every free-tier wake-up signed the administrator out)
    and no ``ADMIN_PASSKEY`` (passkey sign-in disabled outright).
    """

    def setUp(self):
        with open(os.path.join(ROOT, "render.yaml"), encoding="utf-8") as handle:
            self.manifest = handle.read()

    def test_the_dashboard_secrets_are_declared(self):
        """Every credential a dashboard feature needs must be asked for.

        Two of these were missing once and both cost something real: no
        FLASK_SECRET_KEY meant a new signing key every boot, so every deploy —
        and every free-tier wake-up — signed the administrator out and killed
        any one-time link in flight; no ADMIN_PASSKEY meant passkey sign-in was
        off outright.

        The list now includes the Discord pair, because the same omission was
        still live: `DISCORD_CLIENT_SECRET` was undeclared, so Render never
        prompted for it, `discord_auth.enabled()` needs both halves, and
        "Sign in with Discord" had never appeared on the deployment — a feature
        documented in `.env.example` and the README, off in production, with the
        only symptom being a login method that is simply not offered.
        """
        for key in (
            "FLASK_SECRET_KEY",
            "ADMIN_PASSKEY",
            "DISCORD_CLIENT_ID",
            "DISCORD_CLIENT_SECRET",
        ):
            self.assertIn(f"- key: {key}", self.manifest, f"{key} is not declared in render.yaml")

    #: Keys whose value is **public by design**, and so belongs in the repository
    #: rather than in the operator's secret list. An application's client id is in
    #: every invite link anybody can read; it is not a credential. Leaving it out
    #: of the manifest is not the safe choice — the templates *drop* the invite
    #: button when the id is unknown, so omitting it shipped a landing page with no
    #: way to add the bot and no error anywhere to show for it.
    #: ``PLAN_ENFORCEMENT`` and ``PLAN_CHECKOUT`` are on this list because they
    #: are switches, not credentials: committing ``0`` or ``1`` gives the deploy a
    #: definite state instead of a prompt, and a prompt is where a switch gets
    #: answered differently on two hosts. The checkout URLs are public by the same
    #: argument the client id is — the page renders them for anyone to click.
    #: ``OWNER_DISCORD_ELEVATION`` joins them for the same reason: it is a switch
    #: whose committed ``0`` is the *safe* state — the passkey is the door — and a
    #: prompt is where a switch gets answered differently on two hosts, one of
    #: which then opens the console without the passkey.
    PUBLIC_KEYS = frozenset({
        "DISCORD_CLIENT_ID",
        "LEMONSQUEEZY_CHECKOUT_PRO",
        "LEMONSQUEEZY_CHECKOUT_ENTERPRISE",
        "PLAN_CHECKOUT",
        "PLAN_ENFORCEMENT",
        "OWNER_DISCORD_ELEVATION",
        # ``SILENT_UPDATES`` and ``UPDATE_WINDOW_IST`` join them as a switch and a
        # clock rather than credentials, and for the same reason: what they hold
        # *is* the state this deployment was asked to be in — quiet, at 03:33 IST
        # — and a prompt is where a behaviour gets answered differently on two
        # hosts, one of which would then announce every deploy into a channel at
        # whatever hour somebody pushed.
        "SILENT_UPDATES",
        "UPDATE_WINDOW_IST",
    })

    def committed_secrets(self, manifest: str) -> list:
        """Keys carrying a literal value that are not public on purpose.

        The rule, in one place, so it can be exercised against a manifest that
        *does* commit a credential. Scanning the real file alone proves nothing:
        it holds no secret, so the check would pass just as happily if the rule
        had been deleted.
        """
        return [
            key
            for key, _value in re.findall(r"- key: (\S+)\n\s+value: (\S+)", manifest)
            if key not in self.PUBLIC_KEYS
        ]

    def test_declared_secrets_are_prompts_not_committed_values(self):
        entries = re.findall(r"- key: (\S+)\n\s+sync: (\S+)", self.manifest)
        self.assertTrue(entries, "render.yaml should declare env vars")
        for key, sync in entries:
            self.assertEqual(sync, "false", f"{key} should be operator-supplied")

        # A literal value is allowed only for a key that is public on purpose,
        # which keeps the original rule for everything else rather than relaxing
        # it: a *secret* still cannot be committed, whatever it is called.
        self.assertEqual(
            self.committed_secrets(self.manifest),
            [],
            "a literal value in render.yaml is a committed credential",
        )

        # And a name that promises secrecy may never be allow-listed as public.
        for key in sorted(self.PUBLIC_KEYS):
            upper = key.upper()
            self.assertFalse(
                any(word in upper for word in ("SECRET", "TOKEN", "KEY", "PASSKEY")),
                f"{key} is allow-listed as a public value but its name says otherwise",
            )

    def test_a_committed_credential_is_still_refused(self):
        """The allow-list above is precise, not a blanket relaxation.

        Without this the rule could rot into "any value is fine" and the suite
        would keep passing, because the real manifest contains no secret for the
        scan to find.
        """
        for key in ("ADMIN_PASSKEY", "FLASK_SECRET_KEY", "SUPABASE_SERVICE_KEY", "DISCORD_TOKEN"):
            manifest = (
                "services:\n  - type: web\n    envVars:\n"
                f"      - key: {key}\n        value: hunter2\n"
            )
            self.assertEqual(
                self.committed_secrets(manifest),
                [key],
                f"a committed {key} was not refused",
            )

        # The one key that is allowed may still be committed, so the rule is not
        # simply refusing every literal value.
        allowed = next(iter(self.PUBLIC_KEYS))
        manifest = (
            "services:\n  - type: web\n    envVars:\n"
            f"      - key: {allowed}\n        value: \"123\"\n"
        )
        self.assertEqual(self.committed_secrets(manifest), [])


class ContentSecurityPolicy(unittest.TestCase):
    """Our own header must not block what our own pages load."""

    def policy_directive(self, name: str) -> str:
        response = keep_alive.app.test_client().get("/health")
        policy = response.headers["Content-Security-Policy"]
        for directive in policy.split(";"):
            directive = directive.strip()
            if directive.startswith(name + " "):
                return directive
        self.fail(f"no {name} in {policy!r}")

    def test_every_external_script_a_template_loads_is_allowed(self):
        script_src = self.policy_directive("script-src")
        allowed = set(re.findall(r"https://([A-Za-z0-9.\-]+)", script_src))
        self.assertIn("'self'", script_src)

        found = 0
        for name in sorted(os.listdir(TEMPLATES)):
            if not name.endswith(".html"):
                continue
            with open(os.path.join(TEMPLATES, name), encoding="utf-8") as handle:
                text = handle.read()
            for origin in sorted(set(EXTERNAL_JS.findall(text))):
                found += 1
                self.assertIn(
                    origin,
                    allowed,
                    f"{name} loads {origin}, which script-src forbids — the chart can never draw",
                )
        self.assertGreater(found, 0, "the scan must actually find the chart library")

    def test_the_rest_of_the_policy_is_still_locked_down(self):
        self.assertIn("default-src 'self'", self.policy_directive("default-src"))
        self.assertIn("frame-ancestors 'none'", self.policy_directive("frame-ancestors"))
        self.assertIn("connect-src 'self'", self.policy_directive("connect-src"))
        # No wildcard was opened up to fix the chart.
        self.assertNotIn("*", self.policy_directive("script-src"))


class VoiceDebugJSON(DashboardCase):
    """A voice client with no heartbeat yet must not make the endpoint unreadable."""

    def setUp(self):
        super().setUp()
        self._passkey = mock.patch.object(self.keep_alive, "ADMIN_PASSKEY", "test-passkey")
        self._passkey.start()

    def tearDown(self):
        self._passkey.stop()
        super().tearDown()

    class _VoiceClient:
        def __init__(self, latency, average):
            self.guild = SimpleNamespace(name="Meow Server")
            self.channel = SimpleNamespace(name="General")
            self.latency = latency
            self.average_latency = average
            self._connection = SimpleNamespace(dave_protocol_version=None, can_encrypt=True)
            self._player = None

        def is_connected(self):
            return True

        def is_playing(self):
            return False

        def is_paused(self):
            return False

    def test_non_finite_latencies_are_null_not_bare_nan(self):
        self.bot.voice_clients = [self._VoiceClient(float("nan"), float("inf"))]
        response = self.client.get("/api/voice-debug", headers={"X-Admin-Passkey": "test-passkey"})
        self.assertEqual(response.status_code, 200)
        # json.loads is exactly what a browser does — bare NaN fails there.
        payload = json.loads(response.data)
        client = payload["voice_clients"][0]
        self.assertIsNone(client["latency"])
        self.assertIsNone(client["average_latency"])

    def test_a_real_latency_is_still_reported(self):
        self.bot.voice_clients = [self._VoiceClient(0.04321, 0.05)]
        payload = json.loads(
            self.client.get("/api/voice-debug", headers={"X-Admin-Passkey": "test-passkey"}).data
        )
        self.assertEqual(payload["voice_clients"][0]["latency"], 0.043)


class PasskeyAndLogScope(_DashboardFixture):
    """The passkey API is throttled, and the process log is not for scoped sessions.

    ``ADMIN_PASSKEY`` is the same secret the login form verifies, but the three
    endpoints that accept it had no attempt limit at all — so /api/set-module, which
    toggles modules in every guild the bot is in, was an unlimited oracle for a
    long-lived credential that is typed once into a host's dashboard and never
    rotated. The form, which is the weaker interface, was the one with the limit.
    """

    def setUp(self):
        super().setUp()
        self.keep_alive._passkey_attempts.clear()
        self.addCleanup(self.keep_alive._passkey_attempts.clear)
        self._passkey = mock.patch.object(self.keep_alive, "ADMIN_PASSKEY", "correct-horse")
        self._passkey.start()
        self.addCleanup(self._passkey.stop)

    def test_a_wrong_passkey_is_refused(self):
        resp = self.client.get("/api/voice-debug", headers={"X-Admin-Passkey": "nope"})
        self.assertEqual(resp.status_code, 401)

    def test_guessing_the_passkey_is_throttled(self):
        limit = self.keep_alive.PASSKEY_MAX_ATTEMPTS
        for attempt in range(limit - 1):
            resp = self.client.get("/api/voice-debug", headers={"X-Admin-Passkey": "nope"})
            self.assertEqual(
                resp.status_code, 401,
                f"attempt {attempt + 1} of {limit} is still inside the window",
            )
        # The attempt that spends the last slot is told the window is closed, and
        # so is every attempt after it.
        for _ in range(2):
            self.assertEqual(
                self.client.get(
                    "/api/voice-debug", headers={"X-Admin-Passkey": "nope"}
                ).status_code,
                429,
            )
        # The right passkey is refused too while the window is spent: the limit is
        # on the client, not on the guess, or it would not slow an attacker down.
        self.assertEqual(
            self.client.get(
                "/api/voice-debug", headers={"X-Admin-Passkey": "correct-horse"}
            ).status_code,
            429,
        )
        self.assertEqual(
            sum(len(bucket) for bucket in self.keep_alive._passkey_attempts.values()),
            limit,
            "a blocked client must not keep growing the counter either",
        )

    def test_a_correct_passkey_clears_the_window(self):
        self.client.get("/api/live-logs", headers={"X-Admin-Passkey": "nope"})
        self.assertEqual(
            self.client.get(
                "/api/live-logs", headers={"X-Admin-Passkey": "correct-horse"}
            ).status_code,
            200,
        )
        self.assertEqual(
            dict(self.keep_alive._passkey_attempts), {},
            "a successful sign-in must not leave the failure count standing",
        )

    def test_a_guild_scoped_session_cannot_read_the_process_log(self):
        """The ring is written while serving every guild; the scope is not global."""
        self.sign_in(scope="guild")
        resp = self.client.get("/api/live-logs", headers={"X-CSRF-Token": CSRF})
        self.assertEqual(resp.status_code, 403)

    def test_the_unscoped_session_can_read_the_log(self):
        self.sign_in(guild_id=None, scope="global")
        resp = self.client.get("/api/live-logs", headers={"X-CSRF-Token": CSRF})
        self.assertEqual(resp.status_code, 200)


class PreviewHarness(unittest.TestCase):
    """The documented way to eyeball the dashboard has to actually work.

    ``scripts/preview_dashboard.py`` serves the real template with stub data.
    Driving it in a browser is how the module switches were found to be
    unimplementable there: ``/api/toggle`` and ``/api/toggle-all`` had no route,
    so every switch answered with Flask's 404 page, the page showed its red
    "Request failed (404)" toast, and the switch sprang back. The dashboard is
    the thing being checked, and it *looked* broken while only the harness was —
    that is the failure this test exists to prevent.

    It compares the two sides by reading them, so a new endpoint on the page
    cannot quietly go missing from the harness.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _page_calls(self) -> set[str]:
        source = open(os.path.join(self.ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
        # The page builds some urls by concatenation (``/api/scheduled/`` + id),
        # so a trailing slash is kept and matched as a prefix below.
        return set(re.findall(r"['\"`](/api/[A-Za-z0-9_\-/]*)", source))

    def _harness_routes(self) -> set[str]:
        source = open(os.path.join(self.ROOT, "scripts", "preview_dashboard.py"), encoding="utf-8").read()
        routes = set(re.findall(r'@app\.route\("([^"]+)"', source))
        # A page the harness serves by *delegating to the real view* is served
        # just as much as one it declares with a decorator — and delegating is the
        # better answer, because a harness that restates a route answers
        # differently from the route.
        routes.update(re.findall(r'\("(/[^"]*)",\s*"[^"]+",\s*keep_alive\.', source))
        return routes

    @staticmethod
    def _served(path: str, routes: set[str]) -> bool:
        """Is ``path`` served? A ``/``-terminated call matches its own children.

        Matched at a path boundary, never by a bare ``startswith``: that would
        accept ``/api/toggle-REMOVED`` as an implementation of ``/api/toggle``,
        which is exactly the renaming this guard exists to catch. A route's own
        ``<placeholder>`` stands for **one** segment, so
        ``/api/identity/image/<kind>`` does serve the page's
        ``/api/identity/image/avatar``, while ``/api/identity/image/a/b`` is
        still uncovered.
        """
        if path in routes:
            return True
        # ``/api/scheduled/`` is the page's own prefix for ``<id>/cancel``.
        if path.endswith("/"):
            return any(route.startswith(path) for route in routes)
        wanted = path.strip("/").split("/")
        for route in routes:
            parts = route.strip("/").split("/")
            if len(parts) == len(wanted) and all(
                part.startswith("<") or part == called
                for part, called in zip(parts, wanted)
            ):
                return True
        return False

    def test_the_page_only_calls_endpoints_the_harness_serves(self):
        routes = self._harness_routes()
        missing = [
            path
            for path in sorted(self._page_calls())
            if not self._served(path, routes)
        ]
        self.assertEqual(
            missing, [],
            f"the preview harness serves no route for {missing} — the page's calls "
            "to them 404, which reads as a broken dashboard",
        )

    def test_the_harness_serves_the_dashboard_and_the_market_page(self):
        routes = self._harness_routes()
        self.assertIn("/dashboard", routes)
        self.assertIn("/market/<symbol>", routes)
        # The bare index production redirects to the first instrument, and the two
        # session routes the rendered header's own Sign out button posts to — all
        # three were Flask 404s in the preview, which is a wall where the real
        # deployment answers.
        for route in ("/market", "/login", "/logout"):
            self.assertIn(route, routes, f"the harness serves no {route}")

    def test_the_dashboard_links_to_the_market_pages_and_the_harness_serves_them(self):
        """The market pages had no way in from anywhere.

        They are served at ``/market/<symbol>`` and the only other route to them
        was a member's command in Discord, so an operator reading the dashboard
        had to already know the URL. The header links there now — and the link is
        only worth having if the page it points at exists in the preview too,
        which is the pairing this test is for.
        """
        page = open(
            os.path.join(self.ROOT, "templates", "dashboard.html"), encoding="utf-8"
        ).read()
        self.assertIn('href="/market"', page, "the dashboard links nowhere to the charts")

        client = self._load_harness().app.test_client()
        landing = client.get("/market")
        self.assertEqual(landing.status_code, 302)
        self.assertTrue(landing.headers["Location"].endswith("/market/BTC"))
        self.assertEqual(client.get("/market/BTC").status_code, 200)

    def test_the_unknown_symbol_page_says_what_production_says(self):
        """A misspelt symbol is a thing people do, and "404 nope" teaches nothing.

        Production writes the symbol into the refusal and offers BTC; the harness
        rendered a bare heading, so the preview flattered the real page.
        """
        client = self._load_harness().app.test_client()
        refused = client.get("/market/nope")
        self.assertEqual(refused.status_code, 404)
        body = refused.get_data(as_text=True)
        self.assertIn("nope is not on the market", body)
        self.assertIn("href='/market/BTC'", body)

    def _load_harness(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "preview_dashboard_for_test",
            os.path.join(self.ROOT, "scripts", "preview_dashboard.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_entry_point_the_usage_line_names_is_served(self):
        """Step one of the documented check is "open the printed port".

        That used to be Flask's 404, so the first thing an administrator saw
        while checking the dashboard was a broken dashboard. It is the real view
        now, which answers a monitor with the liveness line and a browser with the
        landing page — the landing page being what production serves to somebody
        who is not signed in, rather than the redirect this harness used to write
        for itself.
        """
        client = self._load_harness().app.test_client()

        browser = client.get("/", headers={"User-Agent": "Mozilla/5.0"})
        self.assertEqual(browser.status_code, 200)
        self.assertIn("Meow", browser.get_data(as_text=True))

        monitor = client.get("/", headers={"User-Agent": "curl/8.4.0"})
        self.assertEqual(monitor.status_code, 200)
        self.assertIn("alive", monitor.get_data(as_text=True))

    def test_the_server_switcher_lands_on_the_server_that_was_asked_for(self):
        """The page navigates with ``?guild_id=``; the preview used to ignore it.

        Picking "Second Server" came back showing the first one with the select
        snapped back to it — a control that visibly does nothing, in the tool
        whose whole job is showing what the page does.
        """
        client = self._load_harness().app.test_client()

        page = client.get("/dashboard?guild_id=777002").get_data(as_text=True)
        self.assertIn("<strong>Second Server</strong>", page)
        self.assertIn('<option value="777002" selected>', page)

        # An id that is not a server the bot is in falls back to the first one,
        # the way ``_resolve_guild_id`` does, rather than rendering no server.
        fallback = client.get("/dashboard?guild_id=nonsense").get_data(as_text=True)
        self.assertIn("<strong>Meow Server</strong>", fallback)
        self.assertIn('<option value="777001" selected>', fallback)

    def test_the_pickers_manage_button_actually_switches(self):
        """**Manage** posts and has to land in that server's dashboard.

        It was answered with the same picker page — the harness holds no session,
        so there was nothing to switch — which left a visible button that did
        nothing but reload, in the one tool whose job is showing what the page
        does. The real route redirects; so does this one now, and it moves the
        preview's own idea of the current server with it, so the picker offers
        **Open** where you are and **Manage** on the rest.
        """
        module = self._load_harness()
        client = module.app.test_client()
        self.addCleanup(
            lambda: module.CURRENT_GUILD.update({"id": module.GUILDS[0]["id"]})
        )

        picked = client.post("/servers", data={"guild_id": "777002"})
        self.assertEqual(picked.status_code, 302, picked.get_data(as_text=True))
        self.assertTrue(picked.headers["Location"].endswith("/dashboard?guild_id=777002"))

        page = client.get("/servers").get_data(as_text=True)
        current = page.split("777002", 1)[1].split("</li>", 1)[0]
        self.assertIn("Open", current, "the server just picked is the one that is open")
        other = page.split("777001", 1)[1].split("</li>", 1)[0]
        self.assertIn("Manage", other, "the server left behind offers Manage")

        # An id that is not one the bot is in is refused in production's own
        # words, and does not move the preview.
        refused = client.post("/servers", data={"guild_id": "999999"})
        self.assertEqual(refused.status_code, 403)
        self.assertIn("You do not manage that server.", refused.get_data(as_text=True))
        self.assertEqual(module.CURRENT_GUILD["id"], 777002)

    def test_the_market_stubs_are_the_shapes_the_live_endpoints_send(self):
        """The page reads these with ``.find()`` and an age; an object throws.

        The stubs answered ``{"quotes": {}}`` and ``{"candles": []}``, which the
        page's ``renderQuotes`` threw on — and its ``catch`` blames the network,
        so the preview drew "offline · could not reach the bot" over a page whose
        every request had answered 200.
        """
        client = self._load_harness().app.test_client()

        quotes = client.get("/api/market/quotes").get_json()
        for key in ("as_of", "age_seconds", "sources", "max_quote_age_seconds",
                    "coins_per_usd", "quotes"):
            self.assertIn(key, quotes, "the live payload carries this key")
        self.assertIsInstance(quotes["quotes"], list)
        btc = next(row for row in quotes["quotes"] if row["symbol"] == "BTC")
        self.assertTrue(btc["available"])
        self.assertIn("coins", btc)
        self.assertLessEqual(btc["age_seconds"], quotes["max_quote_age_seconds"],
                             "a preview quoting a paused board teaches the wrong state")

        series = client.get("/api/market/candles?symbol=BTC&period=week").get_json()
        for key in ("symbol", "period", "source", "observed", "candles"):
            self.assertIn(key, series)
        self.assertEqual(series["symbol"], "BTC")
        self.assertEqual(series["period"], "week")
        self.assertGreaterEqual(len(series["candles"]), 2, "the chart needs points")
        for key in ("ts", "open", "high", "low", "close"):
            self.assertIn(key, series["candles"][0])

    def test_the_preview_offers_every_content_source_the_route_reads(self):
        """The generator picker was degenerate: one option, so the one kind of
        scheduled message that writes itself could not be reached from here."""
        from utils import digest

        client = self._load_harness().app.test_client()
        page = client.get("/dashboard").get_data(as_text=True)

        self.assertIn("Message I write", page)
        self.assertTrue(digest.GENERATORS, "this test would pass vacuously")
        for entry in digest.GENERATORS.values():
            self.assertIn(entry["label"], page)

    def test_toggling_a_module_in_the_harness_is_reversible(self):
        """The switches have to be drivable, not merely answered.

        The cards describe the **real** module store, not the seed values at the
        top of the harness: a preview whose switches moved a number nobody else
        read showed the module and the command list disagreeing about the same
        system. So this test reads its baseline the way the page does — after a
        render has synced the cards — and uses a server id no other test
        touches, because a baseline of seed values is only ever right by
        accident once another test has legitimately changed the store.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "preview_dashboard_for_test",
            os.path.join(self.ROOT, "scripts", "preview_dashboard.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # A server this harness knows and no other test switches: an id outside
        # ``GUILDS`` falls back to the first one, so the baseline would be a
        # different server's state wearing this one's name.
        guild = 777002
        client = module.app.test_client()
        # Render first: the cards are decided by the store, so "where they were"
        # has to be read from a page that has already asked for them.
        client.get(f"/dashboard?guild_id={guild}")
        before = {m["id"]: m["enabled"] for m in module.MODULES}
        target = next(m["id"] for m in module.MODULES if m["enabled"])

        off = client.post("/api/toggle", json={"guild_id": guild, "module": target, "enabled": False})
        self.assertEqual(off.status_code, 200, off.get_data(as_text=True))
        self.assertTrue(off.get_json()["success"])
        self.assertFalse(next(m for m in module.MODULES if m["id"] == target)["enabled"])
        self.assertEqual(client.get("/__last-toggle").get_json(),
                         {"module": target, "enabled": False})

        back = client.post("/api/toggle", json={"guild_id": guild, "module": target, "enabled": True})
        self.assertEqual(back.status_code, 200)
        self.assertEqual({m["id"]: m["enabled"] for m in module.MODULES}, before)

    def test_no_preview_endpoint_accepts_a_message_the_bot_would_refuse(self):
        """A preview may not answer success for what production refuses.

        It did, on both composer endpoints: a 2,100-character body came back
        with a receipt reading "SENT 13:24" — the page had even marked its own
        counter ``over`` — where the bot answers *"That message is 2100
        characters; the limit is 2000."* and posts nothing. Driving the page is
        how a defect is *found*, so a page that lies about the outcome is worse
        than useless: it hides the defect and invents a working feature.

        The harness now calls the real builder, and this compares its answer with
        the bot's own word for word rather than trusting that they are wired to
        the same function — a shared function is shared until somebody copies it.
        """
        import time

        import keep_alive

        module = self._load_harness()
        client = module.app.test_client()
        channel = module.CHANNELS[0]["id"]

        refusals = {
            "no channel at all": {"content": "hello"},
            "nothing written": {"channel_ids": [channel], "content": "   "},
            "a body past Discord's limit": {
                "channel_ids": [channel],
                "content": "x" * (keep_alive.MESSAGE_MAX_CHARS + 100),
            },
            "a title with no embed": {
                "channel_ids": [channel], "content": "", "title": "Notice"
            },
            "more channels than one send may reach": {
                "channel_ids": ["1", "2", "3", "4", "5", "6"], "content": "hi"
            },
        }
        for label, body in refusals.items():
            _, _, expected = keep_alive._build_message(module._PreviewGuild(), body)
            self.assertTrue(expected, f"{label} is not a refusal the bot makes")
            for endpoint in ("/api/send-message", "/api/schedule-message"):
                payload = dict(body)
                if endpoint.endswith("schedule-message"):
                    # A time the timing rule accepts, so the *message* rule is
                    # the one under test on this endpoint too.
                    payload["run_at"] = int(time.time()) + 3600
                answer = client.post(endpoint, json=payload)
                self.assertEqual(answer.status_code, 400, f"{endpoint}: {label}")
                self.assertEqual(answer.get_json()["error"], expected, endpoint)

        # And the endpoints still accept what the bot accepts, so the check above
        # cannot pass by refusing everything.
        sent = client.post("/api/send-message",
                           json={"channel_ids": [channel], "content": "hello"})
        self.assertEqual(sent.status_code, 200, sent.get_data(as_text=True))
        self.assertTrue(sent.get_json()["success"])

        # A generated message is the one kind with no text to validate, and the
        # real route deliberately does *not* run the builder for it. A harness
        # that did would refuse the only scheduled message that writes itself —
        # the same defect from the other side: refusing what production accepts.
        from utils import digest

        generated = client.post("/api/schedule-message", json={
            "channel_ids": [channel],
            "generator": next(iter(digest.GENERATORS)),
            "run_at": int(time.time()) + 3600,
        })
        self.assertEqual(generated.status_code, 200, generated.get_data(as_text=True))

        unknown = client.post("/api/schedule-message", json={
            "channel_ids": [channel], "generator": "not_a_generator",
            "run_at": int(time.time()) + 3600,
        })
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("content source", unknown.get_json()["error"])

    def test_the_preview_enforces_the_queue_cap_the_bot_enforces(self):
        """The page shows "N of 25" from ``max_pending``, so 26 may not be one.

        The harness reported the cap to the page and then accepted past it — the
        queue read "32 of 25", holding messages the bot refuses to queue at all
        (``429``, *"cancel one before adding another"*). A preview that lets the
        page print an impossible number hides the one limit an administrator
        actually hits, at the moment the queue fills and they need to know why.
        """
        import time

        import keep_alive

        module = self._load_harness()
        client = module.app.test_client()
        channel = module.CHANNELS[0]["id"]
        at = int(time.time()) + 3600
        cap = keep_alive.scheduled.MAX_PENDING_PER_GUILD

        refusals = 0
        answer = None
        for i in range(cap * 2):
            answer = client.post("/api/schedule-message", json={
                "channel_ids": [channel], "content": f"filler {i}", "run_at": at,
            })
            if answer.status_code != 200:
                refusals += 1
                break
        self.assertEqual(answer.status_code, 429, answer.get_data(as_text=True))
        self.assertIn("cancel one before adding another", answer.get_json()["error"])
        self.assertEqual(
            client.get("/api/scheduled").get_json()["pending"], cap,
            "the queue filled past the cap the page reports",
        )

    def test_the_harness_refuses_what_the_real_endpoint_refuses(self):
        """Same status and same words, or the page's error path learns the wrong
        lesson: an unknown module is ``400 Unknown module`` in ``keep_alive``, not
        a 404 that would read to a user as "this route does not exist"."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "preview_dashboard_for_test",
            os.path.join(self.ROOT, "scripts", "preview_dashboard.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()

        unknown = client.post("/api/toggle", json={"guild_id": 777001, "module": "nope", "enabled": True})
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.get_json()["error"], "Unknown module")

        no_guild = client.post("/api/toggle", json={"module": "economy", "enabled": True})
        self.assertEqual(no_guild.status_code, 400)
        self.assertEqual(no_guild.get_json()["error"], "No guild available")

        no_guild_all = client.post("/api/toggle-all", json={"enabled": True})
        self.assertEqual(no_guild_all.status_code, 400)

        ok = client.post("/api/toggle-all", json={"guild_id": 777001, "enabled": False})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["guild_id"], 777001)
        self.assertTrue(all(m["enabled"] is False for m in module.MODULES))


if __name__ == "__main__":
    unittest.main()
