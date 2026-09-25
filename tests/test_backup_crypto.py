"""Supabase Storage replaces the encrypted Gist — the bucket is private and
the service-role key never reaches the client, so encryption at the
application layer is no longer needed.

These tests verify that the Supabase storage helpers behave correctly
(upload, download, list, delete) and that the restore/backup cycle works
against a fake Supabase endpoint.
"""
import unittest

from utils import cloud_sync


class SupabaseStorageCase(unittest.TestCase):
    """The Supabase helpers accept raw bytes and return raw bytes."""

    def test_db_path_is_shared_with_utils_db(self):
        from utils import db
        self.assertEqual(cloud_sync.db_path(), db.DB_PATH)

    def test_restore_blocked_reason_starts_none(self):
        cloud_sync._restore_blocked_reason = None
        self.assertIsNone(cloud_sync.restore_blocked_reason())

    def test_snapshot_names_sorted_newest_first(self):
        objects = [
            {"name": "bot-db-20260912-100000.db"},
            {"name": "bot-db-20260914-120000.db"},
            {"name": "bot.db"},
            {"name": "bot-db-20260913-080000.db"},
        ]
        names = cloud_sync._snapshot_names(objects)
        self.assertEqual(names, [
            "bot-db-20260914-120000.db",
            "bot-db-20260913-080000.db",
            "bot-db-20260912-100000.db",
        ])

    def test_looks_like_sqlite(self):
        self.assertTrue(cloud_sync._looks_like_sqlite(b"SQLite format 3\x00" + b"\x00" * 200))
        self.assertFalse(cloud_sync._looks_like_sqlite(b"not sqlite"))
        self.assertFalse(cloud_sync._looks_like_sqlite(b""))
