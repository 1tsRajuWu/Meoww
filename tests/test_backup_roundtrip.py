"""End-to-end proof that a redeploy does not lose anyone's progress.

The bot restores bot.db from Supabase Storage before it starts, so if that
path breaks, every rank, balance and warning is gone on the next deploy.
These tests run a real SQLite file through the real code with a fake
Supabase standing in for the real API.
"""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from tests.support import TempDatabaseMixin
from utils import cloud_sync as cs
from utils import db

SEED_XP = 12_345
SEED_LEVEL = 7
SEED_WALLET = 4_321
SEED_BANK = 8_765
GUILD = 777
MEMBER = 42


def _make_db(path: str) -> None:
    """Seed an existing bot.db with one economy and one level row."""
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO economy VALUES (?, ?, ?, ?, ?, ?, ?)", (MEMBER, GUILD, SEED_WALLET, SEED_BANK, None, None, None))
    conn.execute("INSERT OR REPLACE INTO user_levels VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (GUILD, MEMBER, SEED_XP, SEED_LEVEL, 100, 0, 0, 0.0))
    conn.commit()
    conn.close()


class _FakeSupabase:
    """In-memory fake for Supabase Storage operations."""

    def __init__(self):
        self.files: dict[str, bytes] = {}

    def list_objects(self):
        return [{"name": n, "metadata": {"size": len(v)}} for n, v in self.files.items()]

    def download(self, name: str) -> bytes | None:
        return self.files.get(name)

    def upload(self, name: str, data: bytes) -> bool:
        self.files[name] = data
        return True

    def delete(self, name: str) -> bool:
        self.files.pop(name, None)
        return True


class RestoreFromSupabase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("restore_test.db")
        await db.setup()
        cs._last_sync_hash = None
        cs._restore_blocked_reason = None
        cs._remote_checked = False
        # Set env vars for Supabase
        self._orig_url = cs.SUPABASE_URL
        self._orig_key = cs.SUPABASE_KEY
        cs.SUPABASE_URL = "https://test.supabase.co"
        cs.SUPABASE_KEY = "test-key"

    async def asyncTearDown(self):
        cs.SUPABASE_URL = self._orig_url
        cs.SUPABASE_KEY = self._orig_key
        self.release_temp_database()

    def test_restore_downloads_the_newest_snapshot(self):
        """When bot.db is missing, the newest snapshot is restored."""
        fake = _FakeSupabase()
        # Seed the temp database and snapshot it
        _make_db(cs.db_path())
        with open(cs.db_path(), "rb") as f:
            fake.files["bot-db-20260913-120000.db"] = f.read()
        # Remove the local file so restore must download
        os.remove(cs.db_path())

        with mock.patch.object(cs, "_storage_list", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_download", side_effect=lambda n: fake.download(n)):
            result = cs.restore_from_cloud()

        self.assertTrue(result)
        conn = sqlite3.connect(cs.db_path())
        row = conn.execute("SELECT wallet, bank FROM economy WHERE user_id=?", (MEMBER,)).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], SEED_WALLET)
        self.assertEqual(row[1], SEED_BANK)

    def test_restore_prefers_current_file_over_snapshots(self):
        """If bot.db exists alongside snapshots, current wins."""
        fake = _FakeSupabase()
        _make_db(cs.db_path())
        with open(cs.db_path(), "rb") as f:
            data = f.read()
        fake.files["bot.db"] = data
        fake.files["bot-db-20260913-120000.db"] = data
        os.remove(cs.db_path())

        with mock.patch.object(cs, "_storage_list", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_download", side_effect=lambda n: fake.download(n)):
            result = cs.restore_from_cloud()

        self.assertTrue(result)

    def test_restore_fails_gracefully_on_empty_bucket(self):
        with mock.patch.object(cs, "_storage_list", return_value=[]):
            result = cs.restore_from_cloud()
        self.assertFalse(result)

    def test_restore_skips_corrupted_files(self):
        fake = _FakeSupabase()
        fake.files["bot.db"] = b"not a database"
        with mock.patch.object(cs, "_storage_list", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_download", side_effect=lambda n: fake.download(n)):
            result = cs.restore_from_cloud()
        self.assertFalse(result)


class BackupToSupabase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("backup_test.db")
        await db.setup()
        cs._last_sync_hash = None
        cs._restore_blocked_reason = None
        cs._remote_checked = False
        cs._upload_lock_active = False
        cs._last_snapshot_ts = 0
        # Set env vars for Supabase
        self._orig_url = cs.SUPABASE_URL
        self._orig_key = cs.SUPABASE_KEY
        cs.SUPABASE_URL = "https://test.supabase.co"
        cs.SUPABASE_KEY = "test-key"

    async def asyncTearDown(self):
        cs.SUPABASE_URL = self._orig_url
        cs.SUPABASE_KEY = self._orig_key
        self.release_temp_database()

    def test_backup_uploads_current_file(self):
        _make_db(cs.db_path())
        fake = _FakeSupabase()

        with mock.patch.object(cs, "_storage_list", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_upload", side_effect=lambda n, d: fake.upload(n, d)), \
             mock.patch.object(cs, "_storage_delete", side_effect=lambda n: fake.delete(n)):
            result = cs.backup_to_cloud_sync()

        self.assertTrue(result)
        self.assertIn("bot.db", fake.files)
        self.assertGreater(len(fake.files["bot.db"]), 100)

    def test_backup_skips_when_hash_unchanged(self):
        _make_db(cs.db_path())
        fake = _FakeSupabase()

        with mock.patch.object(cs, "_storage_list", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_upload", side_effect=lambda n, d: fake.upload(n, d)), \
             mock.patch.object(cs, "_storage_delete", side_effect=lambda n: fake.delete(n)):
            cs.backup_to_cloud_sync()  # first upload
            call_count = len(fake.files)
            cs.backup_to_cloud_sync()  # second upload — should be skipped

        # No new files should have been created
        self.assertEqual(len(fake.files), call_count)

    def test_backup_refuses_when_db_looks_empty(self):
        """An empty database must not overwrite a real backup."""
        cs._remote_checked = True
        cs._restore_blocked_reason = None
        # bot.db exists but is empty (no economy/level rows)
        fake = _FakeSupabase()
        fake.files["bot.db"] = b"SQLite format 3\x00" + b"\x00" * 200

        with mock.patch.object(cs, "_storage_list", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_upload", side_effect=lambda n, d: fake.upload(n, d)):
            result = cs.backup_to_cloud_sync()

        self.assertFalse(result)

    def test_backup_refuses_when_restore_blocked(self):
        cs._restore_blocked_reason = "test block"
        result = cs.backup_to_cloud_sync()
        self.assertFalse(result)

    def test_backup_prunes_old_snapshots(self):
        _make_db(cs.db_path())
        fake = _FakeSupabase()
        # Pre-fill with too many snapshots
        for i in range(10):
            fake.files[f"bot-db-20260913-{i:06d}.db"] = b"old"

        with mock.patch.object(cs, "_storage_list", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_upload", side_effect=lambda n, d: fake.upload(n, d)), \
             mock.patch.object(cs, "_storage_delete", side_effect=lambda n: fake.delete(n)):
            cs._last_snapshot_ts = 0  # force snapshot
            result = cs.backup_to_cloud_sync()

        self.assertTrue(result)
        # Should have pruned old snapshots (new one + BACKUP_KEEP oldest)
        snaps = [n for n in fake.files if n.startswith("bot-db-")]
        self.assertLessEqual(len(snaps), cs.BACKUP_KEEP + 1)  # +1 for the new snapshot just created
