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


class WhoElseIsRunning(unittest.TestCase):
    """``other_live_copy`` — the question the boot asks the same lease.

    The lease stopped two copies fighting over the backup; the gateway has the
    same hazard and no guard at all, because Discord delivers every interaction
    to *every* connected session of one application: one copy answers, the other
    raises ``Interaction has already been acknowledged``, and the interactions
    that reach a session that has gone quiet are answered too late — a member
    sees "the application did not respond". The boot reports the copy the lease
    names. A wrong name here is a false alarm in the operator's channels, so
    anything that is not a second copy must answer ``None`` rather than guess —
    wrong in both directions costs the same thing: a warning nobody reads.
    """

    def setUp(self):
        self._was = (cs._cloud_has_data, cs._remote_checked, cs._INSTANCE_ID)
        cs._remote_checked = True  # the state a running instance is already in

    def tearDown(self):
        cs._cloud_has_data, cs._remote_checked, cs._INSTANCE_ID = self._was

    def _owner(self, lease: bytes | None, has_data: bool = True):
        cs._cloud_has_data = has_data
        with mock.patch.object(cs, "_storage_download", return_value=lease):
            return cs.other_live_copy()

    def _lease(self, owner: str, age: float = 0.0) -> bytes:
        return cs._json.dumps({"owner": owner, "ts": cs.time.time() - age}).encode()

    def test_a_fresh_lease_names_the_other_copy(self):
        cs._INSTANCE_ID = "Himanshus-MacBook-Air.local"
        self.assertEqual(
            self._owner(self._lease("srv-daiqibp594qs739qebc0-hibernate-7999965fd6-qwrf4")),
            "srv-daiqibp594qs739qebc0-hibernate-7999965fd6-qwrf4",
        )

    def test_the_previous_container_of_this_deployment_is_not_a_second_copy(self):
        """A rolling deploy's own old pod holds the lease for a few seconds.

        Observed live: the container that came up from a push reported "another
        copy of me is online — stop this one" about the pod it had just replaced,
        which is advice to stop the deployment that was just made. Same service
        id, different pod, one deployment.
        """
        cs._INSTANCE_ID = "srv-daiqibp594qs739qebc0-hibernate-7999965fd6-newpod"
        self.assertIsNone(
            self._owner(self._lease("srv-daiqibp594qs739qebc0-hibernate-7999965fd6-qwrf4"))
        )

    def test_a_second_copy_on_another_host_is_reported(self):
        """The case this exists for: a deploy and a laptop, same token."""
        cs._INSTANCE_ID = "Himanshus-MacBook-Air.local"
        self.assertIsNotNone(self._owner(self._lease("srv-aaaaaaaaaaaaaaaaaaaa-other-1-abc")))
        cs._INSTANCE_ID = "srv-aaaaaaaaaaaaaaaaaaaa-one-1-abc"
        self.assertIsNotNone(
            self._owner(self._lease("Himanshus-MacBook-Air.local")),
            "a laptop while a deploy runs is the duplicate that breaks commands",
        )

    def test_a_different_service_running_the_same_token_is_reported(self):
        cs._INSTANCE_ID = "srv-aaaaaaaaaaaaaaaaaaaa-one-1-abc"
        self.assertIsNotNone(self._owner(self._lease("srv-bbbbbbbbbbbbbbbbbbbb-two-1-xyz")))

    def test_our_own_lease_is_not_somebody_else(self):
        self.assertIsNone(self._owner(self._lease(cs._INSTANCE_ID)))

    def test_a_stale_lease_is_not_someone_who_is_running(self):
        self.assertIsNone(self._owner(self._lease("srv-dead", age=cs.LEASE_TTL + 60)))

    def test_an_absent_or_unreadable_lease_is_not_an_owner(self):
        for lease in (None, b"", b"not json", b"{}"):
            with self.subTest(lease=lease):
                self.assertIsNone(self._owner(lease))

    def test_a_lease_with_an_unreadable_timestamp_is_not_an_owner(self):
        """A ``ts`` nobody can parse is not a *stale* lease — that would invite a
        takeover of a bucket whose owner is alive, which is the loss the lease
        exists to prevent."""
        lease = cs._json.dumps({"owner": "srv-x", "ts": "about an hour ago"}).encode()
        self.assertIsNone(self._owner(lease))

    def test_the_name_it_answers_with_is_just_the_name(self):
        """It is quoted in a card an operator reads, not pasted into a sentence."""
        cs._INSTANCE_ID = "Housemate-Mac.local"
        self.assertEqual(self._owner(self._lease("srv-render-abc")), "srv-render-abc")

    def test_the_backup_still_waits_for_the_previous_container(self):
        """The sibling rule is about the gateway only: two writers on one bucket
        is the loss the lease exists to prevent, and the wait costs a deploy
        nothing."""
        cs._INSTANCE_ID = "srv-daiqibp594qs739qebc0-hibernate-7999965fd6-newpod"
        cs._cloud_has_data = True
        sibling = self._lease("srv-daiqibp594qs739qebc0-hibernate-7999965fd6-qwrf4")
        with mock.patch.object(cs, "_storage_download", return_value=sibling):
            self.assertIsNone(cs.other_live_copy(), "not a second *copy*")
            self.assertIn(
                "owns the bucket", cs._lease_problem() or "",
                "but the upload still refuses while that container writes",
            )

    def test_an_empty_bucket_has_nobody_to_name(self):
        lease = cs._json.dumps({"owner": "srv-x", "ts": cs.time.time()}).encode()
        self.assertIsNone(self._owner(lease, has_data=False))


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
