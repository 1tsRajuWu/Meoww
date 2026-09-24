"""God level: the four actions that are not about one server.

Everything else in this project is scoped to a thing — a server, a wallet, a
command list — and a mistake there is a mistake about one server. The tier this
file tests is the exception: a pause stops the bot answering *anywhere*, and the
two fleet switches write every server's system state at once.

Three properties make the tier worth having, and each is pinned below because
each one can be lost without anything looking broken:

1. **It is off until the passkey is proved again.** Being signed in as the
   operator is not the claim; having just typed the credential is.
2. **It expires.** An unlock that stays open is a console that is permanently
   armed, which is the state this tier exists to avoid.
3. **Every action is audited with the reason it was given.** A fleet-wide write
   with no record of who asked for it is indistinguishable from a bug.

Plus the one thing the pause itself has to get right: it is a *cached* read on the
hot path of every command, and a database that cannot be read must not be able to
pause every server in the fleet.
"""
import asyncio
import sqlite3
import time
import unittest
from unittest import mock

import keep_alive
from tests.test_composer import GUILD_ID
from tests.test_dashboard import CSRF, _DashboardFixture
from utils import db, modules, pause

PASSKEY = "the-owner-passkey"


def rows(query: str, args=()) -> list[tuple]:
    """Read the database directly: these tests are about what was written."""
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [tuple(row) for row in conn.execute(query, args).fetchall()]
    finally:
        conn.close()


class _GodFixture(_DashboardFixture):
    """An operator session, the passkey set, and an empty pause flag."""

    def setUp(self):
        super().setUp()
        db.write_setting_sync(pause.NO_GUILD, pause.KEY, None)
        pause.invalidate()
        self._passkey = mock.patch.object(keep_alive, "ADMIN_PASSKEY", PASSKEY)
        self._passkey.start()
        keep_alive._passkey_attempts.clear()
        # Every session in these tests starts as the operator, because the tier
        # is *inside* the console rather than beside it: a non-operator is
        # refused by the gate this file does not re-test (test_console does).
        self.sign_in(scope="global")

    def tearDown(self):
        self._passkey.stop()
        db.write_setting_sync(pause.NO_GUILD, pause.KEY, None)
        pause.invalidate()
        modules.invalidate()
        super().tearDown()

    def get(self, url):
        return self.client.get(url, headers={"X-CSRF-Token": CSRF})

    def post(self, url, body=None):
        return self.client.post(url, json=body or {}, headers={"X-CSRF-Token": CSRF})

    def unlock(self):
        response = self.post("/api/console/god/unlock", {"passkey": PASSKEY})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response

    def act(self, action, reason=""):
        return self.post("/api/console/god/act", {"action": action, "reason": reason})


class TheTierIsOffUntilItIsProved(_GodFixture):

    def test_the_page_can_be_read_while_it_is_locked(self):
        """A locked tier still has to draw itself, or nobody can open it."""
        payload = self.get("/api/console/god").get_json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["locked"])
        self.assertEqual(payload["remaining_seconds"], 0)
        self.assertEqual(
            [action["id"] for action in payload["actions"]],
            list(keep_alive.GOD_ACTION_IDS),
        )
        self.assertFalse(payload["paused"]["paused"])

    def test_an_action_is_refused_while_it_is_locked(self):
        response = self.act("pause", "should not happen")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.get_json()["locked"])
        self.assertFalse(pause.is_paused(), "a locked tier paused the bot")
        self.assertEqual(rows("SELECT COUNT(*) FROM web_actions WHERE scope = 'god'")[0][0], 0)

    def test_the_wrong_passkey_does_not_open_it(self):
        response = self.post("/api/console/god/unlock", {"passkey": "not-the-passkey"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.get("/api/console/god").get_json()["locked"] is False)
        self.assertEqual(self.act("pause", "nope").status_code, 403)

    def test_a_refused_unlock_is_written_down(self):
        """A guess at the owner's passkey is the one thing worth recording."""
        self.post("/api/console/god/unlock", {"passkey": "not-the-passkey"})
        recorded = rows(
            "SELECT action, ok FROM web_actions WHERE scope = 'god' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(recorded, [("god_unlock", 0)])

    def test_guessing_is_rate_limited_by_the_shared_window(self):
        """A second place to type a passkey must not be a second bucket of tries."""
        for _ in range(keep_alive.PASSKEY_MAX_ATTEMPTS):
            self.post("/api/console/god/unlock", {"passkey": "wrong"})
        response = self.post("/api/console/god/unlock", {"passkey": PASSKEY})
        self.assertEqual(response.status_code, 429)
        self.assertFalse(self.get("/api/console/god").get_json()["locked"] is False)

    def test_a_server_scoped_sign_in_cannot_reach_it_at_all(self):
        self.sign_in(guild_id=GUILD_ID, scope="guild")
        response = self.get("/api/console/god")
        self.assertEqual(response.status_code, 403)
        self.assertIn("Operator", response.get_json()["error"])

    def test_the_right_passkey_opens_it(self):
        payload = self.unlock().get_json()
        self.assertTrue(payload["success"])
        self.assertGreater(payload["remaining_seconds"], 0)
        page = self.get("/api/console/god").get_json()
        self.assertFalse(page["locked"])
        self.assertEqual(page["recent"][0]["action"], "god_unlock")


class EveryActionIsAudited(_GodFixture):

    def test_a_pause_writes_the_flag_and_the_reason(self):
        self.unlock()
        response = self.act("pause", "a migration is running")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["paused"]["paused"])
        self.assertIn("a migration is running", response.get_json()["said"])

        # The flag the bot reads, and the record of who set it.
        self.assertTrue(pause.is_paused())
        state = pause.state(refresh=True)
        self.assertEqual(state["reason"], "a migration is running")
        self.assertTrue(state["by"])
        self.assertGreater(state["since"], 0)

        recorded = rows(
            "SELECT action, ok, detail FROM web_actions WHERE scope = 'god' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(recorded[0][0], "god_pause")
        self.assertEqual(recorded[0][1], 1)
        self.assertIn("a migration is running", recorded[0][2])

    def test_resume_clears_the_record_rather_than_leaving_it_behind(self):
        self.unlock()
        self.act("pause", "investigating")
        self.assertTrue(pause.is_paused())
        response = self.act("resume")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["paused"]["paused"])
        self.assertFalse(pause.is_paused())
        self.assertEqual(
            rows("SELECT COUNT(*) FROM guild_settings WHERE guild_id = 0 AND key LIKE 'bot_paused%'")[0][0],
            0,
            "a resume left the last reason behind for the next reader to misread",
        )

    def test_a_dangerous_action_refuses_to_run_without_a_reason(self):
        self.unlock()
        response = self.act("pause")
        self.assertEqual(response.status_code, 400)
        self.assertIn("reason", response.get_json()["error"])
        self.assertFalse(pause.is_paused())
        # …and the harmless one does not ask, because a form that demands a reason
        # for everything trains its reader to type anything.
        self.assertEqual(self.act("resume").status_code, 200)

    def test_the_state_and_the_trail_both_survive_a_restart(self):
        """The flags are rows, not memory: a pause is usually covering a deploy."""
        self.unlock()
        self.act("pause", "deploying")
        pause.invalidate()
        self.assertTrue(pause.is_paused())
        # A fresh process reads the same rows — the only thing that would fail is
        # a flag kept in a module global, which is exactly what this pins.
        self.assertEqual(pause.state(refresh=True)["reason"], "deploying")
        self.assertIn("deploying", pause.describe())


class TheUnlockExpires(_GodFixture):

    def test_an_old_unlock_is_locked_again(self):
        self.unlock()
        with self.client.session_transaction() as session:
            session["god_until"] = time.time() - 1
        self.assertTrue(self.get("/api/console/god").get_json()["locked"])
        response = self.act("pause", "too late")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(pause.is_paused())

    def test_locking_early_closes_it_and_is_recorded(self):
        self.unlock()
        response = self.post("/api/console/god/lock")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["locked"])
        self.assertEqual(self.act("pause", "closed").status_code, 403)
        recorded = rows(
            "SELECT action FROM web_actions WHERE scope = 'god' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(recorded[0][0], "god_lock")


class TheFleetSwitchesWriteEveryServer(_GodFixture):

    def test_turning_every_system_off_reaches_the_guilds_the_bot_is_in(self):
        self.unlock()
        response = self.act("fleet_off", "emergency")
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(payload["success"], payload)
        self.assertEqual(payload["servers"], 2, "the fixture's bot is in two servers")
        self.assertFalse(
            modules.is_enabled(GUILD_ID, "economy"),
            "a fleet switch left a system on",
        )
        # And the counter the console's own pages read agrees.
        self.assertEqual(keep_alive._module_summary(GUILD_ID)["on"], 0)

    def test_turning_them_back_on_is_the_same_write_the_other_way(self):
        self.unlock()
        self.act("fleet_off", "first")
        response = self.act("fleet_on", "all clear")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(modules.is_enabled(GUILD_ID, "economy"))
        self.assertEqual(
            keep_alive._module_summary(GUILD_ID)["on"],
            keep_alive._module_summary(GUILD_ID)["total"],
        )

    def test_the_fleet_switch_is_recorded_with_its_size(self):
        self.unlock()
        self.act("fleet_off", "because")
        recorded = rows(
            "SELECT action, detail FROM web_actions WHERE scope = 'god' ORDER BY id DESC LIMIT 1"
        )[0]
        self.assertEqual(recorded[0], "god_fleet_off")
        self.assertIn("2 servers", recorded[1])


class APausedBotSaysSoInsteadOfGoingQuiet(_GodFixture):

    def test_the_command_path_refuses_and_explains(self):
        """The one place every command passes through, driven directly.

        A paused bot that simply stopped answering would be indistinguishable
        from a crashed one — the member would try again, and again — so what is
        pinned here is the *sentence*, not just the boolean.
        """
        import bot as bot_module

        sent = []

        class _Response:
            def __init__(self):
                self.done = False

            def is_done(self):
                return self.done

            async def send_message(self, content, ephemeral=False):
                self.done = True
                sent.append((content, ephemeral))

        class _Interaction:
            def __init__(self):
                self.response = _Response()
                self.guild_id = GUILD_ID
                self.command = type("C", (), {"name": "daily", "root_parent": None})()

        pause.set_state(True, actor="raj", reason="testing")

        interaction = _Interaction()
        allowed = asyncio.run(bot_module.global_module_check(interaction))
        self.assertFalse(allowed, "a paused bot still answered a command")
        self.assertTrue(sent)
        self.assertIn("paused", sent[0][0].lower())
        self.assertTrue(sent[0][1], "the refusal must be ephemeral, not a channel post")

        pause.set_state(False, actor="raj")
        resumed = _Interaction()
        self.assertTrue(asyncio.run(bot_module.global_module_check(resumed)))
        self.assertEqual(sent.__len__(), 1, "the refusal was sent more than once")

    def test_a_database_that_cannot_be_read_does_not_pause_the_fleet(self):
        """The failure direction, which is the whole safety story of a kill switch.

        Failing closed here would mean one transient lock taking every server
        offline until somebody restarted the process — a worse outage than the one
        the switch exists to handle. The read says so in the log and answers
        "running".
        """
        with mock.patch.object(pause.db, "read_setting_sync", side_effect=sqlite3.OperationalError("locked")):
            pause.invalidate()
            self.assertFalse(pause.is_paused())
            self.assertFalse(pause.state(refresh=True)["read"])


if __name__ == "__main__":
    unittest.main()
