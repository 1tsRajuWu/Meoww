"""Whole-server actions: the plan, the ledger, and the undo that is the server's.

A lockdown is a bulk permission change over somebody else's server, so the tests
are mostly about the two halves that make it safe to offer from a web page: it
writes down what it changed **before** it changes anything, and unlocking puts
back what was there rather than allowing `@everyone` to talk again — which on a
server that had deliberately locked a channel would be handing out a permission
nobody asked for.
"""
import datetime
import os
import sys
import unittest
from unittest import mock

from tests.support import TempDatabaseMixin
from utils import db, server_control

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Perms:
    def __init__(self, *, manage_channels=True, administrator=False, **rest):
        self.manage_channels = manage_channels
        self.administrator = administrator
        for name, value in rest.items():
            setattr(self, name, value)

    def __getattr__(self, name):
        return False


class Bits:
    """A bitfield that answers the three questions a lockdown asks.

    It has to answer them *properly*: a ``Mock`` is truthy for every attribute,
    so a fake whose deny answered ``Mock`` made every channel look already locked
    and the lockdown a no-op — the sort of fake that turns a test into a
    statement about itself.
    """

    #: Discord's own bit for each permission a lockdown removes.
    NAMES = {
        "send_messages": 1 << 11,
        "create_public_threads": 1 << 34,
        "add_reactions": 1 << 6,
    }

    def __init__(self, value=0):
        self.value = int(value)

    def __getattr__(self, name):
        bit = self.NAMES.get(name)
        if bit is None:
            return False
        return bool(self.value & bit)


#: Everything a lockdown denies, as one mask — what "already locked" looks like.
ALL_LOCKED = Bits.NAMES["send_messages"] | Bits.NAMES["create_public_threads"] | Bits.NAMES["add_reactions"]


class Overwrite:
    """A permission overwrite, as the two bitfields the ledger stores."""

    def __init__(self, *, allow=0, deny=0):
        self.allow = Bits(allow)
        self.deny = Bits(deny)


class Role:
    def __init__(self, name="role", position=0):
        self.name = name
        self.position = position
        self.id = 1

    def __ge__(self, other):
        return self.position >= getattr(other, "position", 0)


class Channel:
    def __init__(self, channel_id, name="general", *, kind="text", manage=True, overwrites=None):
        self.id = channel_id
        self.name = name
        self.type = kind
        self.overwrites = dict(overwrites or {})
        self.guild = None
        self._manage = manage
        self.calls = []
        self.slowmode_delay = 0

    def permissions_for(self, _member):
        return Perms(manage_channels=self._manage)

    async def set_permissions(self, target, *, overwrite=None, reason=""):
        self.calls.append(("set_permissions", target, overwrite, reason))
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = overwrite

    async def edit(self, *, slowmode_delay=0, reason=""):
        self.calls.append(("edit", slowmode_delay, reason))
        self.slowmode_delay = slowmode_delay


class Guild:
    def __init__(self, guild_id=424242, *, channels=()):
        self.id = guild_id
        self.name = "Test Server"
        self.default_role = Role("@everyone")
        self.me = Role("Meow")
        self.channels = list(channels)
        self.gone = set()
        for channel in self.channels:
            channel.guild = self

    def get_channel(self, channel_id):
        if channel_id in self.gone:
            return None
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        return None


class Actor:
    id = 5
    label = "Admin (website)"


class ThePlan(unittest.TestCase):
    def test_it_locks_text_and_leaves_voice_alone(self):
        text = Channel(1, "general")
        voice = Channel(2, "lounge", kind="voice")
        guild = Guild(channels=[text, voice])
        plan = server_control.plan(guild)
        self.assertEqual([channel.id for channel in plan.change], [1])
        self.assertEqual([channel.id for channel in plan.skipped], [2])

    def test_a_channel_already_locked_is_reported_rather_than_locked_again(self):
        everyone = Role("@everyone")
        locked = Channel(1, "quiet", overwrites={everyone: Overwrite(deny=ALL_LOCKED)})
        guild = Guild(channels=[locked])
        guild.default_role = everyone
        plan = server_control.plan(guild)
        self.assertEqual(plan.change, [])
        self.assertEqual(len(plan.already_locked), 1)
        self.assertIn("already locked", plan.sentence())

    def test_a_channel_it_cannot_manage_is_named_with_the_permission_it_needs(self):
        guild = Guild(channels=[Channel(1, "other", manage=False)])
        plan = server_control.plan(guild)
        self.assertEqual(len(plan.blocked), 1)
        self.assertEqual(plan.blocked[0][1], "Manage Channels")
        self.assertIn("cannot manage", plan.sentence())

    def test_an_administrator_cannot_lock_nothing_and_be_told_it_worked(self):
        guild = Guild(channels=[])
        plan = server_control.plan(guild)
        self.assertFalse(plan.worth_changing)
        self.assertEqual(plan.counts["change"], 0)


class TheLockdown(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        self.order = []

    def tearDown(self):
        self.release_temp_database()

    def _guild(self, *channels):
        guild = Guild(channels=list(channels))
        # The ledger must be written before the first permission changes.
        real = server_control.db.save_lockdown

        async def watched(guild_id, rows, by, at):
            self.order.append("ledger")
            return await real(guild_id, rows, by, at)

        return guild, watched

    async def test_the_ledger_is_written_before_anything_is_changed(self):
        guild, watched = self._guild(Channel(1, "general"), Channel(2, "memes"))
        with mock.patch.object(server_control.db, "save_lockdown", new=watched):
            report = await server_control.lockdown(guild, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(report.changed, 2)
        self.assertEqual(self.order, ["ledger"], "the undo was recorded first")
        self.assertEqual(len(await db.lockdown_rows(guild.id)), 2)

    async def test_the_deny_removes_talking_threads_and_reactions(self):
        channel = Channel(1, "general")
        guild, watched = self._guild(channel)
        with mock.patch.object(server_control.db, "save_lockdown", new=watched):
            await server_control.lockdown(guild, Actor())
        _kind, _target, overwrite, reason = channel.calls[0]
        self.assertIs(overwrite.send_messages, False)
        self.assertIs(overwrite.create_public_threads, False)
        self.assertIs(overwrite.add_reactions, False)
        self.assertIn("lockdown", reason)

    async def test_a_channel_discord_refuses_does_not_stop_the_others(self):
        good, bad = Channel(1, "general"), Channel(2, "locked-out")
        guild, watched = self._guild(good, bad)

        async def refuse(target, *, overwrite=None, reason=""):
            raise RuntimeError("nope")

        with mock.patch.object(server_control.db, "save_lockdown", new=watched), \
                mock.patch.object(bad, "set_permissions", new=refuse):
            report = await server_control.lockdown(guild, Actor())
        self.assertTrue(report.ok, "one refusal is not a failed lockdown")
        self.assertEqual(report.changed, 1)
        self.assertEqual(len(report.failures), 1)
        self.assertIn("locked-out", report.failures[0][0])

    async def test_a_ledger_that_cannot_be_written_means_nothing_happens(self):
        channel = Channel(1, "general")
        guild = Guild(channels=[channel])
        with mock.patch.object(server_control.db, "save_lockdown", side_effect=RuntimeError("disk full")):
            report = await server_control.lockdown(guild, Actor())
        self.assertFalse(report.ok)
        self.assertEqual(channel.calls, [], "a change with no undo is not worth making")
        self.assertIn("undo", report.detail)

    async def test_an_already_locked_server_is_a_success_with_nothing_to_do(self):
        everyone = Role("@everyone")
        guild = Guild(channels=[Channel(1, "quiet", overwrites={everyone: Overwrite(deny=ALL_LOCKED)})])
        guild.default_role = everyone
        report = await server_control.lockdown(guild, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(report.changed, 0)
        self.assertEqual(await db.lockdown_rows(guild.id), [])


class TheUndo(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()

    def tearDown(self):
        self.release_temp_database()

    async def test_it_puts_back_the_overwrite_that_was_there(self):
        """The whole reason the ledger exists: an undo that blanket-allows is a
        change nobody asked for."""
        everyone = Role("@everyone")
        channel = Channel(1, "quiet", overwrites={everyone: Overwrite(allow=0, deny=1 << 11)})
        guild = Guild(channels=[channel])
        guild.default_role = everyone
        await server_control.lockdown(guild, Actor())
        channel.calls.clear()
        report = await server_control.unlock(guild, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(report.restored, 1)
        _kind, _target, overwrite, _reason = channel.calls[0]
        self.assertIsNotNone(overwrite, "the overwrite that was there is restored")
        _allow, deny = overwrite.pair()
        self.assertEqual(deny.value, 1 << 11, "the deny the server already had")
        self.assertEqual(await db.lockdown_rows(guild.id), [], "the ledger is tidied")

    async def test_an_overwrite_that_did_not_exist_is_removed_not_allowed(self):
        everyone = Role("@everyone")
        channel = Channel(1, "general")
        guild = Guild(channels=[channel])
        guild.default_role = everyone
        await server_control.lockdown(guild, Actor())
        self.assertIn(everyone, channel.overwrites)
        report = await server_control.unlock(guild, Actor())
        self.assertTrue(report.ok)
        self.assertNotIn(everyone, channel.overwrites, "there was nothing here before the lockdown")
        self.assertEqual(channel.calls[-1][2], None)

    async def test_a_channel_deleted_during_a_lockdown_does_not_hold_the_undo_open(self):
        guild = Guild(channels=[Channel(1, "general")])
        await server_control.lockdown(guild, Actor())
        guild.gone.add(1)
        report = await server_control.unlock(guild, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(await db.lockdown_rows(guild.id), [])

    async def test_a_server_that_was_never_locked_says_so_rather_than_reverting_things(self):
        guild = Guild(channels=[Channel(1, "general")])
        report = await server_control.unlock(guild, Actor())
        self.assertTrue(report.ok)
        self.assertIn("No lockdown", report.detail)
        self.assertEqual(guild.channels[0].calls, [])

    async def test_only_the_channels_it_restored_leave_the_ledger(self):
        good, bad = Channel(1, "general"), Channel(2, "locked-out")
        guild = Guild(channels=[good, bad])
        await server_control.lockdown(guild, Actor())

        async def refuse(target, *, overwrite=None, reason=""):
            raise RuntimeError("nope")

        with mock.patch.object(bad, "set_permissions", new=refuse):
            report = await server_control.unlock(guild, Actor())
        self.assertEqual(report.restored, 1)
        self.assertEqual(report.blocked, 1)
        left = [channel_id for channel_id, _p, _a in await db.lockdown_rows(guild.id)]
        self.assertEqual(left, [2], "the one that failed is still owed an undo")


class TheSlowmode(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()

    def tearDown(self):
        self.release_temp_database()

    async def test_it_sets_the_value_on_every_text_channel_and_can_turn_it_off(self):
        channels = [Channel(1, "a"), Channel(2, "b"), Channel(3, "voice", kind="voice")]
        guild = Guild(channels=channels)
        report = await server_control.set_slowmode(guild, 30, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(report.changed, 2)
        self.assertEqual([channel.slowmode_delay for channel in channels], [30, 30, 0])
        await server_control.set_slowmode(guild, 0, Actor())
        self.assertEqual(channels[0].slowmode_delay, 0, "0 is how slowmode is turned off")

    async def test_a_value_over_discords_ceiling_is_clamped_not_refused(self):
        channel = Channel(1, "a")
        guild = Guild(channels=[channel])
        report = await server_control.set_slowmode(guild, 999999, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(channel.slowmode_delay, 21600)

    async def test_channels_it_cannot_manage_are_counted_rather_than_silently_skipped(self):
        guild = Guild(channels=[Channel(1, "a"), Channel(2, "b", manage=False)])
        report = await server_control.set_slowmode(guild, 5, Actor())
        self.assertEqual(report.changed, 1)
        self.assertEqual(report.blocked, 1)

    async def test_the_state_reading_reports_what_is_locked_and_what_would_change(self):
        guild = Guild(channels=[Channel(1, "general"), Channel(2, "memes")])
        self.assertEqual((await server_control.state(guild))["locked"], 0)
        await server_control.lockdown(guild, Actor())
        reading = await server_control.state(guild)
        self.assertEqual(reading["locked"], 2)
        self.assertEqual([channel["name"] for channel in reading["channels"]], ["general", "memes"])
        self.assertEqual(reading["counts"]["already_locked"], 0)


class TheReport(unittest.TestCase):
    """A report that cannot say what happened is a log line nobody can act on."""

    def test_a_failed_report_says_why_rather_than_counting_zero(self):
        from utils.server_control import Report

        report = Report(action="lockdown", ok=False, detail="I need Manage Channels there.")
        self.assertEqual(report.sentence(), "I need Manage Channels there.")

    def test_a_successful_report_counts_what_it_did_and_what_it_could_not(self):
        from utils.server_control import Report

        report = Report(action="lockdown", ok=True, changed=12, already=2, blocked=3)
        said = report.sentence()
        self.assertIn("12", said)
        self.assertIn("2 were already locked", said)
        self.assertIn("3 could not be managed", said)
        self.assertFalse(report.to_dict()["failures"])


if __name__ == "__main__":
    unittest.main()
