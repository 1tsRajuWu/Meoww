"""Tests for the /dbstatus command."""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock


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
    async def defer(self, **kwargs):
        pass


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
        from cogs.server_control import ServerControl
        from utils import cloud_sync as cs

        seen = {}

        def fake_refusal(decide=True):
            seen["thread"] = threading.current_thread()
            seen["decide"] = decide
            return "blocked for the test"

        interaction = _FakeInteraction()
        cog = ServerControl(bot=MagicMock())
        with patch.object(cs, "upload_refusal_reason", side_effect=fake_refusal), \
             patch.object(cs, "_storage_list", return_value=[]):
            await ServerControl.dbstatus.callback(cog, interaction)

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
        from cogs.server_control import ServerControl
        from utils import cloud_sync as cs

        interaction = _FakeInteraction()
        cog = ServerControl(bot=MagicMock())
        with patch.object(cs, "SUPABASE_URL", ""), \
             patch.object(cs, "SUPABASE_KEY", ""):
            await ServerControl.dbstatus.callback(cog, interaction)
        embed = interaction.followup.embeds[-1]
        field = next(f for f in embed.fields if "Supabase Backup" in f.name)
        self.assertIn("Not configured", field.value)


class TestPermissionDeclared(unittest.TestCase):
    """The dbstatus command is declared as admin-only."""

    def test_command_in_access_table(self):
        from utils.permissions import COMMAND_ACCESS
        self.assertIn("dbstatus", COMMAND_ACCESS)
        self.assertEqual(COMMAND_ACCESS["dbstatus"], "administrator")

    def test_command_in_always_allowed(self):
        from utils.features import ALWAYS_ALLOWED_COMMANDS
        self.assertIn("dbstatus", ALWAYS_ALLOWED_COMMANDS)

    def test_command_in_system_feature(self):
        from utils.features import FEATURES
        system = next(f for f in FEATURES if f.id == "system")
        self.assertIn("/dbstatus", system.commands)


if __name__ == "__main__":
    unittest.main()
