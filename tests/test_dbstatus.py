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
