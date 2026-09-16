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


class DashboardCase(TempDatabaseMixin, unittest.TestCase):
    """A signed-in session against a fake gateway, with a throwaway database."""

    def setUp(self):
        self.use_temp_database("dashboard.db")
        asyncio.run(self._setup_db())

        self.keep_alive = keep_alive
        self.bot, self.guild, self.other = make_bot()
        keep_alive._login_attempts.clear()
        keep_alive._send_attempts.clear()
        self._patched = mock.patch.object(keep_alive, "BOT_INSTANCE", self.bot)
        self._patched.start()
        self.client = keep_alive.app.test_client()

    async def _setup_db(self):
        from utils import db, modules

        await db.setup()
        modules.invalidate()

    def tearDown(self):
        from utils import modules

        self._patched.stop()
        modules.invalidate()
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
        self.assertIn(b"41 ms", self.client.get("/dashboard").data)

    def test_an_unknown_latency_reads_as_unknown_not_as_zero(self):
        self.sign_in()
        self.bot.latency = float("nan")
        body = self.client.get("/dashboard").data
        self.assertNotIn(b"0 ms", body, "a missing heartbeat is not a 0 ms ping")
        card = body.split(b"Discord Ping")[1][:80]
        self.assertIn("—".encode("utf-8"), card, "it should say the ping is unknown")

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

        toggled = self.api("POST", "/api/toggle", {"guild_id": GUILD_ID, "module": "fun", "enabled": False})
        self.assertEqual(toggled.status_code, 400)

        channels = self.api("GET", f"/api/channels?guild_id={GUILD_ID}")
        self.assertEqual(channels.status_code, 400)

        sent = self.api(
            "POST",
            "/api/send-message",
            {"guild_id": GUILD_ID, "channel_ids": [CHANNEL_ID], "content": "hi"},
        )
        self.assertEqual(sent.status_code, 400)
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
        self.assertEqual(toggled.get_json()["guild_id"], GUILD_ID)

    def test_a_scoped_session_with_no_server_at_all_is_refused(self):
        self.sign_in(guild_id=None)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertEqual(
            self.api("POST", "/api/toggle", {"guild_id": GUILD_ID, "module": "fun", "enabled": True}).status_code,
            400,
        )

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
        for key in ("FLASK_SECRET_KEY", "ADMIN_PASSKEY"):
            self.assertIn(f"- key: {key}", self.manifest, f"{key} is not declared in render.yaml")

    def test_declared_secrets_are_prompts_not_committed_values(self):
        entries = re.findall(r"- key: (\S+)\n\s+sync: (\S+)", self.manifest)
        self.assertTrue(entries, "render.yaml should declare env vars")
        for key, sync in entries:
            self.assertEqual(sync, "false", f"{key} should be operator-supplied")
        self.assertNotIn(
            "value:", self.manifest,
            "a literal value in render.yaml would commit a credential",
        )


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


if __name__ == "__main__":
    unittest.main()
