"""A bug report is a message, not a ticket, and it always lands somewhere.

The path this pins was written twice and failed differently both times. First
``#bug`` had no producer at all. Then the entry point only worked in a server
that had happened to create a channel named ``#bug`` — everywhere else it refused
with "this server has no bug-report channel", which reads to the person filing it
as the bot losing their report. And the surface people actually reached for was
the ticket panel's own *Bug Report & Glitch* category, which opened a private
ticket channel: a support conversation for one member, in place of a report the
operators can see.

So the rules are: a report is a card in a channel, the server it was filed in is
asked first, the bot's own server is the fallback so nothing is swallowed, and no
part of this ever creates a ticket channel.
"""
import os
import unittest
from pathlib import Path
from unittest import mock

import discord
from discord.ext import commands

from cogs import help as help_cog
from tests.support import TempDatabaseMixin
from utils import bot_channels, db, features, permissions

ROOT = Path(__file__).resolve().parent.parent

REPORTING_GUILD = 900001
REPORTING_CHANNEL = 910001
BOT_SERVER = 900002
BOT_SERVER_CHANNEL = 910002
SECOND_CHANNEL = 910003


class _Message:
    def __init__(self, channel):
        self.channel = channel
        self.id = 920001
        self.thread = None

    async def create_thread(self, **kwargs):
        self.thread = kwargs.get("name")
        return type("_Thread", (), {"mention": "<#930001>"})()


class _Channel:
    def __init__(self, cid, guild, *, forbidden=False):
        self.id = cid
        self.guild = guild
        self.name = "bug"
        self.mention = f"<#{cid}>"
        self.posted = []
        self.forbidden = forbidden

    async def send(self, **kwargs):
        if self.forbidden:
            raise discord.Forbidden(type("R", (), {"status": 403, "reason": "no"})(), "forbidden")
        message = _Message(self)
        self.posted.append(kwargs.get("embed"))
        return message


class _Guild:
    """A guild that records channel creation, so a ticket would be visible here."""

    def __init__(self, gid, name, channel_ids=()):
        self.id = gid
        self.name = name
        self.channels = {}
        self.created = []
        for cid in channel_ids:
            self.channels[cid] = _Channel(cid, self)
        # What a name-based lookup walks — the same attribute the binder reads.
        self.text_channels = list(self.channels.values())

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def create_text_channel(self, **kwargs):  # a ticket, in the old design
        self.created.append(kwargs.get("name"))
        return None

    async def create_category(self, *args, **kwargs):
        self.created.append("category")
        return None


class _Response:
    def __init__(self):
        self.messages = []
        self.modal = None

    async def send_message(self, content="", **kwargs):
        self.messages.append(content)

    async def send_modal(self, modal):
        self.modal = modal


class _Client:
    def __init__(self, *guilds):
        self.guilds = list(guilds)
        self.user = type("_User", (), {"id": 1, "mention": "<@1>", "name": "Meow"})()

    def get_channel(self, cid):
        for guild in self.guilds:
            channel = guild.get_channel(cid)
            if channel is not None:
                return channel
        return None

    def get_guild(self, gid):
        return next((g for g in self.guilds if g.id == gid), None)


class _User:
    def __init__(self, uid=555):
        self.id = uid
        self.name = "raj"
        self.mention = f"<@{uid}>"


class _Interaction:
    def __init__(self, guild, client, user=None):
        self.guild = guild
        self.guild_id = getattr(guild, "id", None)
        self.client = client
        self.user = user or _User()
        self.response = _Response()


@mock.patch.dict(os.environ, {"BOT_SERVER_GUILD_ID": str(BOT_SERVER)})
class BugReportTargets(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """Which channel a report goes to, resolved through the real settings store."""

    async def asyncSetUp(self):
        self.use_temp_database("bugreport.db")
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def test_the_server_it_was_filed_in_is_asked_first(self):
        await db.set_setting(REPORTING_GUILD, "bug_channel_id", REPORTING_CHANNEL)
        await db.set_setting(BOT_SERVER, "bug_channel_id", BOT_SERVER_CHANNEL)
        self.assertEqual(
            await bot_channels.report_targets(REPORTING_GUILD, "bug"),
            [REPORTING_CHANNEL, BOT_SERVER_CHANNEL],
        )

    async def test_a_server_with_no_bug_channel_falls_back_to_the_bots_own(self):
        await db.set_setting(BOT_SERVER, "bug_channel_id", BOT_SERVER_CHANNEL)
        self.assertEqual(
            await bot_channels.report_targets(REPORTING_GUILD, "bug"),
            [BOT_SERVER_CHANNEL],
        )

    async def test_a_report_never_has_the_same_destination_twice(self):
        await db.set_setting(BOT_SERVER, "bug_channel_id", BOT_SERVER_CHANNEL)
        self.assertEqual(
            await bot_channels.report_targets(BOT_SERVER, "bug"), [BOT_SERVER_CHANNEL]
        )

    async def test_neither_configured_is_an_empty_answer_not_a_guess(self):
        self.assertEqual(await bot_channels.report_targets(REPORTING_GUILD, "bug"), [])

    async def test_the_bots_own_server_is_looked_at_by_name_when_never_bound(self):
        """A report must not be refused because no boot has bound anything yet."""
        guild = _Guild(BOT_SERVER, "Development", [BOT_SERVER_CHANNEL])
        client = _Client(guild)
        self.assertEqual(
            await bot_channels.report_targets(REPORTING_GUILD, "bug", client=client),
            [BOT_SERVER_CHANNEL],
        )

    async def test_a_bound_channel_still_wins_over_the_name_in_that_server(self):
        """Discovery is a last resort; a renamed channel must keep what it bound."""
        await db.set_setting(BOT_SERVER, "bug_channel_id", SECOND_CHANNEL)
        guild = _Guild(BOT_SERVER, "Development", [BOT_SERVER_CHANNEL, SECOND_CHANNEL])
        client = _Client(guild)
        self.assertEqual(
            await bot_channels.report_targets(REPORTING_GUILD, "bug", client=client),
            [SECOND_CHANNEL],
        )

    async def test_the_reporting_server_is_never_discovered_this_way(self):
        """Only the last target: a server that renamed its channel says where to go."""
        guild = _Guild(REPORTING_GUILD, "Hmm", [REPORTING_CHANNEL])
        client = _Client(guild)
        self.assertEqual(
            await bot_channels.report_targets(REPORTING_GUILD, "bug", client=client), []
        )

    async def test_a_different_bot_server_can_be_named(self):
        await db.set_setting(REPORTING_GUILD, "bug_channel_id", REPORTING_CHANNEL)
        await db.set_setting(777777, "bug_channel_id", SECOND_CHANNEL)
        with mock.patch.dict(os.environ, {"BOT_SERVER_GUILD_ID": "777777"}):
            self.assertEqual(
                await bot_channels.report_targets(REPORTING_GUILD, "bug"),
                [REPORTING_CHANNEL, SECOND_CHANNEL],
            )

    async def test_a_malformed_setting_does_not_break_filing_a_report(self):
        """Read on the path that files a report: a typo must not be the reason."""
        with mock.patch.dict(os.environ, {"BOT_SERVER_GUILD_ID": "not-a-number"}):
            self.assertEqual(bot_channels.bot_server_guild_id(), 1550227368889684058)


@mock.patch.dict(os.environ, {"BOT_SERVER_GUILD_ID": str(BOT_SERVER)})
class FilingAReport(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """The whole path, driven the way an interaction drives it."""

    async def asyncSetUp(self):
        self.use_temp_database("bugfile.db")
        await db.setup()
        self.reporting = _Guild(REPORTING_GUILD, "Hmm", [REPORTING_CHANNEL])
        self.bot_server = _Guild(BOT_SERVER, "Development", [BOT_SERVER_CHANNEL, SECOND_CHANNEL])
        self.client = _Client(self.reporting, self.bot_server)

    async def asyncTearDown(self):
        self.release_temp_database()

    def _fill(self, modal, text="The Meow isn't meowing"):
        modal.what_happened._value = text
        return modal

    def _channel(self, guild, cid):
        return guild.channels[cid]

    async def test_a_report_from_a_server_with_its_own_channel_stays_there(self):
        await db.set_setting(REPORTING_GUILD, "bug_channel_id", REPORTING_CHANNEL)
        await db.set_setting(BOT_SERVER, "bug_channel_id", BOT_SERVER_CHANNEL)
        interaction = _Interaction(self.reporting, self.client)

        await help_cog.open_bug_report(interaction, source="`/bot bug`")
        modal = self._fill(interaction.response.modal)
        await modal.on_submit(interaction)

        self.assertEqual(len(self._channel(self.reporting, REPORTING_CHANNEL).posted), 1)
        self.assertEqual(self._channel(self.bot_server, BOT_SERVER_CHANNEL).posted, [])
        self.assertIn(str(REPORTING_CHANNEL), interaction.response.messages[-1])

    async def test_a_server_without_one_files_it_in_the_bots_own_server(self):
        """This is the case that read as 'the report did not show up'."""
        await db.set_setting(BOT_SERVER, "bug_channel_id", BOT_SERVER_CHANNEL)
        interaction = _Interaction(self.reporting, self.client)

        await help_cog.open_bug_report(interaction, source="the /help panel")
        modal = self._fill(interaction.response.modal)
        await modal.on_submit(interaction)

        posted = self._channel(self.bot_server, BOT_SERVER_CHANNEL).posted
        self.assertEqual(len(posted), 1, "a report with no home server must still be filed")
        self.assertEqual(self._channel(self.reporting, REPORTING_CHANNEL).posted, [])
        self.assertIn("Development", interaction.response.messages[-1])

    async def test_the_card_says_which_server_it_came_from(self):
        await db.set_setting(BOT_SERVER, "bug_channel_id", BOT_SERVER_CHANNEL)
        interaction = _Interaction(self.reporting, self.client)

        await help_cog.open_bug_report(interaction, source="the /help panel")
        await self._fill(interaction.response.modal).on_submit(interaction)

        embed = self._channel(self.bot_server, BOT_SERVER_CHANNEL).posted[0]
        fields = {field.name: field.value for field in embed.fields}
        # The name *and* the id: the id is what makes the card joinable to the
        # command log and to that server's module rows when the report is checked,
        # and a name alone cannot be searched for.
        self.assertIn("Hmm", fields["Server"])
        self.assertIn(str(self.reporting.id), fields["Server"])
        self.assertEqual(fields["Filed via"], "the /help panel")

    async def test_the_card_carries_the_command_and_when_it_happened(self):
        """A report nobody can check names neither what was run nor when."""
        await db.set_setting(REPORTING_GUILD, "bug_channel_id", REPORTING_CHANNEL)
        interaction = _Interaction(self.reporting, self.client)

        await help_cog.open_bug_report(interaction, source="`/bot bug`")
        modal = self._fill(interaction.response.modal)
        modal.command_used._value = "/balance"
        modal.when_happened._value = "today around 19:40"
        await modal.on_submit(interaction)

        embed = self._channel(self.reporting, REPORTING_CHANNEL).posted[0]
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Command"], "/balance")
        self.assertEqual(fields["When it happened"], "today around 19:40")
        self.assertIn("filed <t:", embed.footer.text, "the card has no timestamp to search around")
        self.assertIn(str(interaction.user.id), embed.footer.text)

    async def test_a_blank_command_and_time_say_so_rather_than_vanishing(self):
        """An absent field reads as a field nobody asked for; "not given" is the
        thing worth seeing, because it is why the report cannot be checked."""
        await db.set_setting(REPORTING_GUILD, "bug_channel_id", REPORTING_CHANNEL)
        interaction = _Interaction(self.reporting, self.client)

        await help_cog.open_bug_report(interaction, source="`/bot bug`")
        await self._fill(interaction.response.modal).on_submit(interaction)

        embed = self._channel(self.reporting, REPORTING_CHANNEL).posted[0]
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Command"], "*not given*")
        self.assertEqual(fields["When it happened"], "*not given*")

    async def test_the_command_line_prefills_the_form(self):
        """Typing the same two facts twice is how they end up left blank."""
        await db.set_setting(REPORTING_GUILD, "bug_channel_id", REPORTING_CHANNEL)
        interaction = _Interaction(self.reporting, self.client)

        await help_cog.open_bug_report(
            interaction,
            source="`/bot bug`",
            command="/market",
            when="about ten minutes ago",
        )

        modal = interaction.response.modal
        self.assertEqual(modal.command_used.default, "/market")
        self.assertEqual(modal.when_happened.default, "about ten minutes ago")
        self.assertIsNone(
            help_cog._prefill("   ", 100),
            "an empty prefill should leave the box empty, not show whitespace",
        )
        self.assertLessEqual(len(help_cog._prefill("x" * 500, 100)), 100)

    async def test_the_thread_is_named_after_the_command(self):
        """A list of report threads is scanned by what was being run."""
        interaction = _Interaction(self.reporting, self.client)
        message = _Message(_Channel(REPORTING_CHANNEL, self.reporting))

        await help_cog._open_report_thread(
            message, "Nothing happens when I check my balance", interaction, "/balance"
        )

        self.assertIn("/balance", message.thread)
        self.assertIn("Nothing happens", message.thread)
        self.assertLessEqual(len(message.thread), 100, "Discord caps a thread name at 100")

    async def test_a_thread_with_no_command_still_gets_a_name(self):
        interaction = _Interaction(self.reporting, self.client)
        message = _Message(_Channel(REPORTING_CHANNEL, self.reporting))
        await help_cog._open_report_thread(message, "   ", interaction, "")
        self.assertTrue(message.thread.strip(), "a nameless thread would be refused")

    async def test_the_administrators_command_is_named_as_the_source(self):
        await db.set_setting(REPORTING_GUILD, "bug_channel_id", REPORTING_CHANNEL)
        interaction = _Interaction(self.reporting, self.client)

        await help_cog.open_bug_report(interaction, source="`/bot bug`")
        await self._fill(interaction.response.modal).on_submit(interaction)

        embed = self._channel(self.reporting, REPORTING_CHANNEL).posted[0]
        self.assertIn("`/bot bug`", [f.value for f in embed.fields])

    async def test_nowhere_to_file_is_said_plainly_and_files_nothing(self):
        """The only refusal left: no channel here, and the bot's own server unseen."""
        interaction = _Interaction(self.reporting, _Client(self.reporting))

        await help_cog.open_bug_report(interaction, source="`/bot bug`")

        self.assertIsNone(interaction.response.modal)
        self.assertEqual(self._channel(self.reporting, REPORTING_CHANNEL).posted, [])
        self.assertEqual(self._channel(self.bot_server, BOT_SERVER_CHANNEL).posted, [])
        self.assertIn("Nowhere to file", interaction.response.messages[-1])

    async def test_a_channel_that_refuses_the_post_falls_through_to_the_next(self):
        """The fallback exists for exactly this: not every target is reachable."""
        await db.set_setting(REPORTING_GUILD, "bug_channel_id", REPORTING_CHANNEL)
        await db.set_setting(BOT_SERVER, "bug_channel_id", BOT_SERVER_CHANNEL)
        self._channel(self.reporting, REPORTING_CHANNEL).forbidden = True
        interaction = _Interaction(self.reporting, self.client)

        await help_cog.open_bug_report(interaction, source="`/bot bug`")
        await self._fill(interaction.response.modal).on_submit(interaction)

        self.assertEqual(len(self._channel(self.bot_server, BOT_SERVER_CHANNEL).posted), 1)
        self.assertIn("Development", interaction.response.messages[-1])

    async def test_a_gone_channel_does_not_strand_the_report(self):
        await db.set_setting(REPORTING_GUILD, "bug_channel_id", 999999)  # deleted
        await db.set_setting(BOT_SERVER, "bug_channel_id", BOT_SERVER_CHANNEL)
        interaction = _Interaction(self.reporting, self.client)

        await help_cog.open_bug_report(interaction, source="`/bot bug`")
        await self._fill(interaction.response.modal).on_submit(interaction)

        self.assertEqual(len(self._channel(self.bot_server, BOT_SERVER_CHANNEL).posted), 1)

    async def test_a_report_never_creates_a_channel_or_a_ticket(self):
        """The whole point: a ticket is a support conversation, not a bug report."""
        await db.set_setting(BOT_SERVER, "bug_channel_id", BOT_SERVER_CHANNEL)
        interaction = _Interaction(self.reporting, self.client)

        await help_cog.open_bug_report(interaction, source="`/bot bug`")
        await self._fill(interaction.response.modal).on_submit(interaction)

        self.assertEqual(self.reporting.created, [], "a bug report must not open a ticket")
        self.assertEqual(self.bot_server.created, [])

    async def test_the_report_opens_a_thread_on_the_card(self):
        await db.set_setting(REPORTING_GUILD, "bug_channel_id", REPORTING_CHANNEL)
        interaction = _Interaction(self.reporting, self.client)

        await help_cog.open_bug_report(interaction, source="`/bot bug`")
        await self._fill(interaction.response.modal).on_submit(interaction)

        self.assertIn("Follow it in", interaction.response.messages[-1])


class TheCommandAndTheButton(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("bugcmd.db")
        await db.setup()
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.failed = []
        for cog in features.cog_paths():
            try:
                await self.bot.load_extension(cog)
            except Exception as exc:  # pragma: no cover - reported as a failure
                self.failed.append(f"{cog}: {exc!r}")

    async def asyncTearDown(self):
        self.release_temp_database()
        try:
            await self.bot.close()
        except Exception:
            pass

    def test_every_cog_loads(self):
        self.assertEqual(self.failed, [])

    def test_the_administrator_command_exists_in_the_bot_group(self):
        group = self.bot.tree.get_command("bot")
        self.assertIsNotNone(group)
        self.assertIn("bug", [sub.name for sub in group.commands])

    def test_it_is_declared_administrator_only(self):
        self.assertEqual(permissions.COMMAND_ACCESS.get("bot bug"), permissions.ADMIN)
        command = self.bot.tree.get_command("bot").get_command("bug")
        self.assertTrue(command.checks, "an admin-only command must carry its check")
        # The picker stamp is applied by the publisher, not by the decorator, so
        # run it here: an admin-only command that is *offered* to everyone is the
        # mismatch the permission table exists to prevent.
        permissions.apply_declared_permissions(self.bot.tree)
        self.assertEqual(
            command.default_permissions, permissions.PICKER_VISIBILITY[permissions.ADMIN]
        )

    def test_it_files_through_the_same_path_as_the_panel_button(self):
        """Two entry points, one behaviour — or the two drift apart silently."""
        command = self.bot.tree.get_command("bot").get_command("bug")
        source = Path(command.callback.__code__.co_filename).read_text()
        self.assertIn("open_bug_report", source)
        self.assertIn("from cogs import help", source)


if __name__ == "__main__":
    unittest.main()
