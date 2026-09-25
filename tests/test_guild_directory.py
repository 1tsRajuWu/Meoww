"""A server must be named even when Discord's cache has not answered yet.

Two complaints, one cause. The console headed every page ``Unknown server`` and
an operator signing in with Discord was offered no servers at all, and both were
the same line of code: ``_known_guilds()`` read ``BOT_INSTANCE.guilds`` and
nothing else, and that cache is **empty in a fresh process** until the handshake
finishes. On a host that stops the process when it is idle — which this one does
— that window is the window a visitor is most likely to arrive in, so "sometimes
wrong" was "wrong whenever anybody looked".

The fix is a directory of servers written down when the gateway *does* know them
and read when it does not. These are the guards for it, and the three that matter
most are the ones the failure mode would break:

1. **A cold gateway still names its server.** No fakes about it: the page is
   rendered with an empty ``bot.guilds``, which is what a restart looks like.
2. **``reconcile`` refuses an empty list.** ``bot.guilds`` is empty during
   exactly the window the directory exists to cover, so a reconcile that trusted
   it would delete every remembered server at the moment they are the only copy —
   turning the fix into a worse version of the bug.
3. **The badge reads the live answer.** Both consoles compared ``bot_ready``,
   which is "this process has logged in at least once": a latch, set on the first
   login and never cleared. So the badge read **"Live"** through a reconnect,
   which is the one case a status badge is looked at for.

The gateway stand-ins come from ``tests.test_composer``, the same fakes the
dashboard and composer suites drive.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import keep_alive
from tests.support import TempDatabaseMixin
from tests.test_composer import GUILD_ID, _Bot, _Guild
from utils import db, figures, guild_directory

CSRF = "csrf-token"
OTHER_ID = 999002


def page_href(page_id: str) -> str:
    """One dashboard page as the route that renders it, read from the table."""
    return next(
        path for candidate, _label, path, _anchor in keep_alive.DASHBOARD_PAGES
        if candidate == page_id
    )


def cold_bot(*guilds):
    """A gateway that has answered with *nothing* — what a restart looks like.

    Not ``guilds=[]`` on the shared helper: this has to be a bot that is up and
    has simply not been told about any server yet, because that is the state the
    directory is read in.
    """
    bot = _Bot(list(guilds))
    bot._has_started = True
    return bot


class _DirectoryFixture(TempDatabaseMixin, unittest.TestCase):
    """A throwaway database, a fake gateway, and a signed-in client. No tests."""

    def setUp(self):
        self.use_temp_database("directory.db")
        asyncio.run(db.setup())
        guild_directory.reset()
        figures.reset()
        keep_alive._passkey_attempts.clear()
        self.client = keep_alive.app.test_client()

    def tearDown(self):
        guild_directory.reset()
        figures.reset()
        self.release_temp_database()

    def sign_in(self, guild_id=GUILD_ID):
        """A one-time-link session: scoped to the one server it was minted in."""
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["csrf_token"] = CSRF
            session["auth_scope"] = "guild"
            session["guild_id"] = guild_id

    def gateway(self, bot):
        """Point the app at ``bot`` for the duration of one test."""
        patcher = mock.patch.object(keep_alive, "BOT_INSTANCE", bot)
        patcher.start()
        self.addCleanup(patcher.stop)


class TheColdStart(_DirectoryFixture):
    """What a page says before the gateway cache has filled."""

    def test_a_remembered_server_is_named_while_the_gateway_knows_nothing(self):
        # Written down by a previous process, which is the whole premise: the
        # gateway is cold *now*, and the name came from the last time it was not.
        guild_directory.remember(GUILD_ID, "Meow Server", 4)
        self.gateway(cold_bot())
        self.sign_in()

        body = self.client.get(page_href("overview")).get_data(as_text=True)

        self.assertIn("Meow Server", body)
        self.assertNotIn("Unknown server", body)

    def test_the_remembered_list_is_what_the_operator_may_pick_from(self):
        # The second half of the same defect, and the worse half: `_visible_guilds`
        # derives from `_known_guilds`, so an empty gateway cache left a Discord
        # sign-in with no servers to choose at all.
        guild_directory.remember(GUILD_ID, "Meow Server", 4)
        guild_directory.remember(OTHER_ID, "Other Server", 1)
        self.gateway(cold_bot())

        names = {g["name"] for g in keep_alive._known_guilds()}

        self.assertEqual(names, {"Meow Server", "Other Server"})

    def test_a_live_name_beats_the_remembered_one(self):
        # A server renamed in Discord must show the new name on the next page,
        # not the name it had whenever the directory last ran.
        guild_directory.remember(GUILD_ID, "Renamed Last Year", 4)
        self.gateway(cold_bot(_Guild(GUILD_ID, "Meow Server", [])))

        self.assertEqual(
            [g["name"] for g in keep_alive._known_guilds() if g["id"] == GUILD_ID],
            ["Meow Server"],
        )

    def test_a_server_nobody_has_written_down_still_says_so(self):
        # The honest fallback is kept. The fix is that it should be unreachable
        # for a server the bot is in, not that the words are forbidden.
        self.gateway(cold_bot())
        self.sign_in()

        body = self.client.get(page_href("overview")).get_data(as_text=True)

        self.assertIn("Unknown server", body)


class TheReconcile(_DirectoryFixture):
    """Boot-time truth: write down what is there, drop what is not."""

    def test_an_empty_guild_list_is_refused_rather_than_trusted(self):
        guild_directory.remember(GUILD_ID, "Meow Server", 4)

        written, forgotten = guild_directory.reconcile([])

        self.assertEqual((written, forgotten), (0, 0))
        self.assertEqual(guild_directory.count(), 1)

    def test_a_server_the_bot_has_left_is_dropped(self):
        guild_directory.remember(GUILD_ID, "Meow Server", 0)
        guild_directory.remember(OTHER_ID, "Gone Server", 1)

        written, forgotten = guild_directory.reconcile([_Guild(GUILD_ID, "Meow Server", [])])

        self.assertEqual(forgotten, 1)
        self.assertEqual([row["id"] for row in guild_directory.read_all()], [GUILD_ID])
        # `written` is 0 because the row was already correct — that idempotence is
        # what keeps a boot from rewriting every server on every restart.
        self.assertEqual(written, 0)

    def test_a_count_discord_did_not_report_does_not_erase_a_known_one(self):
        # Found by this suite rather than by reading the code: `reconcile` first
        # passed `guild.member_count or 0`, and `Guild.member_count` is None
        # without the members intent — so the boot reconcile would have replaced
        # every server's real count with a zero nobody measured, on every start.
        guild_directory.remember(GUILD_ID, "Meow Server", 4)
        silent = SimpleNamespace(id=GUILD_ID, name="Meow Server", member_count=None)

        guild_directory.reconcile([silent])

        self.assertEqual(guild_directory.read(GUILD_ID)["member_count"], 4)

    def test_a_server_that_really_has_no_members_stores_zero(self):
        # The other half: 0 is a legitimate count, so "unknown" cannot simply be
        # spelled as 0. Only ``None`` means Discord did not say.
        guild_directory.remember(GUILD_ID, "Meow Server", 4)

        guild_directory.reconcile([_Guild(GUILD_ID, "Meow Server", [])])

        self.assertEqual(guild_directory.read(GUILD_ID)["member_count"], 0)

    def test_forget_is_what_a_leave_event_calls(self):
        guild_directory.remember(GUILD_ID, "Meow Server", 4)

        self.assertTrue(guild_directory.forget(GUILD_ID))
        self.assertIsNone(guild_directory.read(GUILD_ID))
        # And forgetting twice is not an error: a leave and a boot-reconcile can
        # both decide the same row is gone.
        self.assertFalse(guild_directory.forget(GUILD_ID))

    def test_an_unchanged_row_is_not_rewritten(self):
        self.assertTrue(guild_directory.remember(GUILD_ID, "Meow Server", 4))
        # The console calls this on every render, so a rewrite on every page load
        # would turn reading a dashboard into a write path.
        self.assertFalse(guild_directory.remember(GUILD_ID, "Meow Server", 4))
        # A changed member count is news, and is written.
        self.assertTrue(guild_directory.remember(GUILD_ID, "Meow Server", 5))


class TheWiring(TempDatabaseMixin, unittest.TestCase):
    """The bot's own hooks call the directory.

    Source-level, and deliberately so: driving discord.py's event dispatch needs
    a real client and a loop, and what could actually go wrong here is *absence*
    — a hook that was never added — rather than a hook that misbehaves, which
    ``TheReconcile`` above covers directly.
    """

    def test_the_bot_writes_on_join_and_boot_and_forgets_on_leave(self):
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "bot.py"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("guild_directory.reconcile(bot.guilds)", source)
        self.assertIn("guild_directory.remember(guild.id, guild.name", source)
        self.assertIn("guild_directory.forget(guild.id)", source)


class TheBadge(_DirectoryFixture):
    """A status badge has to answer about *now*, in three states."""

    def _state(self, bot):
        self.gateway(bot)
        return keep_alive._badge_state()

    def test_a_connected_gateway_reads_live(self):
        bot = cold_bot(_Guild(GUILD_ID, "Meow Server", []))
        bot.is_ready = lambda: True

        self.assertEqual(self._state(bot)["label"], "Live")

    def test_a_gateway_that_is_reconnecting_says_so_rather_than_live(self):
        # The regression itself: `_has_started` is a latch, so this bot is
        # "ready" by the old test and is not connected by the live one.
        bot = cold_bot(_Guild(GUILD_ID, "Meow Server", []))
        bot.is_ready = lambda: False

        state = self._state(bot)

        self.assertEqual(state["label"], "Reconnecting")
        self.assertEqual(state["css"], "badge-warn")
        self.assertFalse(state["pulse"])

    def test_a_gateway_that_never_came_up_reads_offline(self):
        bot = cold_bot()
        bot._has_started = False
        bot.is_ready = lambda: False

        self.assertEqual(self._state(bot)["label"], "Bot offline")

    def test_the_rendered_badge_does_not_say_live_while_disconnected(self):
        # The same claim, through the real page: the state a person actually sees.
        bot = cold_bot(_Guild(GUILD_ID, "Meow Server", []))
        bot.is_ready = lambda: False
        self.gateway(bot)
        self.sign_in()

        body = self.client.get(page_href("overview")).get_data(as_text=True)

        self.assertIn("Reconnecting", body)
        self.assertNotIn("> Live<", body.replace("\n", ""))

    def test_each_console_draws_the_badge_from_one_rule(self):
        """One rule, server-owned — re-pointed at where the dashboard reads it.

        Two copies is how this drifted: the dashboard and the fleet console had
        the same rule spelled twice, and the second spelling was the stale one,
        so the dashboard stopped spelling anything and included
        ``templates/gateway_badge.html`` while the console took the value in its
        boot payload. The partial went with the rebuild — the dashboard draws the
        label from its own context now — so the claim is asserted one level up,
        where the value is *handed over* rather than where it is printed: the
        render call passes ``gateway=_badge_state()``, which is the one function
        that decides the three states, and the page prints ``gateway.label``.
        A template that spelled a state of its own would be the second author
        this law exists to refuse, and there is no partial left for it to hide in.
        """
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "keep_alive.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn(
            "gateway=_badge_state(),",
            source,
            "the dashboard's render no longer passes the one badge state, so the "
            "page would have to spell the three states itself",
        )

        with open(os.path.join(root, "templates", "dashboard.html"), encoding="utf-8") as handle:
            dashboard = handle.read()
        self.assertIn("gateway.label", dashboard, "the page stopped printing the state it was given")
        self.assertNotIn('class="status-badge badge-online"', dashboard)
        self.assertFalse(
            os.path.exists(os.path.join(root, "templates", "gateway_badge.html")),
            "the retired badge partial is back, so the state has two authors again",
        )

        with open(os.path.join(root, "templates", "console.html"), encoding="utf-8") as handle:
            console = handle.read()
        self.assertIn('id="console-boot"', console)
        self.assertIn('"gateway": gateway', console)
        self.assertNotIn('class="status-badge badge-online"', console)


if __name__ == "__main__":
    unittest.main()
