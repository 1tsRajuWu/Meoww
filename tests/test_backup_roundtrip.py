"""End-to-end proof that a redeploy does not lose anyone's progress.

The bot restores bot.db from Supabase Storage before it starts, so if that
path breaks, every rank, balance and warning is gone on the next deploy.
These tests run a real SQLite file through the real code with a fake
Supabase standing in for the real API.
"""
import contextlib
import dataclasses
import datetime
import io
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
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
        cs._restored_from = None
        cs._restored_counts = {}
        cs._restored_at = 0.0
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

        with mock.patch.object(cs, "_storage_list_raw", return_value=fake.list_objects()), \
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

        with mock.patch.object(cs, "_storage_list_raw", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_download", side_effect=lambda n: fake.download(n)):
            result = cs.restore_from_cloud()

        self.assertTrue(result)

    def test_restore_fails_gracefully_on_empty_bucket(self):
        with mock.patch.object(cs, "_storage_list_raw", return_value=[]):
            result = cs.restore_from_cloud()
        self.assertFalse(result)

    def test_restore_skips_corrupted_files(self):
        fake = _FakeSupabase()
        fake.files["bot.db"] = b"not a database"
        with mock.patch.object(cs, "_storage_list_raw", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_download", side_effect=lambda n: fake.download(n)), \
             mock.patch.object(cs.time, "sleep", return_value=None):
            result = cs.restore_from_cloud()
        self.assertFalse(result)
        # Backups exist but none could be used: uploading would replace them.
        self.assertIsNotNone(cs._restore_blocked_reason)

    def test_restore_records_what_it_restored(self):
        fake = _FakeSupabase()
        _make_db(cs.db_path())
        with open(cs.db_path(), "rb") as fh:
            fake.files["bot.db"] = fh.read()
        os.remove(cs.db_path())
        with mock.patch.object(cs, "_storage_list_raw", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_download", side_effect=lambda n: fake.download(n)):
            self.assertTrue(cs.restore_from_cloud())
        self.assertEqual(cs._restored_from, "bot.db")
        self.assertTrue(cs._cloud_has_data)
        self.assertEqual(cs._restored_counts.get("economy"), 1)
        self.assertIsNone(cs._restore_blocked_reason)

    def test_restore_retries_before_giving_up(self):
        """One failed list call must not leave the bot running on an empty DB."""
        fake = _FakeSupabase()
        _make_db(cs.db_path())
        with open(cs.db_path(), "rb") as fh:
            fake.files["bot.db"] = fh.read()
        os.remove(cs.db_path())
        answers = [None, None, fake.list_objects()]

        with mock.patch.object(cs, "_storage_list_raw", side_effect=lambda: answers.pop(0)), \
             mock.patch.object(cs, "_storage_download", side_effect=lambda n, quiet=False: fake.download(n)), \
             mock.patch.object(cs.time, "sleep", return_value=None):
            self.assertTrue(cs.restore_from_cloud())

    def test_a_failed_restore_blocks_uploads(self):
        """If the backup cannot be read, it must never be overwritten."""
        with mock.patch.object(cs, "_storage_list_raw", return_value=None), \
             mock.patch.object(cs.time, "sleep", return_value=None):
            self.assertFalse(cs.restore_from_cloud())
        self.assertIsNotNone(cs._restore_blocked_reason)
        _make_db(cs.db_path())
        fake = _FakeSupabase()
        with mock.patch.object(cs, "_storage_list_raw", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_upload", side_effect=lambda n, d: fake.upload(n, d)):
            self.assertFalse(cs.backup_to_cloud_sync())
        self.assertNotIn("bot.db", fake.files)

    def _seed_progress(self, path: str, last_message_ts: float) -> None:
        """One level row whose watermark is exactly `last_message_ts`."""
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT OR REPLACE INTO user_levels VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (GUILD, MEMBER, SEED_XP, SEED_LEVEL, 100, 0, 0, last_message_ts),
        )
        conn.execute(
            "INSERT OR REPLACE INTO economy VALUES (?, ?, ?, ?, ?, ?, ?)",
            (MEMBER, GUILD, SEED_WALLET, SEED_BANK, None, None, None),
        )
        conn.commit()
        conn.close()

    def _restore_with(self, fake: _FakeSupabase) -> bool:
        with mock.patch.object(cs, "_storage_list_raw", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_download", side_effect=lambda n, quiet=False: fake.download(n)):
            return cs.restore_from_cloud()

    def test_restore_keeps_a_live_database_newer_than_the_backup(self):
        """A reboot must not replace a live database with an older backup.

        Seen live as "level 15, and again 15": the member's XP jumps backwards
        and every level is earned twice, with a card each time — and coins and
        purchases silently go with it.
        """
        fake = _FakeSupabase()
        # The bucket holds yesterday's progress...
        self._seed_progress(cs.db_path(), last_message_ts=time.time() - 86400)
        with open(cs.db_path(), "rb") as fh:
            fake.files["bot.db"] = fh.read()
        # ...while this host kept earning until just now.
        self._seed_progress(cs.db_path(), last_message_ts=time.time())
        self.assertTrue(self._restore_with(fake))
        self.assertEqual(cs._restored_from, "local")
        conn = sqlite3.connect(cs.db_path())
        kept = conn.execute(
            "SELECT last_message_ts FROM user_levels WHERE user_id=?", (MEMBER,)
        ).fetchone()[0]
        conn.close()
        self.assertGreater(kept, time.time() - 3600)

    def test_restore_still_replaces_an_older_live_database(self):
        """The rule cuts one way: an older live file still loses to the backup."""
        fake = _FakeSupabase()
        self._seed_progress(cs.db_path(), last_message_ts=time.time())
        with open(cs.db_path(), "rb") as fh:
            fake.files["bot.db"] = fh.read()
        self._seed_progress(cs.db_path(), last_message_ts=time.time() - 86400)
        self.assertTrue(self._restore_with(fake))
        self.assertEqual(cs._restored_from, "bot.db")
        conn = sqlite3.connect(cs.db_path())
        kept = conn.execute(
            "SELECT last_message_ts FROM user_levels WHERE user_id=?", (MEMBER,)
        ).fetchone()[0]
        conn.close()
        self.assertGreater(kept, time.time() - 3600)

    def test_restore_tie_goes_to_the_backup(self):
        """Equal marks are deterministic: today's behavior, byte for byte."""
        fake = _FakeSupabase()
        _make_db(cs.db_path())
        with open(cs.db_path(), "rb") as fh:
            fake.files["bot.db"] = fh.read()
        self.assertTrue(self._restore_with(fake))
        self.assertEqual(cs._restored_from, "bot.db")

    def test_progress_mark_reads_all_three_axes(self):
        """XP stamps, economy stamps and activity days all move the mark."""
        now = time.time()
        conn = sqlite3.connect(cs.db_path())
        conn.execute(
            "INSERT OR REPLACE INTO economy VALUES (?, ?, ?, ?, ?, ?, ?)",
            (MEMBER, GUILD, 0, 0, None, None,
             datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO activity_messages VALUES (?, ?, ?, ?, ?)",
            (GUILD, MEMBER, 1, time.strftime("%Y-%m-%d", time.gmtime(now - 86400)), 5),
        )
        conn.commit()
        conn.close()
        mark = cs._progress_mark(cs.db_path())
        self.assertGreaterEqual(mark, now - 86400)
        self.assertLessEqual(mark, now + 1)
        self.assertEqual(cs._progress_mark("/nonexistent-dir/no.db"), 0.0)


class DeleteRequestShape(unittest.TestCase):
    """The pruner's DELETE has to be one Supabase actually accepts.

    The in-memory fake bucket accepted anything, so the retention rule passed
    its test for weeks while production deleted nothing: Supabase answers a
    bodiless DELETE that claims to carry JSON with
    ``400 Body cannot be empty when content-type is set to 'application/json'``.
    This pins the request itself, not a fake's opinion of it.
    """

    def _capture(self):
        captured: dict = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            captured["method"] = req.get_method()
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured["data"] = req.data
            captured["url"] = req.full_url
            return FakeResponse()

        return captured, fake_urlopen

    def test_delete_is_not_sent_as_json(self):
        captured, fake_urlopen = self._capture()
        with mock.patch.object(cs, "SUPABASE_URL", "https://test.supabase.co"), \
             mock.patch.object(cs, "SUPABASE_KEY", "test-key"), \
             mock.patch.object(cs.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertTrue(cs._storage_delete("bot-db-old.db"))
        self.assertEqual(captured["method"], "DELETE")
        self.assertNotIn("content-type", captured["headers"])
        self.assertIsNone(captured["data"])
        self.assertTrue(captured["url"].endswith("/bot-db-old.db"))
        self.assertIn("authorization", captured["headers"])

    def test_a_refused_delete_is_reported_not_swallowed(self):
        import urllib.error

        def refuse(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 400, "nope", {}, None)

        with mock.patch.object(cs, "SUPABASE_URL", "https://test.supabase.co"), \
             mock.patch.object(cs, "SUPABASE_KEY", "test-key"), \
             mock.patch.object(cs.urllib.request, "urlopen", side_effect=refuse):
            self.assertFalse(cs._storage_delete("bot-db-old.db"))


class BackupToSupabase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("backup_test.db")
        await db.setup()
        cs._last_sync_hash = None
        cs._restore_blocked_reason = None
        cs._remote_checked = False
        cs._upload_requested = False
        cs._last_upload_ts = 0.0
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

    def test_a_refused_snapshot_does_not_start_the_next_hour(self):
        """A snapshot that was refused must be retried, not silently skipped.

        The interval clock used to move regardless of the answer, so one refused
        upload cost a whole hour of snapshots with nothing in the log to say so —
        "keep the newest 5" quietly became a single rolling file.
        """
        _make_db(cs.db_path())
        cs._remote_checked = True
        cs._cloud_has_data = True
        cs._restored_from = "bot.db"
        cs._last_snapshot_ts = 0
        fake = _FakeSupabase()

        def upload(name, data):
            if name.startswith(cs.SNAPSHOT_PREFIX):
                return False  # quota, a 4xx, a rotated token: any refusal
            return fake.upload(name, data)

        with mock.patch.object(cs, "_storage_list_raw", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_upload", side_effect=upload), \
             mock.patch.object(cs, "_storage_delete", side_effect=lambda n: fake.delete(n)):
            self.assertTrue(cs.backup_to_cloud_sync())

        self.assertIn("bot.db", fake.files, "the primary backup still goes out")
        self.assertEqual(cs._last_snapshot_ts, 0, "a refused snapshot must not start the hour")
        self.assertEqual(
            [n for n in fake.files if n.startswith(cs.SNAPSHOT_PREFIX)], [],
            "and nothing may be claimed that was never written",
        )

    def test_a_request_during_an_upload_is_remembered_not_dropped(self):
        """The dashboard's back-up-now call must survive a colliding upload.

        The guard was a bare boolean read and written without a lock, and a caller
        that arrived mid-upload simply got ``False`` — which is exactly the case
        this call exists for: a restart inside the 25-second window would restore
        an older snapshot and undo the administrator's change.
        """
        cs._upload_requested = False
        cs._upload_lock.acquire()
        try:
            self.assertFalse(cs.backup_to_cloud_sync(), "it cannot upload twice at once")
            self.assertTrue(cs._upload_requested, "but the change still needs pushing")
        finally:
            cs._upload_lock.release()
            cs._upload_requested = False

    def test_backup_uploads_current_file(self):
        _make_db(cs.db_path())
        fake = _FakeSupabase()

        with mock.patch.object(cs, "_storage_list_raw", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_upload", side_effect=lambda n, d: fake.upload(n, d)), \
             mock.patch.object(cs, "_storage_delete", side_effect=lambda n: fake.delete(n)):
            result = cs.backup_to_cloud_sync()

        self.assertTrue(result)
        self.assertIn("bot.db", fake.files)
        self.assertGreater(len(fake.files["bot.db"]), 100)

    def test_backup_skips_when_hash_unchanged(self):
        _make_db(cs.db_path())
        fake = _FakeSupabase()

        with mock.patch.object(cs, "_storage_list_raw", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_upload", side_effect=lambda n, d: fake.upload(n, d)), \
             mock.patch.object(cs, "_storage_delete", side_effect=lambda n: fake.delete(n)):
            cs.backup_to_cloud_sync()  # first upload
            call_count = len(fake.files)
            cs.backup_to_cloud_sync()  # second upload — should be skipped

        # No new files should have been created
        self.assertEqual(len(fake.files), call_count)

    def test_an_empty_backup_is_still_an_answer(self):
        """A server whose backups never held a row must not be blocked forever.

        The restore prefers a full snapshot over an empty one, but if *every*
        backup is empty that is the truth about this server — refusing it left
        uploads permanently blocked, so a brand-new deployment could never save
        anything at all.
        """
        fake = _FakeSupabase()
        with open(cs.db_path(), "rb") as fh:      # valid SQLite, zero rows
            fake.files["bot.db"] = fh.read()
        os.remove(cs.db_path())

        with mock.patch.object(cs, "_storage_list_raw", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_download",
                               side_effect=lambda n, quiet=False: fake.download(n) or b""):
            self.assertTrue(cs.restore_from_cloud(), "an empty backup is still restorable")

        self.assertIsNone(cs._restore_blocked_reason)
        self.assertEqual(cs._restored_from, "bot.db")
        self.assertTrue(self._backup_into(fake), "the server must be able to save again")

    def test_backup_refuses_when_db_looks_empty(self):
        """A near-empty database must not overwrite a real backup.

        This is the case that wiped the live server: an instance that had never
        looked at the bucket wrote its own almost-empty database over the
        backup. The guard has to hold even though nothing called restore.
        """
        cs._remote_checked = False          # nobody consulted the bucket yet
        cs._cloud_has_data = False
        cs._restore_blocked_reason = None
        _make_db(cs.db_path())
        fake = _FakeSupabase()
        fake.files["bot.db"] = b"SQLite format 3\x00" + b"\x00" * 2_000
        before = bytes(fake.files["bot.db"])

        result = self._backup_into(fake)

        self.assertFalse(result)
        self.assertEqual(fake.files["bot.db"], before)

    def test_backup_is_allowed_on_a_genuinely_new_server(self):
        """Nothing to protect means the first backup must go through."""
        cs._remote_checked = False
        cs._cloud_has_data = False
        _make_db(cs.db_path())
        fake = _FakeSupabase()
        self.assertTrue(self._backup_into(fake))
        self.assertIn("bot.db", fake.files)

    def test_backup_refuses_when_the_bucket_cannot_be_listed(self):
        """Being unable to check the bucket is not permission to overwrite it."""
        cs._remote_checked = False
        _make_db(cs.db_path())
        fake = _FakeSupabase()
        with mock.patch.object(cs, "_storage_list_raw", return_value=None), \
             mock.patch.object(cs, "_storage_upload", side_effect=lambda n, d: fake.upload(n, d)):
            self.assertFalse(cs.backup_to_cloud_sync())
        self.assertNotIn("bot.db", fake.files)

    def test_backup_refuses_an_unreadable_database(self):
        """A file that is not SQLite is not an empty server — it is damage."""
        with open(cs.db_path(), "wb") as fh:
            fh.write(b"this is not a database at all" * 100)
        cs._remote_checked = True   # the state a running instance is in
        cs._cloud_has_data = True
        cs._restored_from = "bot.db"
        fake = _FakeSupabase()
        self.assertFalse(self._backup_into(fake))
        self.assertNotIn("bot.db", fake.files)

    def test_a_full_wipe_is_held_back_and_then_accepted(self):
        """Even a database emptied to zero rows is saved once it stays empty.

        Without this, a deliberate reset could never be persisted and the next
        restart would restore the rows the admin just deleted.
        """
        _make_db(cs.db_path())
        conn = sqlite3.connect(cs.db_path())
        conn.execute("DELETE FROM economy")
        conn.execute("DELETE FROM user_levels")
        conn.commit()
        conn.close()
        cs._remote_checked = True   # the state a running instance is in
        cs._remote_checked = True
        cs._cloud_has_data = True
        cs._restored_from = "bot.db"
        cs._restored_counts = {"economy": 10, "user_levels": 10}
        fake = _FakeSupabase()

        outcomes = [self._backup_into(fake) for _ in range(cs.SHRINK_GRACE_TICKS)]
        self.assertEqual(outcomes, [False] * (cs.SHRINK_GRACE_TICKS - 1) + [True])
        self.assertIn("bot.db", fake.files)

    def test_backup_refuses_when_restore_blocked(self):
        cs._restore_blocked_reason = "test block"
        result = cs.backup_to_cloud_sync()
        self.assertFalse(result)

    def _backup_into(self, fake, **patches):
        with mock.patch.object(cs, "_storage_list_raw", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_download", side_effect=lambda n, quiet=False: fake.download(n) or b""), \
             mock.patch.object(cs, "_storage_upload", side_effect=lambda n, d: fake.upload(n, d)), \
             mock.patch.object(cs, "_storage_delete", side_effect=lambda n: fake.delete(n)):
            return cs.backup_to_cloud_sync()

    def test_snapshot_includes_rows_still_in_the_wal(self):
        """The headline bug: a raw file read loses everything in the WAL.

        A row committed into ``bot.db-wal`` is invisible to ``open(path, 'rb')``
        until something checkpoints. That is how a restart "reset" progress:
        the uploaded copy was simply older than the database it came from.
        """
        path = cs.db_path()
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT OR REPLACE INTO economy VALUES (?, ?, ?, ?, ?, ?, ?)",
            (MEMBER, GUILD, 999, 111, None, None, None),
        )
        conn.commit()
        # Deliberately no checkpoint, and the connection stays open.
        fake = _FakeSupabase()
        self.assertTrue(self._backup_into(fake))
        uploaded = fake.files["bot.db"]
        conn.close()

        # The uploaded copy must open on its own and contain the commit.
        restored = os.path.join(tempfile.mkdtemp(prefix="meow-wal-"), "restored.db")
        with open(restored, "wb") as f:
            f.write(uploaded)
        check = sqlite3.connect(restored)
        row = check.execute(
            "SELECT wallet, bank FROM economy WHERE user_id=?", (MEMBER,)
        ).fetchone()
        check.close()
        self.assertIsNotNone(row, "the uploaded backup lost rows that were in the WAL")
        self.assertEqual(row[0], 999)

    def test_stranger_instance_may_not_overwrite_the_backup(self):
        """A second host with its own database must not replace the cloud copy."""
        _make_db(cs.db_path())
        cs._remote_checked = True
        cs._cloud_has_data = True
        cs._restored_from = None  # never restored: not a descendant of the backup
        fake = _FakeSupabase()
        fake.files["bot.db"] = b"SQLite format 3\x00" + b"\x00" * 2_000
        before = bytes(fake.files["bot.db"])
        self.assertFalse(self._backup_into(fake))
        self.assertEqual(fake.files["bot.db"], before)

    def test_descendant_instance_may_write(self):
        _make_db(cs.db_path())
        cs._remote_checked = True   # the state a running instance is in
        cs._cloud_has_data = True
        cs._restored_from = "bot.db"
        cs._restored_counts = {"economy": 1, "user_levels": 1}
        fake = _FakeSupabase()
        self.assertTrue(self._backup_into(fake))
        self.assertIn("bot.db", fake.files)

    def test_wiped_database_is_held_back_then_accepted(self):
        """A collapse is not uploaded immediately, and not blocked forever."""
        _make_db(cs.db_path())
        cs._remote_checked = True   # the state a running instance is in
        cs._remote_checked = True
        cs._cloud_has_data = True
        cs._restored_from = "bot.db"
        cs._restored_counts = {"economy": 10, "user_levels": 10}
        fake = _FakeSupabase()

        # The local rows are gone (someone wiped the file), so the first checks
        # refuse...
        for _ in range(cs.SHRINK_GRACE_TICKS - 1):
            self.assertFalse(self._backup_into(fake))
        # ...and then the settled state is accepted, or no server could ever
        # save again after a deliberate reset.
        self.assertTrue(self._backup_into(fake))

    def test_another_live_instance_blocks_the_upload(self):
        _make_db(cs.db_path())
        cs._remote_checked = True   # the state a running instance is in
        cs._remote_checked = True
        cs._cloud_has_data = True
        cs._restored_from = "bot.db"
        cs._restored_counts = {}
        fake = _FakeSupabase()
        fake.files[cs.LEASE_FILE] = cs._json.dumps(
            {"owner": "other-host", "ts": cs.time.time()}
        ).encode()
        self.assertFalse(self._backup_into(fake))
        self.assertNotIn("bot.db", fake.files)

    def test_a_stale_lease_can_be_taken_over(self):
        _make_db(cs.db_path())
        cs._remote_checked = True   # the state a running instance is in
        cs._remote_checked = True
        cs._cloud_has_data = True
        cs._restored_from = "bot.db"
        cs._restored_counts = {}
        fake = _FakeSupabase()
        fake.files[cs.LEASE_FILE] = cs._json.dumps(
            {"owner": "other-host", "ts": cs.time.time() - cs.LEASE_TTL - 60}
        ).encode()
        self.assertTrue(self._backup_into(fake))
        self.assertIn("bot.db", fake.files)

    def test_backup_prunes_old_snapshots(self):
        _make_db(cs.db_path())
        cs._remote_checked = True
        cs._cloud_has_data = True
        cs._restored_from = "bot.db"
        fake = _FakeSupabase()
        # Pre-fill with too many snapshots
        for i in range(10):
            fake.files[f"bot-db-20260913-{i:06d}.db"] = b"old"

        with mock.patch.object(cs, "_storage_list_raw", return_value=fake.list_objects()), \
             mock.patch.object(cs, "_storage_upload", side_effect=lambda n, d: fake.upload(n, d)), \
             mock.patch.object(cs, "_storage_delete", side_effect=lambda n: fake.delete(n)):
            cs._last_snapshot_ts = 0  # force snapshot
            result = cs.backup_to_cloud_sync()

        self.assertTrue(result)
        # Should have pruned old snapshots (new one + BACKUP_KEEP oldest)
        snaps = [n for n in fake.files if n.startswith("bot-db-")]
        self.assertLessEqual(len(snaps), cs.BACKUP_KEEP + 1)  # +1 for the new snapshot just created


#: The names these cases are written in: this machine, a second one, and the pods
#: the two incidents left behind — a Hugging Face pod a push replaced, and the
#: Render sibling a rolling deploy replaces.
THIS_HOST = "Himanshus-MacBook-Air.local"
OTHER_HOST = "srv-aaaaaaaaaaaaaaaaaaaa-one-1-abc"
DEAD_HOST = "r-1tsrajowo-meow-zxho7yru-d2319-tph82"
SIBLING_POD = "srv-daiqibp594qs739qebc0-hibernate-7999965fd6-qwrf4"
NEW_POD = "srv-daiqibp594qs739qebc0-hibernate-7999965fd6-newpod"


@dataclasses.dataclass(frozen=True)
class Case:
    """One reading of the writer slot, and what a boot does with it.

    ``renew`` is what the *second* look finds, and is left unset for the readings
    a boot decides on the first look.
    """

    what: str
    lease: bytes | None
    #: The name the boot answers with — the card's subject — or ``None``.
    answer: str | None
    #: The phrase that makes the line this answer is reported by.
    line: str
    instance: str = THIS_HOST
    has_data: bool = True
    renew: bytes | None = None


class WhoElseIsRunning(unittest.TestCase):
    """``other_copy_verdict`` — the question the boot asks the same lease.

    The lease stopped two copies fighting over the backup; the gateway has the
    same hazard and no guard at all, because Discord delivers every interaction
    to *every* connected session of one application: one copy answers, the other
    raises ``Interaction has already been acknowledged``, and the interactions
    that reach a session that has gone quiet are answered too late — a member
    sees "the application did not respond". The boot reports the copy the lease
    names. A wrong name here is a false alarm in the operator's channels, so
    anything that is not a second copy must answer ``None`` rather than guess —
    wrong in both directions costs the same thing: a warning nobody reads.

    A name is only a *candidate*, though, which is why what a boot actually asks
    is ``other_copy_verdict``: a claim outlives the process that wrote it by up to
    its whole TTL, so the last thing a replaced container did was leave behind
    both halves of a convincing duplicate — and every answer it gets is printed,
    because a check's silence is indistinguishable from a check that never ran.

    The cases are a table (:meth:`_cases`), because each one is the same question
    asked of different bytes and the answers are three: somebody else is live,
    nobody is, or there is nothing to read. Two laws stand beside it — the watch
    outlasts the tick that renews a claim, and the backup still waits for a
    rolling deploy's previous container.
    """

    #: The phrase that makes each line the answer it is: the three a boot can
    #: print, and the one that stops it printing the warning at all.
    NOBODY = "no other copy of this bot is answering"
    CANNOT_TELL = "cannot be ruled out"
    FOUND = "another copy of this bot is running"
    PREVIOUS = "previous container"

    def setUp(self):
        self._was = (cs._cloud_has_data, cs._remote_checked, cs._INSTANCE_ID)
        cs._remote_checked = True  # the state a running instance is already in

    def tearDown(self):
        cs._cloud_has_data, cs._remote_checked, cs._INSTANCE_ID = self._was

    def _said(
        self,
        lease: bytes | None,
        *,
        renew: bytes | None = None,
        has_data: bool = True,
        instance: str = THIS_HOST,
    ):
        """The boot's answer and the line it printed, with the wait made instant.

        The one fake in this class: the list *is* the bucket and the sleep is time
        passing, so ``renew`` is what the second look finds — the same claim
        written again by a live copy, a claim that has gone stale, or, by default,
        the very bytes already there.
        """
        cs._INSTANCE_ID = instance
        bucket = [lease]
        cs._cloud_has_data = has_data
        said = io.StringIO()
        with mock.patch.object(
            cs, "_storage_download", side_effect=lambda *a, **k: bucket[0]
        ):
            with contextlib.redirect_stdout(said):
                answer = cs.other_copy_verdict(
                    sleep=lambda _seconds: bucket.__setitem__(
                        0, renew if renew is not None else lease
                    )
                )
        return answer, said.getvalue()

    def _lease(self, owner: str, age: float = 0.0) -> bytes:
        return cs._json.dumps({"owner": owner, "ts": cs.time.time() - age}).encode()

    def _cases(self) -> list[Case]:
        """Every reading of the writer slot, and what a boot does with it.

        The renewing cases write the same claim again from the same owner, which
        is what a copy that is running does; the ones that end in *nobody* let it
        go idle, take it away, or never find it at all.
        """
        return [
            Case("no claim at all", b"", None, self.NOBODY),
            Case("this machine's own claim", self._lease(THIS_HOST), None, self.NOBODY),
            Case(
                "a claim left behind by a copy that stopped",
                self._lease(DEAD_HOST, age=cs.LEASE_TTL + 60),
                None,
                self.NOBODY,
            ),
            Case("a record that is not JSON", b"not json", None, self.CANNOT_TELL),
            Case("a record that is not an object", b"[]", None, self.CANNOT_TELL),
            Case("a record with no name in it", b"{}", None, self.CANNOT_TELL),
            Case(
                "a timestamp nobody can parse",
                cs._json.dumps({"owner": "srv-x", "ts": "about an hour ago"}).encode(),
                None,
                self.CANNOT_TELL,
            ),
            Case("a bucket that does not answer", None, None, self.CANNOT_TELL),
            Case(
                "a boot that never asked",
                self._lease("srv-x"),
                None,
                self.CANNOT_TELL,
                has_data=False,
            ),
            Case(
                "a copy that keeps renewing its claim",
                self._lease(OTHER_HOST),
                OTHER_HOST,
                self.FOUND,
                renew=self._lease(OTHER_HOST),
            ),
            Case(
                "a laptop while a deploy runs",
                self._lease(THIS_HOST),
                THIS_HOST,
                self.FOUND,
                instance=OTHER_HOST,
                renew=self._lease(THIS_HOST),
            ),
            Case(
                "a copy that stops while we watch",
                self._lease(OTHER_HOST),
                None,
                self.NOBODY,
                renew=self._lease(OTHER_HOST, age=cs.LEASE_TTL + 60),
            ),
            Case(
                "a slot that goes quiet while we watch",
                self._lease(OTHER_HOST),
                None,
                self.NOBODY,
                renew=b"",
            ),
            Case(
                "this deployment's previous container",
                self._lease(SIBLING_POD),
                None,
                self.PREVIOUS,
                instance=NEW_POD,
            ),
        ]

    def test_every_reading_gets_its_answer_and_the_line_that_says_which(self):
        """The table, driven: what the slot holds, what the boot answers with, and
        which of the three lines the log gets instead of a card.

        One row per reading rather than one test per reading, because every case is
        the same question asked of different bytes — and the answer to a card that
        is not sent has to exist, because a check's silence is indistinguishable
        from a check that never ran. The negative half is asserted too: the line
        for *cannot tell* must never borrow the words of *nobody*.
        """
        for case in self._cases():
            with self.subTest(case=case.what):
                answer, said = self._said(
                    case.lease,
                    renew=case.renew,
                    has_data=case.has_data,
                    instance=case.instance,
                )
                self.assertEqual(answer, case.answer)
                self.assertIn("[INSTANCE]", said, "a boot with no card still speaks")
                self.assertIn(case.line, said.lower())
                if case.line == self.CANNOT_TELL:
                    self.assertNotIn("nothing to report", said.lower())

    def test_the_watch_outlasts_the_tick_that_renews_the_claim(self):
        """A live copy renews its claim on its **backup tick**, not on
        :data:`LEASE_TOUCH_INTERVAL` — that constant is only the floor between two
        refreshes inside one tick, which is what made this arithmetic wrong.

        Getting it wrong is invisible from the outside: a watch shorter than the
        tick reads a copy that is *answering* as a claim left behind and sends no
        card, which is the one thing a boot cannot afford to miss. Observed live —
        a Space container's claim was 114s old when the watch began and was never
        written again inside the 150s window, so a running copy was answered "not
        a copy that is answering".
        """
        self.assertGreater(
            cs.COPY_WATCH_SECONDS,
            cs.SYNC_INTERVAL,
            "a watch shorter than one backup tick cannot see a live copy move",
        )

    def test_the_backup_still_waits_for_the_previous_container(self):
        """The sibling rule is about the gateway only: two writers on one bucket
        is the loss the lease exists to prevent, and the wait costs a deploy
        nothing."""
        cs._INSTANCE_ID = "srv-daiqibp594qs739qebc0-hibernate-7999965fd6-newpod"
        cs._cloud_has_data = True
        sibling = self._lease("srv-daiqibp594qs739qebc0-hibernate-7999965fd6-qwrf4")
        with mock.patch.object(cs, "_storage_download", return_value=sibling):
            self.assertIsNone(
                cs.other_copy_verdict(sleep=lambda _seconds: None), "not a second *copy*"
            )
            self.assertIn(
                "owns the bucket", cs._lease_problem() or "",
                "but the upload still refuses while that container writes",
            )



class _Reply:
    """Just enough of an ``http.client.HTTPResponse`` for the storage helpers."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Bucket:
    """A Supabase Storage stand-in at the ``urlopen`` seam.

    The in-memory fakes the rest of this file uses replace ``_storage_upload``
    and ``_storage_download`` outright, which is the right seam for every
    question about *which* object is written. It is the wrong seam for a
    question about *what the bytes look like on the wire*, because it skips the
    two functions that shape them — so these tests stand one level down, at the
    HTTP call, and keep the objects exactly as Supabase would hold them.
    """

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def urlopen(self, req, timeout=None):
        url = req.full_url
        method = req.get_method()
        if "/object/list/" in url:
            return _Reply(cs._json.dumps([{"name": n} for n in self.objects]).encode())
        name = url.rsplit("/", 1)[-1]
        if method == "PUT":
            self.objects[name] = req.data or b""
            return _Reply(b"{}")
        if method == "DELETE":
            self.objects.pop(name, None)
            return _Reply(b"")
        body = self.objects.get(name)
        if body is None:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        return _Reply(body)

    def patch(self):
        return mock.patch.object(cs.urllib.request, "urlopen", side_effect=self.urlopen)


def _sqlite_bytes() -> bytes:
    """Some bytes that are unmistakably a database and long enough to pack."""
    return b"SQLite format 3\x00" + b"\x00" * 4096


class WhatGoesOverTheWire(unittest.TestCase):
    """The bucket holds gzipped payloads, and every older object still reads.

    ``bot.db`` is uploaded on every change and snapshotted hourly, so it is
    simultaneously the biggest thing this bot sends and the thing it sends most
    often. It compresses to about a third of its size; sending it raw is paying
    three times over for bytes that are already in the file.
    """

    def setUp(self):
        self._orig_url = cs.SUPABASE_URL
        self._orig_key = cs.SUPABASE_KEY
        cs.SUPABASE_URL = "https://test.supabase.co"
        cs.SUPABASE_KEY = "test-key"

    def tearDown(self):
        cs.SUPABASE_URL = self._orig_url
        cs.SUPABASE_KEY = self._orig_key

    def test_a_database_goes_up_gzipped_and_comes_back_whole(self):
        bucket = _Bucket()
        raw = _sqlite_bytes()
        with bucket.patch():
            self.assertTrue(cs._storage_upload("bot.db", raw))
        sent = bucket.objects["bot.db"]
        self.assertTrue(sent.startswith(cs._GZIP_MAGIC), "it left this host uncompressed")
        self.assertLess(len(sent), len(raw))
        with bucket.patch():
            self.assertEqual(cs._storage_download("bot.db"), raw)

    def test_a_small_object_is_not_wrapped(self):
        """The lease is a paragraph of JSON; an envelope would only enlarge it."""
        bucket = _Bucket()
        lease = cs._json.dumps({"owner": "srv-x", "ts": 1700000000}).encode()
        self.assertLess(len(lease), cs.PACK_MIN_BYTES)
        with bucket.patch():
            self.assertTrue(cs._storage_upload(cs.LEASE_FILE, lease))
        self.assertEqual(bucket.objects[cs.LEASE_FILE], lease)

    def test_an_object_written_before_this_change_still_restores(self):
        """The bucket on the other side was filled by the release before this one.

        No migration and no second object name: the reader decides by looking at
        the bytes, so a raw ``bot.db`` is read exactly as it was written.
        """
        bucket = _Bucket()
        raw = _sqlite_bytes()
        bucket.objects["bot.db"] = raw
        with bucket.patch():
            self.assertEqual(cs._storage_download("bot.db"), raw)

    def test_a_payload_that_would_grow_is_sent_as_it_is(self):
        """Random bytes gain the envelope's header and nothing else."""
        payload = os.urandom(64 * 1024)
        self.assertIs(cs._pack_blob(payload), payload)

    def test_a_corrupt_packed_object_is_handed_back_rather_than_raised(self):
        """One unreadable candidate must not fail the whole restore."""
        broken = cs._GZIP_MAGIC + b"this is not a gzip stream"
        self.assertEqual(cs._unpack_blob(broken), broken)
        self.assertEqual(cs._unpack_blob(b""), b"")


class TheBackupRoundTripsPacked(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """End to end through the real helpers and the real HTTP shape."""

    async def asyncSetUp(self):
        self.use_temp_database("packed_test.db")
        await db.setup()
        cs._last_sync_hash = None
        cs._restore_blocked_reason = None
        cs._remote_checked = False
        cs._upload_requested = False
        cs._last_upload_ts = 0.0
        cs._last_snapshot_ts = 0
        cs._cloud_has_data = False
        self._orig_url = cs.SUPABASE_URL
        self._orig_key = cs.SUPABASE_KEY
        cs.SUPABASE_URL = "https://test.supabase.co"
        cs.SUPABASE_KEY = "test-key"
        self.bucket = _Bucket()

    async def asyncTearDown(self):
        cs.SUPABASE_URL = self._orig_url
        cs.SUPABASE_KEY = self._orig_key
        self.release_temp_database()

    def test_the_snapshot_is_compacted_rather_than_copied_page_for_page(self):
        """Deleted rows must stop costing bytes the moment they are deleted.

        The backup API copies the file as it is, free pages included, so a table
        that prunes itself — the market's candles, the queue's finished jobs, the
        fixtures removed at boot — keeps costing its old size in every upload
        forever. This compares the two implementations on one file rather than
        measuring the file, because what is being asserted is that the *payload*
        is compact, whatever the file on disk looks like afterwards.
        """
        fill = "x" * 2000
        conn = sqlite3.connect(cs.db_path())
        for _ in range(200):
            conn.execute(
                "INSERT INTO scheduled_messages (guild_id, payload, status) "
                "VALUES (777001, ?, 'pending')",
                (fill,),
            )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(cs.db_path())
        conn.execute("DELETE FROM scheduled_messages")
        conn.commit()
        conn.close()

        copied = cs._snapshot_bytes_uncompacted(cs.db_path())
        compacted = cs._snapshot_bytes(cs.db_path())
        self.assertGreater(len(copied), 300_000, "the fixture never grew the file")
        self.assertLess(len(compacted) * 2, len(copied), "the pages were copied, not reclaimed")

        temp = f"{cs.db_path()}.audit"
        with open(temp, "wb") as handle:
            handle.write(compacted)
        try:
            check = sqlite3.connect(temp)
            self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM scheduled_messages").fetchone()[0], 0
            )
            check.close()
        finally:
            os.remove(temp)

    def test_the_snapshot_that_ships_is_the_snapshot_that_restores(self):
        _make_db(cs.db_path())
        with self.bucket.patch():
            self.assertTrue(cs.backup_to_cloud_sync())
        self.assertIn("bot.db", self.bucket.objects)
        self.assertTrue(self.bucket.objects["bot.db"].startswith(cs._GZIP_MAGIC))

        # A fresh process, a fresh file: exactly what a redeploy does.
        os.remove(cs.db_path())
        for suffix in ("-wal", "-shm"):
            if os.path.exists(cs.db_path() + suffix):
                os.remove(cs.db_path() + suffix)
        cs._last_sync_hash = None
        cs._restored_from = None
        with self.bucket.patch():
            self.assertTrue(cs.restore_from_cloud())

        conn = sqlite3.connect(cs.db_path())
        row = conn.execute("SELECT wallet, bank FROM economy WHERE user_id=?", (MEMBER,)).fetchone()
        conn.close()
        self.assertEqual(row, (SEED_WALLET, SEED_BANK))
