"""The operator's own log: who has the bot, and what people run with it.

Two channels in the bot's own server — ``#active-servers`` and
``#real-time-cmd-data`` — are fed by three events: a server adding the bot, a
server removing it, and every command anyone runs anywhere.

What makes this worth its own tests is that all three are *loggers*. They run
after the thing they describe has already succeeded (or failed for its own
reasons), they touch a database, the network and another server's channel, and
none of that may be able to break the thing being logged. The rules pinned here:

* a log entry never reaches a member's own server — only the operator's;
* a log entry never pings anyone, even though it names members and servers;
* **the values typed into a command are never logged** — only its name;
* a missing channel, a raising send and an unreadable audit log are all reported
  and swallowed, never raised into the caller.
"""
import asyncio
import unittest
from pathlib import Path
from unittest import mock

import discord

import bot as bot_module
from tests.support import TempDatabaseMixin
from utils import bot_channels, db, operator_log, style

BOT_SERVER = 1550227368889684058
MEMBER_SERVER = 1542278896148422676
ROOT = Path(__file__).resolve().parent.parent

GUILDS_CHANNEL = 1550255301486190592
CMDLOG_CHANNEL = 1550255387402444811


def _text_of(message) -> str:
    """Every string a card can carry, so a test can assert on what is *said*."""
    parts = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "content" and isinstance(value, str):
                    parts.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        else:
            content = getattr(node, "content", None)
            if isinstance(content, str):
                parts.append(content)
            for child in getattr(node, "children", None) or []:
                walk(child)

    walk(message)
    return "\n".join(parts)


class _Channel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return _Message(self, kwargs)


class _Message:
    def __init__(self, channel, kwargs):
        self.channel = channel
        self.kwargs = kwargs
        self.id = 9000

    def to_dict(self):
        return {"content": ""}

    @property
    def text(self):
        return _text_of(self.kwargs)


class _FailingChannel(_Channel):
    async def send(self, **kwargs):
        raise discord.Forbidden(_Response(), "missing access")


class _Response:
    status = 403
    reason = "Forbidden"


class _Role:
    def __init__(self, rid, name, default=False):
        self.id = rid
        self.name = name
        self._default = default

    def is_default(self):
        return self._default


class _Member:
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name
        self.name = name


class _Entry:
    def __init__(self, target, user):
        self.target = target
        self.user = user


class _Audit:
    """Stands in for ``guild.audit_logs(...)`` — an async iterator of entries."""

    def __init__(self, entries, raises=None):
        self._entries = entries
        self._raises = raises

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self

    def __aiter__(self):
        async def gen():
            if self._raises is not None:
                raise self._raises
            for entry in self._entries:
                yield entry

        return gen()


class _Guild:
    def __init__(self, gid, name, channels=(), member_count=10, me=None,
                 owner=None, entries=(), audit_raises=None):
        self.id = gid
        self.name = name
        self.text_channels = list(channels)
        self.member_count = member_count
        self.me = me
        self.owner = owner
        self.audit_logs = _Audit(list(entries), raises=audit_raises)
        self.roles = []

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)


class _Bot:
    """The two lookups the logger makes, and nothing else."""

    def __init__(self, guilds):
        self.guilds = list(guilds)

    def get_guild(self, gid):
        return next((g for g in self.guilds if g.id == gid), None)

    def get_channel(self, cid):
        for guild in self.guilds:
            found = guild.get_channel(cid)
            if found is not None:
                return found
        return None


class _User:
    def __init__(self, uid=1, name="Operator"):
        self.id = uid
        self.display_name = name
        self.name = name


class _Command:
    def __init__(self, qualified_name):
        self.qualified_name = qualified_name


class _Interaction:
    def __init__(self, guild, channel, user, data=None):
        self.guild = guild
        self.channel = channel
        self.user = user
        self.data = data or {}


class OperatorLogCase(TempDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.use_temp_database("operator-log.db")

        self.guilds_channel = _Channel(GUILDS_CHANNEL, "active-servers")
        self.cmdlog_channel = _Channel(CMDLOG_CHANNEL, "real-time-cmd-data")
        self.operator = _Guild(
            BOT_SERVER, "Development", [self.guilds_channel, self.cmdlog_channel]
        )
        # A member's own server, with channels the bot already uses for other
        # things. Nothing this module writes may ever land in one of these.
        self.foreign = _Channel(4242, "general")
        self.member = _Guild(MEMBER_SERVER, "Hmm", [self.foreign])
        self.bot = _Bot([self.operator, self.member])

    def tearDown(self):
        self.release_temp_database()

    async def _bind_operator_roles(self):
        await db.setup()
        await db.set_setting(BOT_SERVER, bot_channels.setting_key("guilds"), GUILDS_CHANNEL)
        await db.set_setting(BOT_SERVER, bot_channels.setting_key("cmdlog"), CMDLOG_CHANNEL)


class GuildLifecycle(OperatorLogCase):
    def test_a_join_is_logged_once_in_the_operator_server(self):
        guild = _Guild(MEMBER_SERVER, "Hmm", [self.foreign], member_count=42)

        async def run():
            await self._bind_operator_roles()
            return await operator_log.guild_joined(self.bot, guild)

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(len(self.guilds_channel.sent), 1)
        self.assertEqual(self.foreign.sent, [], "a join must not be announced locally")

        text = _text_of(self.guilds_channel.sent[0])
        self.assertIn("Hmm", text)
        self.assertIn(str(MEMBER_SERVER), text, "the id is what makes it greppable")
        self.assertIn("42", text, "the size is the point of the entry")

    def test_a_leave_says_which_server_and_how_many_remain(self):
        guild = _Guild(MEMBER_SERVER, "Hmm", [self.foreign], member_count=7)

        async def run():
            await self._bind_operator_roles()
            return await operator_log.guild_left(self.bot, guild)

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(len(self.guilds_channel.sent), 1)
        self.assertEqual(self.foreign.sent, [])
        text = _text_of(self.guilds_channel.sent[0])
        self.assertIn("Hmm", text)
        self.assertIn(str(MEMBER_SERVER), text)

    def test_the_audit_log_names_who_added_the_bot(self):
        adder = _Member(555, "RAJ")
        guild = _Guild(
            MEMBER_SERVER, "Hmm", [self.foreign],
            me=_Member(1548380269776605324, "Meow"),
            entries=[_Entry(_Member(1548380269776605324, "Meow"), adder)],
        )

        async def run():
            await self._bind_operator_roles()
            return await operator_log.guild_joined(self.bot, guild)

        self.assertTrue(asyncio.run(run()))
        text = _text_of(self.guilds_channel.sent[0])
        self.assertIn("RAJ", text)
        self.assertIn("555", text)

    def test_an_unreadable_audit_log_is_unknown_not_a_failure(self):
        guild = _Guild(
            MEMBER_SERVER, "Hmm", [self.foreign],
            me=_Member(1548380269776605324, "Meow"),
            audit_raises=discord.Forbidden(_Response(), "missing access"),
        )

        async def run():
            await self._bind_operator_roles()
            return await operator_log.guild_joined(self.bot, guild)

        self.assertTrue(asyncio.run(run()), "the entry still gets written")
        text = _text_of(self.guilds_channel.sent[0])
        self.assertIn("unknown", text.lower())
        self.assertIn("View Audit Log", text, "and it says which permission fills it in")


class CommandLog(OperatorLogCase):
    def _interaction(self, data=None):
        return _Interaction(
            _Guild(MEMBER_SERVER, "Hmm", [self.foreign]),
            self.foreign,
            _User(777, "RAJ"),
            data=data,
        )

    def test_a_command_is_logged_with_who_ran_it_and_where(self):
        async def run():
            await self._bind_operator_roles()
            return await operator_log.command_used(
                self.bot, self._interaction(), _Command("eco-admin")
            )

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(len(self.cmdlog_channel.sent), 1)
        self.assertEqual(self.foreign.sent, [])
        text = _text_of(self.cmdlog_channel.sent[0])
        self.assertIn("eco-admin", text)
        self.assertIn("RAJ", text)
        self.assertIn("777", text)
        self.assertIn("Hmm", text)
        self.assertIn("general", text, "the channel is part of the trail")

    def test_what_a_person_typed_is_never_copied_into_the_log(self):
        """The one privacy rule this module can break on its own.

        A command's options are whatever someone chose to write — a report, a
        DM, a pay note — and the log's job is the command, not its arguments.
        """
        secret = "my-private-report-text"
        interaction = self._interaction(
            data={"name": "bug", "options": [{"name": "details", "value": secret}]}
        )

        async def run():
            await self._bind_operator_roles()
            return await operator_log.command_used(
                self.bot, interaction, _Command("bug")
            )

        self.assertTrue(asyncio.run(run()))
        text = _text_of(self.cmdlog_channel.sent[0])
        self.assertNotIn(secret, text)

    def test_a_direct_message_run_is_logged_without_a_server(self):
        interaction = _Interaction(None, None, _User(9, "RAJ"))

        async def run():
            await self._bind_operator_roles()
            return await operator_log.command_used(
                self.bot, interaction, _Command("help")
            )

        self.assertTrue(asyncio.run(run()))
        text = _text_of(self.cmdlog_channel.sent[0])
        self.assertIn("help", text)
        self.assertIn("Direct message", text)


class FailureRules(OperatorLogCase):
    def test_an_unbound_channel_reports_and_returns_false(self):
        """No binding: the entry is reported on the host, not raised."""
        async def run():
            await db.setup()
            return await operator_log.command_used(
                self.bot, _Interaction(None, None, _User()), _Command("help")
            )

        self.assertFalse(asyncio.run(run()))

    def test_a_channel_that_refuses_the_send_is_swallowed(self):
        """A log that breaks a command is worse than no log at all."""
        failing = _FailingChannel(GUILDS_CHANNEL, "active-servers")
        operator = _Guild(BOT_SERVER, "Development", [failing])
        bot = _Bot([operator])
        guild = _Guild(MEMBER_SERVER, "Hmm", [self.foreign])

        async def run():
            await db.setup()
            await db.set_setting(
                BOT_SERVER, bot_channels.setting_key("guilds"), GUILDS_CHANNEL
            )
            return await operator_log.guild_joined(bot, guild)

        self.assertFalse(asyncio.run(run()))

    def test_a_member_server_can_never_bind_these_roles(self):
        """The two channels belong to the operator, by construction."""
        self.assertIn("guilds", bot_channels.roles_for(BOT_SERVER))
        self.assertIn("cmdlog", bot_channels.roles_for(BOT_SERVER))
        self.assertNotIn("guilds", bot_channels.roles_for(MEMBER_SERVER))
        self.assertNotIn("cmdlog", bot_channels.roles_for(MEMBER_SERVER))

    def test_a_server_named_after_a_mention_cannot_ping_the_operator(self):
        """A server name is a stranger's text, and it lands in the operator's channel.

        Anyone can name their server ``@everyone``; if that were rendered as a
        mention, logging a join would ping the operator's whole server.
        """
        guild = _Guild(MEMBER_SERVER, "@everyone @here", [self.foreign])

        async def run():
            await self._bind_operator_roles()
            return await operator_log.guild_joined(self.bot, guild)

        self.assertTrue(asyncio.run(run()))
        sent = self.guilds_channel.sent[0]
        allowed = sent.get("allowed_mentions")
        self.assertIsNotNone(
            allowed, "the send has to carry the suppression itself"
        )
        self.assertEqual(allowed.to_dict(), style.NO_PINGS.to_dict())
        self.assertTrue(
            isinstance(sent.get("view"), style.LayoutView),
            "and it is a V2 card, which is the payload that would ping",
        )

    def test_a_malformed_operator_guild_id_falls_back_instead_of_raising(self):
        """A typo in an environment variable must not cost the log its entry."""
        async def run():
            await self._bind_operator_roles()
            with mock.patch.dict("os.environ", {"BOT_SERVER_GUILD_ID": "development"}):
                return await operator_log.guild_joined(
                    self.bot, _Guild(MEMBER_SERVER, "Hmm", [self.foreign])
                )

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(len(self.guilds_channel.sent), 1)

    def test_no_card_carries_a_raw_mention_of_the_logged_member(self):
        async def run():
            await self._bind_operator_roles()
            return await operator_log.command_used(
                self.bot,
                _Interaction(_Guild(MEMBER_SERVER, "Hmm"), self.foreign, _User(777, "RAJ")),
                _Command("help"),
            )

        self.assertTrue(asyncio.run(run()))
        text = _text_of(self.cmdlog_channel.sent[0])
        self.assertNotIn("<@777>", text, "the name and id are plain text, never a ping")


class FirstRun(OperatorLogCase):
    """An installed feature and a working one looked identical. Not any more."""

    def setUp(self):
        super().setUp()
        operator_log._warned.clear()

    def tearDown(self):
        operator_log._warned.clear()
        super().tearDown()

    def test_the_boot_that_binds_them_says_what_they_are_for(self):
        async def run():
            await self._bind_operator_roles()
            return await operator_log.announce_setup(
                self.bot,
                self.operator,
                {"guilds": GUILDS_CHANNEL, "cmdlog": CMDLOG_CHANNEL},
            )

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(len(self.guilds_channel.sent), 1, "into the channel it wired up")
        text = _text_of(self.guilds_channel.sent[0])
        self.assertIn("#active-servers", text)
        self.assertIn("#real-time-cmd-data", text)
        self.assertIn("adds", text, "it says what will arrive here")
        self.assertIn("every command", text)
        self.assertIn("/bot db", text, "and what to do after a rename")

    def test_the_card_is_posted_from_what_was_bound_not_from_a_read_back(self):
        """`newly` is the record of what this boot wrote, so it is believed first.

        Caught live: the card came out *silent* on a first run because the caller
        had been handed the bindings but the store was consulted for them, and a
        write that has not landed (or did not) would cost the one card whose whole
        purpose is to explain the channel.
        """
        async def run():
            await db.setup()  # nothing written to the settings at all
            return await operator_log.announce_setup(
                self.bot,
                self.operator,
                {"guilds": GUILDS_CHANNEL, "cmdlog": CMDLOG_CHANNEL},
            )

        self.assertTrue(asyncio.run(run()))
        text = _text_of(self.guilds_channel.sent[0])
        self.assertIn("#real-time-cmd-data", text)
        self.assertNotIn("not bound", text, "both really were bound this boot")

    def test_a_boot_that_changes_nothing_says_nothing(self):
        """Otherwise every restart would re-introduce both channels."""
        async def run():
            await self._bind_operator_roles()
            return await operator_log.announce_setup(self.bot, self.operator, {})

        self.assertFalse(asyncio.run(run()))
        self.assertEqual(self.guilds_channel.sent, [])
        self.assertEqual(self.cmdlog_channel.sent, [])

    def test_the_setup_card_points_at_the_channel_that_is_missing(self):
        """Half the feature installed is the likeliest state, and the card says so."""
        async def run():
            await db.setup()
            await db.set_setting(
                BOT_SERVER, bot_channels.setting_key("guilds"), GUILDS_CHANNEL
            )
            return await operator_log.announce_setup(
                self.bot, self.operator, {"guilds": GUILDS_CHANNEL}
            )

        self.assertTrue(asyncio.run(run()))
        text = _text_of(self.guilds_channel.sent[0])
        self.assertIn("not bound", text)
        self.assertIn("#real-time-cmd-data", text, "it names the one to create")

    def test_an_entry_with_nowhere_to_go_reaches_the_operator(self):
        """The host log is not an audience — the operator is."""
        update = _Channel(5555, "update")
        operator = _Guild(
            BOT_SERVER, "Development", [update, self.guilds_channel, self.cmdlog_channel]
        )
        bot = _Bot([operator, self.member])

        async def run():
            await db.setup()
            await db.set_setting(
                BOT_SERVER, bot_channels.setting_key("update"), update.id
            )
            return await operator_log.guild_joined(
                bot, _Guild(MEMBER_SERVER, "Hmm", [self.foreign])
            )

        self.assertFalse(asyncio.run(run()), "the entry itself cannot be written")
        self.assertEqual(len(update.sent), 1, "but the operator is told why")
        text = _text_of(update.sent[0])
        self.assertIn("#active-servers", text, "and it names the channel to create")
        self.assertIn("/bot db", text)

    def test_that_notice_is_sent_once_not_once_per_command(self):
        """A notice per command would bury the channel it is trying to save."""
        update = _Channel(5555, "update")
        operator = _Guild(BOT_SERVER, "Development", [update])
        bot = _Bot([operator, self.member])

        async def run():
            await db.setup()
            await db.set_setting(
                BOT_SERVER, bot_channels.setting_key("update"), update.id
            )
            results = []
            for index in range(4):
                results.append(
                    await operator_log.command_used(
                        bot,
                        _Interaction(None, None, _User(index, "RAJ")),
                        _Command("help"),
                    )
                )
            return results

        self.assertEqual(asyncio.run(run()), [False] * 4, "every entry still reports")
        self.assertEqual(len(update.sent), 1, "exactly one notice")

    def test_nothing_bound_anywhere_stays_quiet_instead_of_looping(self):
        """With no channel at all there is nothing to post to and nothing to try."""
        bare = _Bot([_Guild(BOT_SERVER, "Development", []), self.member])

        async def run():
            await db.setup()
            return await operator_log.guild_joined(
                bare, _Guild(MEMBER_SERVER, "Hmm", [self.foreign])
            )

        self.assertFalse(asyncio.run(run()))
        self.assertEqual(self.foreign.sent, [], "never into a member's server")


class _Boom:
    """A client whose every lookup fails, the way a half-built one can."""

    def __getattr__(self, name):
        raise RuntimeError(f"boom.{name}")


class _OddGuild:
    """A guild with the wrong types in every field the log reads."""

    id = BOT_SERVER
    name = None
    text_channels = None
    member_count = "not a number"
    owner = object()
    me = None

    def __getattr__(self, name):
        if name == "audit_logs":
            raise RuntimeError("no audit log access")
        raise AttributeError(name)

    def get_channel(self, cid):
        return None


class _BoomBot:
    """``get_guild``/``get_channel`` raise; nothing else is defined."""

    guilds: list = []

    def get_guild(self, gid):
        return _Boom().x

    def get_channel(self, cid):
        return _Boom().x


class NeverBreaksItsCaller(OperatorLogCase):
    """Every entry point returns an answer; none of them raises.

    These run *after* the thing they describe has already worked, or on a boot
    loop that has other servers left to bind. A fuzz over deliberately broken
    clients found ``announce_setup`` raising where its siblings returned
    ``False`` — its guard covered only the send, not the lookups that find the
    channel to send to.
    """

    def setUp(self):
        super().setUp()
        operator_log._warned.clear()

    def tearDown(self):
        operator_log._warned.clear()
        super().tearDown()

    def _entries(self, bot):
        return (
            ("guild_joined", lambda: operator_log.guild_joined(bot, _OddGuild())),
            ("guild_left", lambda: operator_log.guild_left(bot, _OddGuild())),
            ("command_used", lambda: operator_log.command_used(
                bot,
                _Interaction(None, None, _User(1, None)),
                type("C", (), {"qualified_name": None})(),
            )),
            ("announce_setup", lambda: operator_log.announce_setup(
                bot, _OddGuild(), {"guilds": GUILDS_CHANNEL, "cmdlog": CMDLOG_CHANNEL}
            )),
        )

    def test_a_client_that_cannot_answer_is_reported_not_raised(self):
        async def run():
            await db.setup()
            return [(name, await call()) for name, call in self._entries(_BoomBot())]

        results = asyncio.run(run())
        for name, value in results:
            self.assertIs(value, False, f"{name} should report, not raise")

    def test_a_guild_full_of_wrong_types_is_reported_not_raised(self):
        """None for a name, a string where a count goes, no ``audit_logs`` at all.

        Nothing is bound in this case, so the three entries have nowhere to go
        and say so. ``announce_setup`` still succeeds — deliberately, and worth
        pinning: it describes the *bindings* and reads the guild only for its id,
        so a server whose name or member count is missing is not a reason to
        withhold the card that explains the channels it was just given.
        """
        async def run():
            await db.setup()
            return dict([(name, await call()) for name, call in self._entries(self.bot)])

        results = asyncio.run(run())
        for name in ("guild_joined", "guild_left", "command_used"):
            self.assertIs(results[name], False, f"{name} should report, not raise")
        self.assertIs(results["announce_setup"], True, "the card does not need a name")

    def test_every_newly_shape_is_survivable(self):
        """``newly`` comes from the binder, but it is an argument like any other."""
        async def run():
            await db.setup()
            out = []
            for newly in (None, {}, {"guilds": None}, {"guilds": "not-an-id"}, {"unknown": 1}):
                out.append(await operator_log.announce_setup(self.bot, self.operator, newly))
            return out

        self.assertEqual(asyncio.run(run()), [False, False, False, False, False])


class BotWiring(unittest.TestCase):
    """The three events have to actually be registered, or none of this runs."""

    def test_the_bot_listens_for_servers_joining_and_leaving(self):
        self.assertTrue(callable(getattr(bot_module.bot, "on_guild_join", None)))
        self.assertTrue(callable(getattr(bot_module.bot, "on_guild_remove", None)))

    def test_every_completed_command_reaches_the_log(self):
        """`on_app_command_completion` is the only place a finished command is seen.

        Completion, not ``on_interaction``: a command that failed never completes,
        and a log that counts attempts as uses would read as healthy while it broke.
        """
        interaction = mock.Mock()
        interaction.user = _User(1, "RAJ")
        interaction.guild = None
        command = _Command("help")
        seen = []

        async def spy(bot, got_interaction, got_command):
            seen.append((got_interaction, got_command))
            return True

        with mock.patch.object(operator_log, "command_used", spy):
            asyncio.run(
                bot_module.on_app_command_completion(interaction, command)
            )
        self.assertEqual(seen, [(interaction, command)])

    def test_a_command_the_bot_does_not_recognise_is_not_an_error(self):
        """A context menu and a subcommand both reach here, and names vary."""
        for name in ("help", "eco-admin add", "Purge Message"):
            with self.subTest(name=name):
                interaction = mock.Mock()
                interaction.user = _User(1, "RAJ")
                interaction.guild = None

                async def spy(bot, got, command):
                    return True

                with mock.patch.object(operator_log, "command_used", spy):
                    asyncio.run(
                        bot_module.on_app_command_completion(
                            interaction, _Command(name)
                        )
                    )


class GuildJoinHandler(unittest.TestCase):
    """A server added mid-session: bind, announce, log — in that order, isolated."""

    def _guild(self):
        return _Guild(MEMBER_SERVER, "Hmm", [_Channel(4242, "general")])

    def _run(self, *, bind=None, announce=None, joined=None):
        calls = {"bind": [], "announce": [], "logged": []}

        async def default_bind(guild):
            calls["bind"].append(guild.id)
            return {"update": 1}

        async def default_announce(guild, current_hash=None):
            calls["announce"].append(guild.id)
            return "posted"

        async def default_joined(bot, guild):
            calls["logged"].append(guild.id)
            return True

        with mock.patch.object(bot_channels, "ensure_bound", bind or default_bind), \
             mock.patch.object(bot_module, "announce_release_notes_to_guild",
                               announce or default_announce), \
             mock.patch.object(operator_log, "guild_joined", joined or default_joined):
            asyncio.run(bot_module.bot.on_guild_join(self._guild()))
        return calls

    def test_a_new_server_is_bound_announced_and_logged(self):
        calls = self._run()
        self.assertEqual(calls["bind"], [MEMBER_SERVER], "its own channels by name")
        self.assertEqual(calls["announce"], [MEMBER_SERVER], "it gets a release note")
        self.assertEqual(calls["logged"], [MEMBER_SERVER], "and the operator hears about it")

    def test_a_failed_binding_does_not_stop_the_note_or_the_log(self):
        """These are three independent steps; one failing must not hide the others."""
        async def broken_bind(guild):
            raise RuntimeError("no permission to read the channel list")

        calls = self._run(bind=broken_bind)
        self.assertEqual(calls["announce"], [MEMBER_SERVER])
        self.assertEqual(calls["logged"], [MEMBER_SERVER])

    def test_a_failed_announcement_still_gets_logged(self):
        """The log is the record that the server arrived, so it is last and it runs."""
        async def broken_announce(guild, current_hash=None):
            raise RuntimeError("Discord refused the message")

        calls = self._run(announce=broken_announce)
        self.assertEqual(calls["bind"], [MEMBER_SERVER])
        self.assertEqual(calls["logged"], [MEMBER_SERVER])

    def test_the_setup_card_cannot_cost_a_later_server_its_channels(self):
        """The boot loop binds *every* server; the card it posts is cosmetic.

        Caught in review: the call sat inside the loop's single ``try``, so a
        failure on the card for one server would have skipped binding the rest —
        a decoration taking priority over the thing it decorates.
        """
        source = (ROOT / "bot.py").read_text()
        loop = source.split("for guild in bot.guilds:", 1)[1].split("@bot.event", 1)[0]
        region = loop.split("# Auto-announce updates", 1)[0]
        self.assertIn("announce_setup", region)
        before = region.split("announce_setup", 1)[0]
        self.assertIn(
            "try:", before.rsplit("continue", 1)[-1],
            "the card needs its own guard inside the loop, not the loop's own",
        )

    def test_a_leave_is_handled(self):
        seen = []

        async def spy(bot, guild):
            seen.append(guild.id)
            return True

        with mock.patch.object(operator_log, "guild_left", spy):
            asyncio.run(bot_module.bot.on_guild_remove(self._guild()))
        self.assertEqual(seen, [MEMBER_SERVER])

    def test_the_join_handler_does_not_sync_commands_per_guild(self):
        """Commands are published globally, so a new server already has them.

        A per-guild publish here would burn the 200-writes-a-day budget on a
        duplicate of a tree Discord is already carrying. Pinned because it looks
        like an omission otherwise.
        """
        source = (ROOT / "bot.py").read_text()
        handler = source.split("async def on_guild_join", 1)[1].split("@bot.event", 1)[0]
        self.assertNotIn(
            "_sync_guild", handler,
            "global commands reach a new server; do not also publish per guild",
        )


if __name__ == "__main__":
    unittest.main()
