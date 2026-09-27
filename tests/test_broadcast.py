"""One message, every server — and the four things that make that safe to have.

A broadcast is the only action in this product whose blast radius is *every* member
of every server the bot can reach, so what these laws pin is not that it sends, but
the four decisions that keep a send from being a mistake nobody can take back:

1. **The template is checked.** A token this module cannot fill in is refused
   rather than shipped as literal braces.
2. **Every server's destination is the server's own answer** — its bound ``#update``
   channel, Discord's system channel for that guild, or a *reason* it is not
   reached. Never a guess, and never a silent skip.
3. **The preview is the send with the send removed**, so the list the operator
   reads is the list that would happen.
4. **A ping is only ever a role**, and it is built by hand — because the message is
   a template and a *server's own name* is interpolated into it, so a guild called
   ``@everyone`` must not be able to mention anybody.
"""
import asyncio
import unittest

from tests.support import TempDatabaseMixin
from tests.test_composer import GUILD_ID, _Bot, _Channel, _Guild, _Role
from utils import bot_channels, broadcast, db

OTHER_ID = 999002
BOUND_CHANNEL = 7001
SYSTEM_CHANNEL = 7002


class _BroadcastCase(TempDatabaseMixin, unittest.TestCase):
    """Stand-in gateway objects and a database that can hold channel bindings."""

    def setUp(self):
        self.use_temp_database("broadcast.db")
        asyncio.run(db.setup())

    def tearDown(self):
        self.release_temp_database()

    def bind(self, guild_id, channel_id, role="update"):
        asyncio.run(db.set_setting(int(guild_id), bot_channels.setting_key(role), channel_id))

    @staticmethod
    def guild(gid, name, *, bound=False, system=False, can_send=True, roles=()):
        # ``can_send`` applies to every channel the guild has, because the case
        # being described is "the bot may not post here" rather than "the bot may
        # not post in this one" — a guild where only one channel refuses is the
        # ordinary case and is covered by the send path's own refusal.
        channels = [_Channel(gid * 10 + 1, "general", can_send=can_send)]
        if bound:
            channels.append(_Channel(BOUND_CHANNEL, "bot-updates", can_send=can_send))
        if system:
            channels.append(_Channel(SYSTEM_CHANNEL, "announcements", can_send=can_send))
        guild = _Guild(gid, name, channels, roles=list(roles))
        guild.member_count = 7
        if system:
            guild.system_channel = guild.get_channel(SYSTEM_CHANNEL)
        return guild

    def client(self, *guilds):
        return _Bot(list(guilds))


class TheTemplateIsCheckedBeforeItIsSent(_BroadcastCase):
    """A typo in a broadcast is visible on every server at once."""

    def test_a_token_that_cannot_be_filled_in_is_refused(self):
        with self.assertRaises(broadcast.Refused) as caught:
            broadcast.validate("Hello {serve} — read this")
        self.assertIn("{serve}", str(caught.exception))
        for name, _what in broadcast.PLACEHOLDERS:
            self.assertIn("{%s}" % name, str(caught.exception))

    def test_the_four_tokens_it_can_fill_in_are_accepted(self):
        text = " ".join("{%s}" % name for name, _what in broadcast.PLACEHOLDERS)
        self.assertEqual(broadcast.validate(text), text)

    def test_an_empty_message_and_an_endless_one_are_both_refused(self):
        with self.assertRaises(broadcast.Refused):
            broadcast.validate("   ")
        with self.assertRaises(broadcast.Refused) as caught:
            broadcast.validate("x" * (broadcast.MAX_CHARS + 1))
        self.assertIn(str(broadcast.MAX_CHARS), str(caught.exception))

    def test_a_figure_it_could_not_read_is_a_dash_and_not_a_zero(self):
        """The distinction this project makes everywhere: unread is not empty."""
        rendered = broadcast.render("{server} has {members} members", {"server": "X", "members": None})
        self.assertEqual(rendered, "X has — members")

    def test_the_message_is_filled_in_once_per_server(self):
        guild = self.guild(GUILD_ID, "First", bound=True)
        self.bind(guild.id, BOUND_CHANNEL)
        report = asyncio.run(
            broadcast.run(self.client(guild), "{server} ({members} people, {systems} systems)")
        )
        row = report["rows"][0]
        self.assertEqual(row["name"], "First")
        self.assertEqual(row["members"], 7)
        self.assertIsInstance(row["systems"], int)
        self.assertEqual(row["text"], f"First (7 people, {row['systems']} systems)")


class EveryServersTargetIsItsOwnAnswer(_BroadcastCase):
    """Where a message lands is the server's decision, and a skip is named."""

    def test_the_channel_a_server_bound_wins(self):
        guild = self.guild(GUILD_ID, "First", bound=True, system=True)
        self.bind(guild.id, BOUND_CHANNEL)
        report = asyncio.run(broadcast.run(self.client(guild), "hi"))
        row = report["rows"][0]
        self.assertEqual(row["channel_id"], BOUND_CHANNEL)
        self.assertEqual(row["via"], "update")
        self.assertTrue(row["reachable"])

    def test_a_server_that_bound_nothing_falls_back_to_its_system_channel(self):
        """Discord's own channel for service messages, and named as the *second*
        choice so the operator can see it was a fallback rather than a binding."""
        guild = self.guild(GUILD_ID, "First", system=True)
        report = asyncio.run(broadcast.run(self.client(guild), "hi"))
        row = report["rows"][0]
        self.assertEqual(row["channel_id"], SYSTEM_CHANNEL)
        self.assertEqual(row["via"], "system")

    def test_a_server_with_neither_is_named_rather_than_guessed_at(self):
        guild = self.guild(GUILD_ID, "First")
        report = asyncio.run(broadcast.run(self.client(guild), "hi"))
        row = report["rows"][0]
        self.assertFalse(row["reachable"])
        self.assertEqual(row["reason"], broadcast.NO_CHANNEL)
        self.assertEqual(report["unreached"], 1)
        self.assertIn("0 of 1", broadcast.summarise(report))

    def test_a_channel_the_bot_may_not_post_in_is_named_too(self):
        guild = self.guild(GUILD_ID, "First", bound=True, can_send=False)
        self.bind(guild.id, BOUND_CHANNEL)
        report = asyncio.run(broadcast.run(self.client(guild), "hi"))
        self.assertEqual(report["rows"][0]["reason"], broadcast.NO_PERMISSION)


class ThePreviewIsTheSendWithTheSendRemoved(_BroadcastCase):
    """The list the operator reads before pressing the other button."""

    def _fixture(self):
        bound = self.guild(GUILD_ID, "First", bound=True)
        self.bind(bound.id, BOUND_CHANNEL)
        unreached = self.guild(OTHER_ID, "Second")
        return self.client(bound, unreached), bound, unreached

    def test_a_preview_sends_nothing_and_still_resolves_every_target(self):
        client, bound, unreached = self._fixture()
        report = asyncio.run(broadcast.run(client, "hello {server}"))
        self.assertFalse(report["send"])
        for guild in report["rows"]:
            channel = (bound if guild["name"] == "First" else unreached).get_channel(
                guild["channel_id"] or BOUND_CHANNEL
            )
            if channel is not None:
                self.assertEqual(channel.sent, [], "the preview posted a message")
        self.assertEqual(report["reachable"], 1)
        self.assertEqual(report["sent"], 0)
        self.assertIn("Nothing was sent", broadcast.summarise(report))
        self.assertEqual(bound.get_channel(BOUND_CHANNEL).sent, [])

    def test_the_send_posts_once_per_server_with_that_servers_own_text(self):
        client, bound, _unreached = self._fixture()
        report = asyncio.run(broadcast.run(client, "{server} is having a moment", send=True))
        posted = bound.get_channel(BOUND_CHANNEL).sent
        self.assertEqual(len(posted), 1, "one server received the message more than once")
        self.assertEqual(posted[0]["content"], "First is having a moment")
        self.assertEqual(report["sent"], 1)
        self.assertIn("Sent to 1 of 2 servers", broadcast.summarise(report))

    def test_one_server_refusing_does_not_stop_the_rest(self):
        refusing = self.guild(GUILD_ID, "First", bound=True)
        self.bind(refusing.id, BOUND_CHANNEL)
        refusing.get_channel(BOUND_CHANNEL).send_error = PermissionError("nope")
        fine = self.guild(OTHER_ID, "Second", bound=True)
        self.bind(fine.id, BOUND_CHANNEL)
        report = asyncio.run(broadcast.run(self.client(refusing, fine), "hi", send=True))
        self.assertEqual(report["sent"], 1, "a refusal stopped the servers after it")
        self.assertEqual(report["failed"], 1)
        self.assertEqual(len(fine.get_channel(BOUND_CHANNEL).sent), 1)
        summary = broadcast.summarise(report)
        self.assertIn("1 of 2", summary)
        self.assertIn("refused by Discord", summary)

    def test_a_fleet_with_no_servers_says_so_rather_than_reporting_zero(self):
        report = asyncio.run(broadcast.run(self.client(), "hi", send=True))
        self.assertEqual(report["total"], 0)
        self.assertIn("not in any server", broadcast.summarise(report))


class AMentionIsOnlyEverARole(_BroadcastCase):
    """The message is a template, so what it may mention is stated, not inferred."""

    def _sent(self, guild, **kwargs):
        channel_id = guild.get_channel(BOUND_CHANNEL).id if guild.get_channel(BOUND_CHANNEL) else None
        if channel_id is not None:
            self.bind(guild.id, BOUND_CHANNEL)
        asyncio.run(broadcast.run(self.client(guild), kwargs.pop("template"), send=True, **kwargs))
        channel = guild.get_channel(channel_id) if channel_id else guild.text_channels[0]
        return channel.sent, channel

    def test_a_server_named_everyone_cannot_mention_anybody(self):
        """The interpolation is the whole reason the allowance is built by hand."""
        guild = self.guild(GUILD_ID, "@everyone", bound=True)
        sent, _channel = self._sent(guild, template="{server} please read this")
        self.assertEqual(len(sent), 1)
        mentions = sent[0]["allowed_mentions"]
        self.assertIsNotNone(mentions, "the mention allowance was left to Discord to infer")
        # ``everyone`` defaults to true in discord.py, so this is the assertion the
        # whole hand-built allowance exists for.
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertFalse(mentions.users)

    def test_asking_to_ping_mentions_one_role_and_never_everyone(self):
        guild = self.guild(GUILD_ID, "First", bound=True, roles=[_Role(555, "admin")])
        sent, _channel = self._sent(guild, template="read this", ping=True)
        mentions = sent[0]["allowed_mentions"]
        self.assertEqual([role.name for role in mentions.roles], ["admin"])
        self.assertFalse(mentions.everyone)
        self.assertTrue(sent[0]["content"].startswith("<@&555>"))

    def test_a_server_with_no_such_role_is_not_mentioned_at_all(self):
        """No role to ping is a message with no mention in it, not an @everyone."""
        guild = self.guild(GUILD_ID, "First", bound=True)
        self.bind(guild.id, BOUND_CHANNEL)
        asyncio.run(broadcast.run(self.client(guild), "read this", send=True, ping=True))
        message = guild.get_channel(BOUND_CHANNEL).sent[0]
        self.assertFalse(message["content"].startswith("<@&"))
        self.assertFalse(message["allowed_mentions"].everyone)


if __name__ == "__main__":
    unittest.main()
