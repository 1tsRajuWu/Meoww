"""Tests for the database report — now ``/bot dbstatus``, in ``utils/dbstats``.

The command moved when Discord's hundred top-level commands ran out, and the
report itself moved out of the cog and into a module of its own, because it was
always a fact about a file rather than a server-control action. The tests moved
with it: every one of them now drives ``utils.dbstats`` or ``BotConfig.dbstatus``,
and the last class pins the move itself so it cannot drift back.
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class _BootBot:
    """As much of a bot as ``ServerControl.__init__`` and the module gate touch.

    The cog starts one pass at boot that reads ``bot.guilds`` and waits for the
    gateway. A stand-in without either would make that pass fail in a background
    task, and an error a test did not cause is an error a test run has to explain.
    """

    def __init__(self, *, enabled=True):
        self.guilds = ()
        self._enabled = enabled

    def is_module_enabled(self, _guild_id, _module):
        return self._enabled

    async def wait_until_ready(self):
        return True


class TestDbstatusMath(unittest.TestCase):
    """Verify the database stats logic independent of Discord."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE economy (user_id INTEGER, wallet INTEGER)")
        conn.execute("CREATE TABLE user_levels (user_id INTEGER, xp INTEGER)")
        conn.execute("INSERT INTO economy VALUES (1, 100)")
        conn.execute("INSERT INTO economy VALUES (2, 200)")
        conn.execute("INSERT INTO user_levels VALUES (1, 500)")
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_row_counts_are_accurate(self):
        """Each table's row count matches reality."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        # Fetch all table names first — reusing the same cursor for an inner
        # execute would reset the sqlite_master iterator.
        table_names = [
            name for (name,) in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        tables = {}
        for name in table_names:
            count = cur.execute(f'SELECT COUNT(*) FROM [{name}]').fetchone()[0]
            tables[name] = count
        conn.close()
        self.assertEqual(tables["economy"], 2)
        self.assertIn("user_levels", tables)
        self.assertEqual(tables["user_levels"], 1)

    def test_db_size_is_positive(self):
        """The database file size is reported in KB and is positive."""
        size_kb = os.path.getsize(self.db_path) / 1024
        self.assertGreater(size_kb, 0)

    def test_empty_database_has_zero_rows(self):
        """An empty table reports zero rows."""
        empty = os.path.join(self.tmpdir, "empty.db")
        conn = sqlite3.connect(empty)
        conn.execute("CREATE TABLE foo (id INTEGER)")
        conn.commit()
        conn.close()
        cur = sqlite3.connect(empty).cursor()
        count = cur.execute("SELECT COUNT(*) FROM foo").fetchone()[0]
        cur.close()
        self.assertEqual(count, 0)

    def test_a_table_whose_name_needs_quoting_is_skipped_not_interpolated(self):
        """A row count is not the place to find out what a name means.

        A table name cannot be a bound parameter — ``?`` stands for a value, not
        for a name — so a query that counts one is built by string interpolation.
        ``a"b`` is a legal table name and, interpolated straight in, it closes
        the quoting early; escaping the inner quote fixed the read, and the rule
        now is the shorter one: only names that are *nothing but* letters, digits
        and underscores are counted at all. Every table this bot creates is one
        of those, so a name outside the set belongs to somebody else — and it is
        left out rather than reported as unreadable, which would say a readable
        table is broken.
        """
        from utils.dbstats import local_table_counts

        weird = os.path.join(self.tmpdir, "weird.db")
        conn = sqlite3.connect(weird)
        conn.execute('CREATE TABLE "a""b" (x INTEGER)')
        conn.execute('INSERT INTO "a""b" VALUES (1), (2), (3)')
        conn.execute("CREATE TABLE plain (x INTEGER)")
        conn.execute("INSERT INTO plain VALUES (9)")
        conn.commit()
        conn.close()

        counts, _size_kb, _wal_kb, integrity = local_table_counts(weird)
        self.assertNotIn('a"b', counts)
        self.assertNotIn(-1, counts.values(), f"a table was reported unreadable: {counts}")
        # …and the ordinary table beside it is still counted, so the rule above
        # cannot be satisfied by counting nothing at all.
        self.assertEqual(counts.get("plain"), 1)
        self.assertEqual(integrity, "ok")


class TestCloudSyncStatus(unittest.TestCase):
    """Verify the cloud sync status fields are well-formed."""

    def test_status_fields_exist(self):
        """All expected status fields are importable."""
        from utils.cloud_sync import (
            SUPABASE_URL,
            SUPABASE_KEY,
            BUCKET,
            _last_sync_hash,
            _restore_blocked_reason,
            _remote_checked,
            _storage_list,
        )
        # These should be importable without error
        self.assertIsInstance(SUPABASE_URL, str)
        self.assertIsInstance(SUPABASE_KEY, str)
        self.assertIsInstance(BUCKET, str)

    def test_supabase_configured_check(self):
        """The configured flag is true when both env vars are set."""
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_SERVICE_KEY": "eyJ.test",
        }):
            # Re-import to pick up patched env
            import importlib
            import utils.cloud_sync as cs
            importlib.reload(cs)
            self.assertTrue(bool(cs.SUPABASE_URL and cs.SUPABASE_KEY))

    def test_supabase_not_configured(self):
        """The configured flag is false when env vars are missing."""
        with patch.dict(os.environ, {"SUPABASE_URL": "", "SUPABASE_SERVICE_KEY": ""}):
            import importlib
            import utils.cloud_sync as cs
            importlib.reload(cs)
            self.assertFalse(bool(cs.SUPABASE_URL and cs.SUPABASE_KEY))


class _Avatar:
    url = "https://cdn.example/avatar.png"


class _Me:
    display_avatar = _Avatar()


class _Guild:
    me = _Me()
    id = 777
    name = "Hmm"


class _User:
    display_name = "RAJ"
    display_avatar = _Avatar()


class _Response:
    def __init__(self):
        self.messages = []

    async def defer(self, **kwargs):
        pass

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _Followup:
    def __init__(self):
        self.embeds = []

    async def send(self, **kwargs):
        self.embeds.append(kwargs.get("embed"))


class _FakeInteraction:
    """Just enough of an interaction to drive the real callback."""

    def __init__(self):
        self.response = _Response()
        self.followup = _Followup()
        self.guild = _Guild()
        self.user = _User()
        self.guild_id = 777


class TestDbstatusRunsOffTheEventLoop(unittest.IsolatedAsyncioTestCase):
    """The card talks to Supabase and reads SQLite — not on the event loop.

    Called inline, this held the loop (and with it every other command and the
    gateway heartbeat) for the length of a round trip: measured at 507 ms with a
    stand-in for the network, and the real Supabase has answered in seconds.
    """

    async def asyncSetUp(self):
        import threading  # noqa: F401  (documented in the assertion below)
        self._dir = tempfile.mkdtemp()
        from utils import db
        self._original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._dir, "card.db")
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("CREATE TABLE economy (user_id INTEGER, wallet INTEGER)")
        conn.execute("INSERT INTO economy VALUES (1, 100)")
        conn.commit()
        conn.close()

    async def asyncTearDown(self):
        import shutil
        from utils import db
        db.DB_PATH = self._original_db_path
        shutil.rmtree(self._dir, ignore_errors=True)

    async def test_the_decision_is_computed_in_a_worker_thread(self):
        import threading
        from unittest.mock import patch
        from cogs.bot_config import BotConfig
        from utils import cloud_sync as cs

        seen = {}

        def fake_refusal(decide=True):
            seen["thread"] = threading.current_thread()
            seen["decide"] = decide
            return "blocked for the test"

        interaction = _FakeInteraction()
        cog = BotConfig(bot=_BootBot())
        with patch.object(cs, "upload_refusal_reason", side_effect=fake_refusal), \
             patch.object(cs, "_storage_list", return_value=[]):
            await BotConfig.dbstatus.callback(cog, interaction)

        self.assertIn("thread", seen, "the card never asked whether uploads are allowed")
        self.assertNotEqual(
            seen["thread"], threading.main_thread(),
            "the refusal was computed on the event loop, which blocks every command",
        )
        self.assertFalse(seen["decide"], "reporting must not spend a shrink grace tick")
        embed = interaction.followup.embeds[-1]
        field = next(f for f in embed.fields if "Sync Status" in f.name)
        self.assertIn("blocked for the test", field.value)

    async def test_the_card_still_renders_without_supabase_configured(self):
        """The import of asyncio must not live inside the Supabase branch."""
        from unittest.mock import patch
        from cogs.bot_config import BotConfig
        from utils import cloud_sync as cs

        interaction = _FakeInteraction()
        cog = BotConfig(bot=_BootBot())
        with patch.object(cs, "SUPABASE_URL", ""), \
             patch.object(cs, "SUPABASE_KEY", ""):
            await BotConfig.dbstatus.callback(cog, interaction)
        embed = interaction.followup.embeds[-1]
        field = next(f for f in embed.fields if "Supabase Backup" in f.name)
        self.assertIn("Not configured", field.value)


class _Group:
    def __init__(self, name):
        self.name = name


class _Command:
    """A slash command as the gateway hands it over.

    A subcommand carries its group as ``root_parent`` and reports its own name
    in ``name``, which is why matching a group name against ``name`` misses.
    """

    def __init__(self, name, group=None):
        self.name = name
        self.root_parent = _Group(group) if group else None


class TestServerControlModuleGate(unittest.IsolatedAsyncioTestCase):
    """/defaultrole must not inherit the Moderation module's off switch."""

    def _cog(self, enabled):
        from cogs.server_control import ServerControl

        return ServerControl(bot=_BootBot(enabled=enabled))

    async def _check(self, enabled, command):
        interaction = _FakeInteraction()
        interaction.command = command
        return await self._cog(enabled).interaction_check(interaction)

    async def test_a_group_command_is_matched_by_its_group_name(self):
        self.assertTrue(
            await self._check(False, _Command("set", group="defaultrole")),
            "/defaultrole set was refused because the gate read the subcommand name",
        )

    async def test_a_flat_bypass_command_still_bypasses(self):
        self.assertTrue(await self._check(False, _Command("serverinfo")))

    async def test_a_gated_command_is_still_refused_while_the_module_is_off(self):
        self.assertFalse(await self._check(False, _Command("lockdown")))
        self.assertTrue(await self._check(True, _Command("lockdown")))

    async def test_the_refusal_tells_the_user_why(self):
        interaction = _FakeInteraction()
        interaction.command = _Command("lockdown")
        await self._cog(False).interaction_check(interaction)
        self.assertTrue(interaction.response.messages, "the refusal said nothing")
        self.assertIn("Moderation", interaction.response.messages[-1]["content"])


class TestDefaultRoleWarnsWhenItCannotWork(unittest.TestCase):
    """Member events need the Members intent; with it off the feature is a no-op.

    The setting would still save, the command would still answer "new members
    receive this role", and nobody would ever receive it — silent in every place
    an administrator looks.
    """

    def test_the_warning_names_the_intent_and_how_to_enable_it(self):
        from types import SimpleNamespace
        from cogs.server_control import _members_intent_warning

        off = _members_intent_warning(
            SimpleNamespace(intents=SimpleNamespace(members=False))
        )
        self.assertIn("Members intent", off)
        self.assertIn("ENABLE_MEMBERS_INTENT", off)
        self.assertEqual(
            _members_intent_warning(SimpleNamespace(intents=SimpleNamespace(members=True))),
            "",
            "a correctly configured bot must not nag",
        )
        # A bot with no intents attribute at all warns instead of raising.
        self.assertIn("Members intent", _members_intent_warning(SimpleNamespace()))

    def test_the_commands_that_talk_about_the_role_say_it(self):
        import inspect
        from cogs.server_control import ServerControl

        for name in ("defaultrole_set", "defaultrole_view"):
            source = inspect.getsource(getattr(ServerControl, name).callback)
            self.assertIn("_members_intent_warning", source, name)


class TestTheMoveToTheBotGroup(unittest.TestCase):
    """``/dbstatus`` is ``/bot dbstatus`` now, and the record of why is here.

    Discord allows a hundred top-level commands and this tree was **at** it, so
    the command that made room for ``/decat`` was the one that only *reports*
    something. It is flat rather than ``/bot db health`` on purpose: a nested group
    is a shape ``utils.permissions.walk`` cannot see, so its audiences would escape
    the one audit that keeps this tree's permissions honest.
    """

    def test_command_in_access_table(self):
        from utils.permissions import COMMAND_ACCESS
        self.assertIn("bot dbstatus", COMMAND_ACCESS)
        self.assertEqual(COMMAND_ACCESS["bot dbstatus"], "administrator")
        self.assertNotIn(
            "dbstatus", COMMAND_ACCESS,
            "the old flat name is back in the audience table, which means the tree "
            "has a command the audit cannot see",
        )

    def test_it_is_no_longer_its_own_root(self):
        """The whole point of the move: one fewer top-level command."""
        from utils.features import ALWAYS_ALLOWED_COMMANDS, MODULE_COMMAND_MAP
        self.assertNotIn("dbstatus", ALWAYS_ALLOWED_COMMANDS)
        self.assertNotIn("dbstatus", MODULE_COMMAND_MAP)
        # `/bot` is the group that stays reachable when a module is off, and the
        # report lives under it, so nothing is lost by leaving the flat list.
        self.assertIn("bot", ALWAYS_ALLOWED_COMMANDS)

    def test_command_in_system_feature(self):
        from utils.features import FEATURES
        system = next(f for f in FEATURES if f.id == "system")
        self.assertIn("/bot dbstatus", system.commands)
        self.assertNotIn("/dbstatus", system.commands)

    def test_the_report_is_readable_without_discord(self):
        """The half that made the move worth doing: a fact about a file, testable."""
        from utils import dbstats
        import inspect

        source = inspect.getsource(dbstats.collect)
        self.assertIn("to_thread", source, "the blocking halves run on the loop")
        self.assertIn("def embed", inspect.getsource(dbstats))


if __name__ == "__main__":
    unittest.main()
