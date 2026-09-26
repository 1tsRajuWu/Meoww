"""A switch can read *on* while the feature behind it does nothing.

That is the one failure the rest of this dashboard cannot see: every reading on
it is true — the system is enabled, the ledger balances, the roster is right —
and the moderator log went with a channel somebody deleted last week. So the
tests here are in two halves, and the split is deliberate:

* ``utils/health`` decides, from plain facts, so every check is exercised
  without a gateway — including the ones that must *not* fire, because a
  checklist that complains about a healthy server is a checklist somebody stops
  reading;
* the page renders what ``keep_alive._server_health`` read, so the last step —
  the reading a server actually sees, including the sentence it says when a
  reading could not be taken at all — is driven rather than argued.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import keep_alive
from tests.support import TempDatabaseMixin
from tests.test_composer import CHANNEL_ID, GUILD_ID
from tests.test_dashboard import _DashboardFixture
from utils import command_publish, db, health, modlog

#: A settings value pointing at a channel that is not in this server — what a
#: deleted channel leaves behind in ``log_channel_id``.
GONE_CHANNEL = CHANNEL_ID + 99


def facts(**overrides) -> dict:
    """The four readings, all taken and all healthy, with named overrides.

    Built through ``health.presets()`` so a key added to the shape later is a key
    these tests still fill in — and so a check that starts reading something new
    fails here rather than silently finding nothing. Everything is healthy to
    begin with, which is what makes a test's *one* override the whole finding.
    """
    base = health.presets()
    base.update({
        "systems": {},
        "names": {"moderation": "Moderation & Logs", "structure": "Server Structure"},
        "permissions": {name: True for name in health.PERMISSION_NAMES},
        "log": {"configured": True, "exists": True, "channel": "#mod-log", "can_post": True},
        "default_role": {"configured": False},
        "publish": {"matches": True, "text": ""},
    })
    base.update(overrides)
    return base


def keys(rows) -> list[str]:
    return [row.key for row in rows]


class TheFindingsAreAboutSomethingCostingTheServer(unittest.TestCase):
    """The checks themselves, on facts alone."""

    # ── permissions ───────────────────────────────────────────────────────
    def test_a_permission_the_bot_lacks_is_reported_while_its_system_is_on(self):
        rows = health.findings(facts(
            systems={"moderation": True},
            permissions={"kick_members": False},
        ))
        self.assertEqual(keys(rows), ["perm:moderation:kick_members"])
        row = rows[0]
        self.assertEqual(row.severity, "broken")
        self.assertIn("kick a member", row.title)
        self.assertIn("Moderation & Logs", row.detail)
        self.assertIn("Kick Members", row.fix, "the remedy names the permission Discord names")

    def test_the_same_gap_in_a_switched_off_system_costs_nothing_yet(self):
        rows = health.findings(facts(
            systems={"moderation": False},
            permissions={"kick_members": False},
        ))
        self.assertEqual(rows, [], "a server that does not run a system is not broken")

    def test_a_permission_nobody_answered_about_is_not_a_finding(self):
        """A stand-in that never heard of a permission is not Discord saying no."""
        answered = {name: True for name in health.PERMISSION_NAMES if name != "kick_members"}
        rows = health.findings(facts(systems={"moderation": True}, permissions=answered))
        self.assertEqual(rows, [])

    def test_a_server_that_cannot_post_at_all_is_one_row_not_two(self):
        rows = health.findings(facts(
            permissions={"send_messages": False, "embed_links": False},
        ))
        self.assertEqual(keys(rows), ["posting"])
        self.assertIn("Send Messages", rows[0].detail)
        self.assertIn("Embed Links", rows[0].detail)

    # ── the moderator log ─────────────────────────────────────────────────
    def test_a_log_channel_that_was_never_set_is_a_warning_not_a_break(self):
        """The record is missing, the feature still works — worth an ask, not a shout."""
        rows = health.findings(facts(systems={"moderation": True}, log={"configured": False}))
        self.assertEqual(keys(rows), ["log:unset"])
        self.assertEqual(rows[0].severity, "warning")
        self.assertIn("/modlog", rows[0].fix)

    def test_a_log_channel_that_is_gone_is_broken(self):
        rows = health.findings(facts(
            systems={"moderation": True},
            log={"configured": True, "exists": False, "channel": "`4242`"},
        ))
        self.assertEqual(keys(rows), ["log:gone"])
        self.assertEqual(rows[0].severity, "broken")
        self.assertIn("4242", rows[0].detail)

    def test_a_log_channel_the_bot_cannot_write_to_is_broken(self):
        rows = health.findings(facts(
            systems={"moderation": True},
            log={"configured": True, "exists": True, "channel": "#mod-log", "can_post": False},
        ))
        self.assertEqual(keys(rows), ["log:blocked"])
        self.assertIn("#mod-log", rows[0].detail)

    def test_a_healthy_log_channel_says_nothing_and_an_unanswered_one_says_nothing_either(self):
        for can_post in (True, None):
            with self.subTest(can_post=can_post):
                rows = health.findings(facts(
                    systems={"moderation": True},
                    log={"configured": True, "exists": True, "channel": "#mod-log", "can_post": can_post},
                ))
                self.assertEqual(rows, [])

    def test_the_log_checks_stay_quiet_while_moderation_is_off(self):
        for log in ({"configured": False}, {"configured": True, "exists": False}):
            with self.subTest(log=log):
                self.assertEqual(health.findings(facts(systems={"moderation": False}, log=log)), [])

    # ── the default role ──────────────────────────────────────────────────
    def test_a_default_role_above_the_bot_is_broken(self):
        """The one failure that reports success: Discord refuses, the cog says it worked."""
        rows = health.findings(facts(
            default_role={"configured": True, "found": True, "name": "Members", "above": False},
        ))
        self.assertEqual(keys(rows), ["default_role:hierarchy"])
        self.assertIn("above", rows[0].title)

    def test_a_default_role_that_no_longer_exists_is_broken(self):
        rows = health.findings(facts(
            default_role={"configured": True, "found": False, "name": "`4242`"},
        ))
        self.assertEqual(keys(rows), ["default_role:gone"])

    def test_a_role_that_is_assignable_and_one_that_was_never_set_say_nothing(self):
        for role in (
            {"configured": False},
            {"configured": True, "found": True, "name": "Members", "above": True},
            {"configured": True, "found": True, "name": "Members"},  # hierarchy unread
        ):
            with self.subTest(role=role):
                self.assertEqual(health.findings(facts(default_role=role)), [])

    # ── the command list Discord holds ────────────────────────────────────
    def test_a_command_list_that_disagrees_is_a_warning(self):
        rows = health.findings(facts(
            publish={"matches": False, "text": "Discord holds 90 — 2 more than this server."},
        ))
        self.assertEqual(keys(rows), ["commands:stale"])
        self.assertEqual(rows[0].severity, "warning")
        self.assertIn("2 more", rows[0].detail)

    def test_a_command_list_nobody_read_says_nothing(self):
        self.assertEqual(health.findings(facts(publish={"matches": None})), [])

    # ── the shape of the answer ───────────────────────────────────────────
    def test_the_worst_thing_is_the_first_row_and_the_rest_read_in_their_own_order(self):
        rows = health.findings(facts(
            systems={"moderation": True},
            permissions={"kick_members": False},
            log={"configured": False},
            publish={"matches": False, "text": "stale"},
        ))
        self.assertEqual(
            keys(rows),
            ["perm:moderation:kick_members", "log:unset", "commands:stale"],
            "the broken row leads, and the warnings keep the order the checks are written in",
        )

    def test_a_reading_that_was_not_taken_is_named_rather_than_counted_as_healthy(self):
        """The whole point: silence from a check nobody could run is not a clean answer."""
        partial = facts(permissions=None, log=None)
        self.assertEqual(
            health.read(partial),
            ["the default role", "Discord's copy of this server's commands"],
        )
        self.assertEqual(
            health.unread(partial),
            ["the bot's own permissions here", "the moderator log"],
        )
        self.assertEqual(health.findings(partial), [], "and no rows either — it has nothing to say")

    def test_the_counts_are_by_severity(self):
        rows = health.findings(facts(
            systems={"moderation": True},
            permissions={"kick_members": False},
            log={"configured": False},
            publish={"matches": False, "text": "stale"},
        ))
        self.assertEqual(health.summary(rows), {"broken": 1, "warning": 2})
        self.assertEqual(health.summary([]), {"broken": 0, "warning": 0})


class _Guild:
    """A guild that answers the three lookups ``_server_health`` makes."""

    def __init__(self, guild_id):
        self.id = guild_id
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(**{name: True for name in health.PERMISSION_NAMES})
        )
        self._channels = {CHANNEL_ID: SimpleNamespace(name="mod-log")}
        self._roles = {}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_role(self, role_id):
        return self._roles.get(role_id)


class TheServerHealthReadingIsTakenFromThisServersOwnFacts(TempDatabaseMixin, unittest.TestCase):
    """``_server_health``: what it reads, and what it refuses to claim."""

    def setUp(self):
        self.use_temp_database("health.db")
        asyncio.run(db.setup())
        self.guild = _Guild(GUILD_ID)
        self.bot = SimpleNamespace(get_guild=lambda gid: self.guild if gid == GUILD_ID else None)
        self.patch = mock.patch.object(keep_alive, "BOT_INSTANCE", self.bot, create=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_a_server_the_bot_is_not_in_cannot_be_read_at_all(self):
        reading = keep_alive._server_health(GUILD_ID + 5, {})
        self.assertFalse(reading["read"])
        self.assertIn("not connected", reading["why"])
        self.assertEqual(reading["findings"], [])

    def test_no_server_at_all_says_that_rather_than_answering(self):
        reading = keep_alive._server_health(0, {})
        self.assertFalse(reading["read"])
        self.assertIn("No server is in hand", reading["why"])

    def test_a_moderator_log_that_was_deleted_is_read_out_of_the_stored_setting(self):
        db.write_setting_sync(GUILD_ID, modlog.SETTING, str(GONE_CHANNEL))
        reading = keep_alive._server_health(GUILD_ID, {"moderation": True})
        self.assertEqual([row["key"] for row in reading["findings"]], ["log:gone"])
        self.assertIn("the moderator log", reading["checked"], "and it was read, not guessed")

    def test_a_log_channel_that_exists_is_read_and_clean(self):
        db.write_setting_sync(GUILD_ID, modlog.SETTING, str(CHANNEL_ID))
        reading = keep_alive._server_health(
            GUILD_ID, {"moderation": True}, {"matches": True, "text": ""}
        )
        self.assertEqual(reading["findings"], [])
        self.assertEqual(reading["unread"], [], "every reading was taken")
        self.assertEqual(len(reading["checked"]), len(health.READINGS))

    def test_a_guild_that_cannot_resolve_channels_leaves_that_reading_unread(self):
        """The preview harness's gateway-less server: honesty, not a clean answer."""
        self.guild.get_channel = None  # a stand-in with no channel lookup at all
        db.write_setting_sync(GUILD_ID, modlog.SETTING, str(CHANNEL_ID))
        reading = keep_alive._server_health(GUILD_ID, {"moderation": True})
        self.assertIn("the moderator log", reading["unread"])
        self.assertEqual(reading["findings"], [])

    def test_a_permission_nobody_could_answer_is_left_unread(self):
        """A guild with no bot member in it is a reading that was not taken."""
        self.guild.me = None
        reading = keep_alive._server_health(GUILD_ID, {"moderation": False})
        self.assertTrue(reading["read"])
        self.assertIn("the bot's own permissions here", reading["unread"])
        self.assertEqual(reading["findings"], [], "and it does not invent a permission gap")


class TheOverviewSaysWhatNeedsALook(_DashboardFixture):
    """The card, on the page a server's own administrator opens.

    The publish reading is process-wide and the suite is one process, so it is
    forgotten here rather than inherited: a record another test made for this same
    server id is a disagreement about a command list nobody in this test ever
    published, and the card correctly reports it — which is how this test failed
    in the full suite and passed alone.
    """

    def setUp(self):
        super().setUp()
        command_publish.forget()
        self.addCleanup(command_publish.forget)

    def enable_permissions(self, **overrides):
        """Give the stand-in bot member Discord's own answer, with named gaps."""
        self.guild.me.guild_permissions = SimpleNamespace(
            **{name: overrides.get(name, True) for name in health.PERMISSION_NAMES}
        )

    def page(self):
        self.sign_in()
        return self.client.get("/dashboard").data.decode()

    def test_a_log_channel_that_is_gone_is_named_on_the_page(self):
        self.enable_permissions()
        db.write_setting_sync(GUILD_ID, modlog.SETTING, str(GONE_CHANNEL))
        body = self.page()
        self.assertIn("Needs a look", body)
        self.assertIn("points at a channel that is gone", body)
        self.assertIn("What to do:", body)
        self.assertIn("notice--bad", body, "a case that is not being recorded is not a style note")

    def test_a_permission_gap_is_named_with_the_system_it_costs(self):
        self.enable_permissions(kick_members=False)
        body = self.page()
        self.assertIn("The bot cannot kick a member", body)
        self.assertIn("Kick Members", body, "the fix names the permission Discord names")

    def test_a_healthy_server_is_told_nothing_needs_a_look_and_what_was_read(self):
        self.enable_permissions()
        db.write_setting_sync(GUILD_ID, modlog.SETTING, str(CHANNEL_ID))
        body = self.page()
        self.assertIn("Nothing needs a look", body)
        self.assertIn("Read:", body)
        self.assertNotIn("Could not be read", body)

    def test_a_reading_nobody_could_take_is_not_drawn_as_a_clean_answer(self):
        body = self.page()
        # The fixture's stand-in gateway has a bot member with no permissions on
        # it at all, which is exactly the case this card has to be honest about.
        self.assertIn("No answer for:", body)
        self.assertIn("permissions here", body)
        self.assertIn(
            "clean bill of health for them",
            body,
            "a reading nobody took must not read as a clean one",
        )
