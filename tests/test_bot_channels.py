"""A second server, and the channels it wants the bot to use.

Three of the bot's four house channels had no per-server home. The update channel
was a literal in ``bot.py`` naming one server's channel, the error path knew only
a webhook configured for the whole process, and ``#bug`` had nothing behind it at
all. ``utils/bot_channels`` is the layer that fixed that, and these tests pin the
two ways it can fail quietly: binding the wrong channel, and letting one server's
unreachable channel consume another server's note.
"""
import unittest
from pathlib import Path
from unittest import mock

import bot as bot_module
from tests.support import TempDatabaseMixin
from utils import bot_channels, db, version

BOT_PY = Path(__file__).resolve().parent.parent / "bot.py"
HELP_PY = Path(__file__).resolve().parent.parent / "cogs" / "help.py"

NEW_SERVER = 1550227368889684058
HOME_SERVER = 1542278896148422676


class _Channel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.mention = f"<#{cid}>"


class _Guild:
    """Just enough guild for the binding code, which only reads names and ids."""

    def __init__(self, gid, names):
        self.id = gid
        self.text_channels = [_Channel(7000 + i, name) for i, name in enumerate(names)]

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)


class _ChannelGuild:
    """Just enough guild for the own-server question, which reads an id and looks
    one channel up by id."""

    def __init__(self, gid, channels):
        self.id = gid
        self.text_channels = list(channels)

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)


class _Role:
    def __init__(self, rid, name, default=False):
        self.id = rid
        self.name = name
        self._default = default

    def is_default(self):
        return self._default


class _RoleGuild:
    """Just enough guild for the ping lookup, which only reads roles."""

    def __init__(self, roles):
        self.roles = roles

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)


def _setting(guild_id: int, key: str):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT value FROM guild_settings WHERE guild_id=? AND key=?",
            (guild_id, key),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


class NameMatching(unittest.TestCase):
    """Which channel a name means. Getting this wrong is silent and permanent."""

    def test_the_most_specific_name_wins_whatever_order_the_guild_lists(self):
        self.assertEqual(
            bot_channels.pick([_Channel(11, "bug"), _Channel(22, "bug-report")], "bug"),
            22,
        )

    def test_the_answer_does_not_depend_on_the_cache_order(self):
        self.assertEqual(
            bot_channels.pick([_Channel(22, "bug-report"), _Channel(11, "bug")], "bug"),
            22,
            "a server that names a channel exactly got the generic one instead",
        )

    def test_names_match_without_regard_to_case(self):
        self.assertEqual(bot_channels.pick([_Channel(5, "Bug-Report")], "bug"), 5)

    def test_a_test_channel_answers_only_for_the_test_role(self):
        """Adding a sixth role must not let its names answer for the other five."""
        channels = [_Channel(11, "test"), _Channel(22, "beta")]
        self.assertEqual(bot_channels.pick(channels, "test"), 11)
        for role in ("update", "error", "bug", "commands", "log"):
            self.assertIsNone(bot_channels.pick(channels, role), role)

    def test_the_names_a_server_actually_used_are_recognised(self):
        guild = _Guild(NEW_SERVER, ["update", "bot-error", "bug"])
        self.assertEqual(bot_channels.pick(guild.text_channels, "update"), 7000)
        self.assertEqual(bot_channels.pick(guild.text_channels, "error"), 7001)
        self.assertEqual(bot_channels.pick(guild.text_channels, "bug"), 7002)

    def test_a_server_that_named_nothing_stays_unbound(self):
        guild = _Guild(4242, ["general", "off-topic", "memes"])
        for role in bot_channels.ROLES:
            self.assertIsNone(bot_channels.pick(guild.text_channels, role))

    def test_no_name_is_claimed_by_two_roles(self):
        """#logs must never become the error channel, or vice versa."""
        seen: dict[str, str] = {}
        for role in bot_channels.ROLES:
            for name in bot_channels.candidates(role):
                self.assertNotIn(
                    name, seen, f"{name!r} binds both {seen.get(name)} and {role}"
                )
                seen[name] = role

    def test_an_unknown_role_is_refused_rather_than_guessed(self):
        with self.assertRaises(KeyError):
            bot_channels.setting_key("moderation")

    def test_every_role_has_somewhere_to_be_stored(self):
        keys = {bot_channels.setting_key(role) for role in bot_channels.ROLES}
        self.assertEqual(len(keys), len(bot_channels.ROLES), "two roles share a key")

    def test_a_decorated_channel_name_still_binds(self):
        """The home server's own channels are decorated, so exact matches are not enough."""
        channels = [_Channel(11, "💬・bot-cmd"), _Channel(22, "🤖🦊 bot-error"), _Channel(33, "📋 logs")]
        self.assertEqual(bot_channels.pick(channels, "commands"), 11)
        self.assertEqual(bot_channels.pick(channels, "error"), 22)
        self.assertEqual(bot_channels.pick(channels, "log"), 33)

    def test_an_exact_name_beats_a_decorated_one(self):
        self.assertEqual(
            bot_channels.pick([_Channel(11, "💬・update"), _Channel(22, "updates")], "update"),
            22,
        )

    def test_a_decorated_test_channel_binds(self):
        """What a test ground actually gets called: ``🧪・test-ground``."""
        self.assertEqual(
            bot_channels.pick([_Channel(11, "🧪・test-ground")], "test"), 11
        )
        self.assertEqual(bot_channels.pick([_Channel(11, "beta-testing")], "test"), 11)

    def test_a_decorated_name_cannot_answer_for_another_role(self):
        """Stripping decoration must not let one channel satisfy two roles."""
        channels = [_Channel(11, "💬・logs"), _Channel(22, "💬・bug-report")]
        self.assertIsNone(bot_channels.pick(channels, "update"))
        self.assertIsNone(bot_channels.pick(channels, "error"))
        self.assertEqual(bot_channels.pick(channels, "bug"), 22)


class NotifyRole(unittest.TestCase):
    """Who a release note tags. A server added later used to be tagged with nothing."""

    def test_the_server_that_always_got_the_ping_keeps_exactly_that_role(self):
        guild = _RoleGuild([_Role(1, "@everyone", default=True), _Role(5, "Dev"), _Role(9, "Admin")])
        self.assertEqual(bot_channels.notify_role(guild, 5).id, 5)

    def test_a_server_without_that_role_is_tagged_by_name_instead(self):
        guild = _RoleGuild([_Role(1, "@everyone", default=True), _Role(7, "Dev")])
        self.assertEqual(bot_channels.notify_role(guild, 999).id, 7)

    def test_a_server_with_no_such_role_gets_a_note_with_no_mention(self):
        guild = _RoleGuild([_Role(1, "@everyone", default=True), _Role(7, "Friends")])
        self.assertIsNone(bot_channels.notify_role(guild, 999))

    def test_the_highest_matching_role_is_the_one_tagged(self):
        guild = _RoleGuild([_Role(3, "dev"), _Role(8, "Dev")])
        self.assertEqual(bot_channels.notify_role(guild, None).id, 8)


class TheOperatorRole(unittest.TestCase):
    """Who counts as an operator of *this* bot, which is not the same question as
    who should be told about a release. A ping may fall back to whatever a server
    calls its staff; a gate on reading who asked an anonymous question may not."""

    def test_the_role_the_bot_has_always_pinged_is_the_operator_role(self):
        guild = _RoleGuild([_Role(1, "@everyone", default=True), _Role(5, "Staff")])
        guild.roles.append(_Role(bot_channels.DEV_ROLE_ID, "Renamed"))
        self.assertEqual(bot_channels.dev_role(guild).id, bot_channels.DEV_ROLE_ID)

    def test_a_server_that_renamed_it_is_still_asked_by_name(self):
        guild = _RoleGuild([_Role(1, "@everyone", default=True), _Role(7, "bot-dev")])
        self.assertEqual(bot_channels.dev_role(guild).id, 7)

    def test_the_highest_holder_of_the_name_is_the_one(self):
        guild = _RoleGuild([_Role(3, "dev"), _Role(8, "Dev")])
        self.assertEqual(bot_channels.dev_role(guild).id, 8)

    def test_a_server_without_the_role_has_no_operator_rather_than_a_guess(self):
        """The whole reason this is not :func:`notify_role`: falling back the way a
        ping does would make every administrator of every server an operator of
        one, which is the leak this gate exists to prevent."""
        guild = _RoleGuild([_Role(1, "@everyone", default=True), _Role(9, "Admin")])
        self.assertIsNone(bot_channels.dev_role(guild))
        self.assertNotIn("admin", bot_channels.DEV_ROLE_NAMES)

    def test_everyone_is_never_an_operator(self):
        guild = _RoleGuild([_Role(1, "dev", default=True)])
        self.assertIsNone(bot_channels.dev_role(guild))

    def test_a_dm_has_no_roles_to_read(self):
        self.assertIsNone(bot_channels.dev_role(None))


class WhichServersAreTheBotsOwn(unittest.TestCase):
    """There are two, and a gate that names one refuses in the other.

    ``BOT_SERVER_GUILD_ID`` names the server the bot's own output lands in today;
    the legacy update channel lives in the server its release notes were hardcoded
    to *first*, which is where the Dev role the ping has always tagged was created.
    The operator is in both, so "only in the bot's own server" has to mean both —
    and this was wrong in the first version of that gate, which refused the command
    in the server its owner was sitting in.
    """

    def test_the_server_the_bot_reports_from_is_its_own(self):
        guild = _ChannelGuild(bot_channels.bot_server_guild_id(), [])
        self.assertTrue(bot_channels.is_own_server(guild))

    def test_the_server_holding_the_legacy_update_channel_is_its_own_too(self):
        legacy = _Channel(bot_channels.LEGACY_UPDATE_CHANNEL_ID, "meow-bot-update")
        guild = _ChannelGuild(4242, [legacy])
        self.assertTrue(bot_channels.is_own_server(guild))

    def test_a_server_with_neither_id_is_one_the_bot_is_a_visitor_in(self):
        """Whatever its channels are called: a name is not an id, and 9999 is not
        either server."""
        guild = _ChannelGuild(9999, [_Channel(7000, "meow-bot-update")])
        self.assertFalse(bot_channels.is_own_server(guild))

    def test_a_dm_is_not_a_server_at_all(self):
        self.assertFalse(bot_channels.is_own_server(None))

    def test_a_guild_with_no_channel_lookup_is_not_assumed_to_be_one(self):
        self.assertFalse(bot_channels.is_own_server(_RoleGuild([])))


class TheOperatorIdHasOneHome(unittest.TestCase):
    """Two ids decide who operates this bot and where they do it: the Dev role a
    release note pings, and the legacy update channel that tells the server its
    notes were hardcoded to from every other one. Each is read by more than one
    feature — the ping, the release-note channel and the gate on the operator's
    side of ``/qna`` — so a second copy of either is a rename away from two
    features disagreeing about who is an operator and where they are.
    """

    #: Files that are allowed to hold the digits themselves: the module that owns
    #: the question. Everything else, including this test, says the name instead.
    HOME = Path("utils") / "bot_channels.py"

    def _files_holding(self, needle: str) -> list[str]:
        """Every hand-written Python file that contains *needle*, except the owner's."""
        root = Path(__file__).resolve().parent.parent
        home = (root / self.HOME).resolve()
        skip = {".venv", "deleted_features", "node_modules", "__pycache__"}
        found = []
        for path in root.rglob("*.py"):
            if set(path.parts) & skip or path.name.startswith("test_"):
                continue
            if path.resolve() == home:
                continue
            if needle in path.read_text(encoding="utf-8", errors="ignore"):
                found.append(str(path.relative_to(root)))
        return found

    def test_the_dev_role_id_is_written_down_once(self):
        self.assertTrue(bot_channels.DEV_ROLE_ID, "the module declares no Dev role")
        strays = self._files_holding(str(bot_channels.DEV_ROLE_ID))
        self.assertEqual(strays, [], f"the Dev role id is copied into {strays}")

    def test_the_legacy_update_channel_id_is_written_down_once(self):
        strays = self._files_holding(str(bot_channels.LEGACY_UPDATE_CHANNEL_ID))
        self.assertEqual(strays, [], f"the legacy update channel id is copied into {strays}")

    def test_bot_py_reads_both_rather_than_writing_them_down_again(self):
        source = BOT_PY.read_text(encoding="utf-8")
        self.assertIn("bot_channels.DEV_ROLE_ID", source)
        self.assertIn("bot_channels.LEGACY_UPDATE_CHANNEL_ID", source)


class Binding(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("channels.db")
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def test_a_new_server_is_bound_by_name_once(self):
        guild = _Guild(NEW_SERVER, ["update", "bot-error", "bug"])
        first = await bot_channels.ensure_bound(guild)
        self.assertEqual(
            first, {"update": 7000, "error": 7001, "bug": 7002}
        )
        # Every later boot is a read: nothing is re-bound, re-named or moved.
        self.assertEqual(await bot_channels.ensure_bound(guild), {})

    async def test_what_was_bound_is_still_there_next_boot(self):
        guild = _Guild(NEW_SERVER, ["update", "bot-error", "bug"])
        await bot_channels.ensure_bound(guild)
        self.assertEqual(await bot_channels.bound(NEW_SERVER, "error"), 7001)
        self.assertEqual((await bot_channels.all_bound(NEW_SERVER))["bug"], 7002)

    async def test_a_role_a_server_already_set_is_left_alone(self):
        guild = _Guild(NEW_SERVER, ["logs", "update"])
        await db.set_setting(NEW_SERVER, "log_channel_id", 999)
        bound = await bot_channels.ensure_bound(guild)
        self.assertNotIn("log", bound)
        self.assertEqual(await bot_channels.bound(NEW_SERVER, "log"), 999)

    async def test_a_server_with_a_bot_cmd_channel_binds_it_for_level_cards(self):
        """Leveling already read this setting; nothing ever wrote it."""
        guild = _Guild(NEW_SERVER, ["bot-cmd"])
        bound = await bot_channels.ensure_bound(guild)
        self.assertEqual(bound, {"commands": 7000})
        self.assertEqual(await bot_channels.bound(NEW_SERVER, "commands"), 7000)

    async def test_the_level_card_setting_is_the_one_the_cog_already_reads(self):
        self.assertEqual(
            bot_channels.setting_key("commands"),
            "bot_cmd_channel_id",
            "a second key would leave leveling reading an empty one forever",
        )
        await db.set_setting(NEW_SERVER, "bot_cmd_channel_id", 4321)
        self.assertEqual(await bot_channels.bound(NEW_SERVER, "commands"), 4321)

    async def test_a_role_with_no_matching_channel_is_simply_unset(self):
        guild = _Guild(NEW_SERVER, ["update"])
        await bot_channels.ensure_bound(guild)
        self.assertIsNone(await bot_channels.bound(NEW_SERVER, "bug"))
        self.assertIsNone(await bot_channels.bound(NEW_SERVER, "error"))

    async def test_an_id_stored_as_a_string_still_reads_as_a_channel(self):
        await db.set_setting(NEW_SERVER, "bug_channel_id", "1550231115992928287")
        self.assertEqual(
            await bot_channels.bound(NEW_SERVER, "bug"), 1550231115992928287
        )

    async def test_a_setting_that_is_not_a_channel_reads_as_unconfigured(self):
        for junk in ("not-a-channel", "", 0, None, True):
            await db.set_setting(NEW_SERVER, "bug_channel_id", junk)
            self.assertIsNone(
                await bot_channels.bound(NEW_SERVER, "bug"),
                f"{junk!r} was taken for a channel",
            )

    async def test_a_guild_that_does_not_exist_yet_is_not_an_error(self):
        self.assertIsNone(await bot_channels.bound(999999, "update"))
        self.assertIsNone(await bot_channels.bound(None, "update"))


class AnnouncedStatePerServer(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """One recorded hash for every server is how a note goes missing."""

    async def asyncSetUp(self):
        self.use_temp_database("version.db")
        await db.setup()
        self.conn = db.connect()
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

    async def asyncTearDown(self):
        self.conn.close()
        self.release_temp_database()

    def test_each_server_keeps_its_own_record(self):
        version.set_last_hash(self.conn, "hash-a", NEW_SERVER)
        self.assertEqual(version.get_last_hash(self.conn, NEW_SERVER), "hash-a")
        self.assertEqual(version.get_last_hash(self.conn, HOME_SERVER), "")

    def test_a_server_that_has_never_been_told_still_gets_its_first_note(self):
        version.set_last_hash(self.conn, "hash-a", HOME_SERVER)
        self.assertEqual(
            version.get_last_hash(self.conn, NEW_SERVER),
            "",
            "a new server inherited another server's record and would never be "
            "told anything",
        )

    def test_the_legacy_row_is_left_for_the_legacy_scope(self):
        version.set_last_hash(self.conn, "old")
        self.assertEqual(version.get_last_hash(self.conn), "old")
        self.assertEqual(version.get_last_hash(self.conn, NEW_SERVER), "")

    def test_the_legacy_row_moves_to_the_server_it_describes(self):
        version.set_last_hash(self.conn, "old")
        version.migrate_to_per_guild(self.conn, HOME_SERVER)
        self.assertEqual(version.get_last_hash(self.conn, HOME_SERVER), "old")

    def test_only_one_server_inherits_the_legacy_row(self):
        version.set_last_hash(self.conn, "old")
        version.migrate_to_per_guild(self.conn, HOME_SERVER)
        version.migrate_to_per_guild(self.conn, NEW_SERVER)
        self.assertEqual(version.get_last_hash(self.conn, NEW_SERVER), "")

    def test_migrating_never_overwrites_a_server_that_already_has_a_record(self):
        version.set_last_hash(self.conn, "old")
        version.set_last_hash(self.conn, "fresh", HOME_SERVER)
        version.migrate_to_per_guild(self.conn, HOME_SERVER)
        self.assertEqual(version.get_last_hash(self.conn, HOME_SERVER), "fresh")

    def test_there_is_nothing_to_migrate_on_a_fresh_database(self):
        version.migrate_to_per_guild(self.conn, HOME_SERVER)
        self.assertEqual(version.get_last_hash(self.conn, HOME_SERVER), "")


class _ChannelSink:
    def __init__(self):
        self.messages = []

    async def send(self, **kwargs):
        self.messages.append(kwargs)
        return self


class _DeployChannel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.mention = f"<#{cid}>"
        self.sink = _ChannelSink()

    async def send(self, **kwargs):
        return await self.sink.send(**kwargs)


class _DeployRole:
    def __init__(self, rid, name):
        self.id = rid
        self.name = name

    def is_default(self):
        return self.name == "@everyone"


class _DeployGuild:
    def __init__(self, gid, name, channels, roles=()):
        self.id = gid
        self.name = name
        self.text_channels = list(channels)
        self.roles = list(roles)

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)


def _drop(guilds, cid):
    """Make a channel unreachable, the way a deleted one looks."""
    for guild in guilds:
        guild.text_channels = [c for c in guild.text_channels if c.id != cid]


class TheDigestReachesEveryServer(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """Driven through the real announcement, not a copy of its logic."""

    async def asyncSetUp(self):
        self.use_temp_database("announce.db")
        await db.setup()
        conn = db.connect()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()

        self.home_update = _DeployChannel(bot_module.LEGACY_UPDATE_CHANNEL_ID, "meow-bot-update")
        self.new_update = _DeployChannel(1550230595085275247, "update")
        self.home = _DeployGuild(
            HOME_SERVER,
            "Meow",
            [self.home_update],
            roles=[_DeployRole(bot_module.DEV_ROLE_ID, "Dev")],
        )
        # The second server's own dev role, under a different id — the real
        # situation, and the one that used to arrive with no mention in it.
        self.new_dev = _DeployRole(1550227368889684357, "Dev")
        self.new = _DeployGuild(
            NEW_SERVER, "Development", [self.new_update], roles=[self.new_dev]
        )
        self.bot = mock.Mock(guilds=[self.home, self.new], user=None)

    async def asyncTearDown(self):
        self.release_temp_database()

    async def _boot(self):
        """Exactly what on_ready does, in the order it does it.

        The quiet window is held open: with the clock left to itself a boot
        outside 03:33–05:14 IST would post nothing at all and every assertion
        below would fail for a reason that has nothing to do with channels.
        """
        for guild in self.bot.guilds:
            await bot_channels.ensure_bound(guild)
        with mock.patch.object(bot_module, "bot", self.bot), mock.patch.object(
            version, "window_is_open", return_value=True
        ):
            await bot_module._announce_release_notes()

    async def test_each_server_gets_the_note_in_its_own_channel(self):
        await self._boot()
        self.assertEqual(len(self.home_update.sink.messages), 1)
        self.assertEqual(len(self.new_update.sink.messages), 1)
        for message in (self.home_update.sink.messages[0], self.new_update.sink.messages[0]):
            self.assertIsNotNone(message["embed"])
            self.assertEqual(message["embed"].title, "🆕 Bot Updated — Meow")

    async def test_the_ping_is_the_right_role_for_each_server(self):
        await self._boot()
        self.assertEqual(
            self.home_update.sink.messages[0]["content"],
            f"<@&{bot_module.DEV_ROLE_ID}>",
            "the server that always got the ping keeps exactly that role",
        )
        self.assertEqual(
            self.new_update.sink.messages[0]["content"],
            f"<@&{self.new_dev.id}>",
            "another server's role id would be an unreadable mention there",
        )

    async def test_a_server_with_no_dev_role_gets_a_note_with_no_mention(self):
        self.new.roles = []
        await self._boot()
        self.assertIsNone(self.new_update.sink.messages[0]["content"])

    async def test_a_second_boot_announces_nothing_at_all(self):
        await self._boot()
        await self._boot()
        self.assertEqual(len(self.home_update.sink.messages), 1)
        self.assertEqual(len(self.new_update.sink.messages), 1)

    async def test_one_dead_channel_does_not_drag_the_other_server_along(self):
        """The whole reason each server keeps its own record."""
        await self._boot()
        # A later deploy, and this time only the new server's channel is gone.
        conn = db.connect()
        version.set_last_hash(conn, "previous", HOME_SERVER)
        version.set_last_hash(conn, "previous", NEW_SERVER)
        conn.close()
        _drop([self.new], self.new_update.id)

        await self._boot()

        self.assertEqual(
            len(self.home_update.sink.messages), 2, "the readable server was told"
        )
        self.assertEqual(len(self.new_update.sink.messages), 1, "nothing could be sent")
        # And it is still owed, rather than consumed by a send that never happened.
        conn = db.connect()
        try:
            owed = version.get_last_hash(conn, NEW_SERVER)
        finally:
            conn.close()
        self.assertEqual(
            owed,
            "previous",
            "an unreachable channel consumed the note it never delivered",
        )

    async def test_a_send_discord_refuses_is_still_owed_next_boot(self):
        await self._boot()
        conn = db.connect()
        version.set_last_hash(conn, "previous", HOME_SERVER)
        version.set_last_hash(conn, "previous", NEW_SERVER)
        conn.close()

        async def refused(**kwargs):
            raise RuntimeError("403 Forbidden (Missing Permissions)")

        self.new_update.send = refused
        await self._boot()

        self.assertEqual(len(self.home_update.sink.messages), 2)
        conn = db.connect()
        try:
            owed = version.get_last_hash(conn, NEW_SERVER)
        finally:
            conn.close()
        self.assertEqual(
            owed,
            "previous",
            "recording a note Discord refused is how an update goes missing forever",
        )

    async def test_a_server_that_bound_nothing_is_left_alone(self):
        quiet = _DeployGuild(4242, "Someone Else", [])
        self.bot.guilds = [self.home, quiet]
        await self._boot()
        self.assertEqual(len(self.home_update.sink.messages), 1)


class TheBotUsesThem(unittest.TestCase):
    """The wiring, which no unit test above would notice going missing."""

    def setUp(self):
        self.bot_src = BOT_PY.read_text(errors="replace")
        self.help_src = HELP_PY.read_text(errors="replace")

    def test_the_digest_asks_each_server_where_its_notes_go(self):
        self.assertIn('bot_channels.bound(guild.id, "update")', self.bot_src)

    def test_the_digest_still_names_one_channel_for_nobody(self):
        self.assertNotIn(
            "bot.get_channel(UPDATE_CHANNEL_ID)",
            self.bot_src,
            "the digest is addressing one hardcoded channel again",
        )

    def test_each_server_records_being_told_separately(self):
        self.assertIn(
            "version.set_last_hash(conn, current_hash, guild.id)", self.bot_src
        )

    def test_the_dev_role_is_pinged_only_where_that_role_exists(self):
        self.assertIn("bot_channels.notify_role(guild, DEV_ROLE_ID)", self.bot_src)

    def test_every_server_is_bound_on_boot(self):
        self.assertIn("await bot_channels.ensure_bound(guild)", self.bot_src)

    def test_a_failed_command_reaches_that_servers_own_channel(self):
        self.assertIn("await _report_error_to_channel(interaction, error)", self.bot_src)
        self.assertIn('bot_channels.bound(guild.id, "error")', self.bot_src)

    def test_the_help_panel_carries_the_bug_button(self):
        self.assertIn("row.add_item(BugReportButton())", self.help_src)

    def test_the_bug_report_costs_no_command_slot(self):
        """The bot is at Discord's 100-command ceiling; this must not be a command."""
        self.assertNotIn('name="bug"', self.help_src)
        self.assertIn("discord.ui.Modal", self.help_src)


if __name__ == "__main__":
    unittest.main()
