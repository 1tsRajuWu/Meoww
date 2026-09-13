"""End-to-end proof that a redeploy does not lose anyone's progress.

The bot restores bot.db from the cloud Gist before it starts, so if that path
breaks, every rank, balance and warning is gone on the next deploy. These tests
run a real SQLite file through the real code with a fake Gist standing in for
GitHub, covering the encrypted format, the legacy plaintext format, the upgrade
from one to the other, and every way the restore can go wrong."""
import asyncio
import base64
import json
import os
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


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UnreachableGist:
    """A Gist that cannot be read at all — a DNS blip, an expired token."""

    def __init__(self, *args, **kwargs):
        self.files: dict[str, str] = {}
        self.patches = 0

    @property
    def names(self) -> set[str]:
        return set()

    def urlopen(self, req, timeout=None):
        raise OSError("the Gist API is unreachable")


class RejectingGist:
    """A Gist whose writes are always refused (rate limit, read-only token)."""

    def __init__(self, files: dict[str, str] | None = None, public: bool = False):
        self.files = dict(files or {})
        self.public = public
        self.patches = 0

    @property
    def names(self) -> set[str]:
        return set(self.files)

    def urlopen(self, req, timeout=None):
        method = (getattr(req, "method", None) or "GET").upper()
        if method == "PATCH":
            self.patches += 1
            return FakeResponse(b'{"message":"rate limited"}', status=429)
        body = json.dumps({
            "public": self.public,
            "files": {name: {"content": content} for name, content in self.files.items()},
        }).encode("utf-8")
        return FakeResponse(body)


class FakeGist:
    """Minimal stand-in for the GitHub Gist API."""

    def __init__(self, files: dict[str, str] | None = None, public: bool = False):
        self.files = dict(files or {})
        self.public = public
        self.patches = 0

    @property
    def names(self) -> set[str]:
        return set(self.files)

    def urlopen(self, req, timeout=None):
        method = (getattr(req, "method", None) or "GET").upper()
        if method == "PATCH":
            payload = json.loads(req.data.decode("utf-8"))
            for name, spec in payload.get("files", {}).items():
                if spec is None:
                    self.files.pop(name, None)
                else:
                    self.files[name] = spec["content"]
            self.patches += 1
            return FakeResponse(b'{"ok":true}')
        body = json.dumps({
            "public": self.public,
            "files": {name: {"content": content} for name, content in self.files.items()},
        }).encode("utf-8")
        return FakeResponse(body)


class BackupRoundTripCase(TempDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.dirname(self.use_temp_database("bot.db"))
        asyncio.run(db.setup())
        self._seed()

        self._saved_key = os.environ.get("BACKUP_ENCRYPTION_KEY")
        os.environ.pop("BACKUP_ENCRYPTION_KEY", None)

        self._saved_token = cs.GITHUB_TOKEN
        self._saved_gist = cs.GIST_ID
        cs.GITHUB_TOKEN = "fake-token"
        cs.GIST_ID = "fake-gist"
        self._reset_module_state()

    def tearDown(self):
        cs.GITHUB_TOKEN = self._saved_token
        cs.GIST_ID = self._saved_gist
        if self._saved_key is None:
            os.environ.pop("BACKUP_ENCRYPTION_KEY", None)
        else:
            os.environ["BACKUP_ENCRYPTION_KEY"] = self._saved_key
        self.release_temp_database()

    # -- helpers ---------------------------------------------------------- #
    def _reset_module_state(self):
        cs._last_sync_hash = None
        cs._last_snapshot_ts = 0.0
        cs._remote_checked = False
        cs._restore_blocked_reason = None
        cs._warned.clear()

    def _seed(self):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO user_levels "
                "(guild_id, user_id, xp, level, total_messages, voice_minutes, total_earned) "
                "VALUES (?,?,?,?,?,?,?)",
                (GUILD, MEMBER, SEED_XP, SEED_LEVEL, 99, 5, 3),
            )
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?,?,?,?)",
                (MEMBER, GUILD, SEED_WALLET, SEED_BANK),
            )
            conn.commit()
        finally:
            conn.close()

    def _wipe_local_db(self):
        """Simulate a fresh container: no local file, then db.setup() recreates it."""
        for suffix in ("", ".tmp", "-wal", "-shm"):
            try:
                os.remove(db.DB_PATH + suffix)
            except FileNotFoundError:
                pass
        asyncio.run(db.setup())
        self._reset_module_state()

    def _use(self, gist: FakeGist):
        patcher = mock.patch.object(cs.urllib.request, "urlopen", gist.urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        return gist

    def _read_back(self):
        conn = db.connect()
        try:
            level_row = conn.execute(
                "SELECT xp, level FROM user_levels WHERE guild_id=? AND user_id=?",
                (GUILD, MEMBER),
            ).fetchone()
            eco_row = conn.execute(
                "SELECT wallet, bank FROM economy WHERE user_id=? AND guild_id=?",
                (MEMBER, GUILD),
            ).fetchone()
        finally:
            conn.close()
        return level_row, eco_row

    def _assert_progress_intact(self):
        level_row, eco_row = self._read_back()
        self.assertIsNotNone(level_row, "level row was lost")
        self.assertIsNotNone(eco_row, "economy row was lost")
        self.assertEqual(level_row[0], SEED_XP, "XP was lost")
        self.assertEqual(level_row[1], SEED_LEVEL, "level was lost")
        self.assertEqual(eco_row[0], SEED_WALLET, "wallet balance was lost")
        self.assertEqual(eco_row[1], SEED_BANK, "bank balance was lost")

    def _set_key(self):
        os.environ["BACKUP_ENCRYPTION_KEY"] = cs.generate_key()
        return cs.encryption_key()

    # -- the core promise: a redeploy keeps everyone's progress ----------- #
    def test_backup_is_encrypted_even_without_a_configured_key(self):
        """The whole point: nobody has to opt in for the Gist to be unreadable.

        No BACKUP_ENCRYPTION_KEY is set here (setUp pops it), so this is exactly
        the deployment that used to upload the database as plaintext.
        """
        self.assertIsNone(cs.explicit_encryption_key())
        gist = self._use(FakeGist())
        self.assertTrue(cs.backup_to_cloud_sync())
        self.assertIn("bot.db.enc", gist.names)
        self.assertNotIn("bot.db.b64", gist.names, "the database was uploaded in plaintext")

        self._wipe_local_db()
        self.assertIsNone(self._read_back()[0], "precondition: local DB should be empty")

        self.assertTrue(cs.restore_from_cloud())
        self._assert_progress_intact()

    def test_legacy_plaintext_backup_still_restores_and_is_then_retired(self):
        """The upgrade path for a Gist that only holds the old plaintext file."""
        payload = base64.b64encode(cs.snapshot_db_bytes()).decode()
        gist = self._use(FakeGist({"bot.db.b64": payload}))

        self._wipe_local_db()
        self.assertTrue(cs.restore_from_cloud(), "legacy plaintext backup failed to restore")
        self._assert_progress_intact()

        # The next upload is encrypted and removes the readable copy.
        self._reset_module_state()
        self.assertTrue(cs.backup_to_cloud_sync())
        self.assertIn("bot.db.enc", gist.names)
        self.assertNotIn("bot.db.b64", gist.names, "the plaintext backup was left behind")

    def test_encrypted_backup_survives_a_redeploy(self):
        self._set_key()
        gist = self._use(FakeGist())
        self.assertTrue(cs.backup_to_cloud_sync())
        self.assertIn("bot.db.enc", gist.names)
        self.assertNotIn("bot.db.b64", gist.names, "plaintext copy should not exist")

        self._wipe_local_db()
        self.assertTrue(cs.restore_from_cloud())
        self._assert_progress_intact()

    def test_turning_on_a_dedicated_key_does_not_strand_existing_snapshots(self):
        """Snapshots already written with the derived key must keep restoring.

        Otherwise adding BACKUP_ENCRYPTION_KEY later would quietly make every
        backup taken before it unrecoverable.
        """
        gist = self._use(FakeGist())
        uploaded = cs.snapshot_db_bytes()
        self.assertTrue(cs.backup_to_cloud_sync())
        self.assertIn("bot.db.enc", gist.names)
        before = gist.files["bot.db.enc"]

        # The operator now adds a dedicated key and redeploys.
        self._set_key()
        self._wipe_local_db()

        self.assertTrue(cs.restore_from_cloud(), "a snapshot written with the derived key failed")
        self._assert_progress_intact()
        # The new key must be able to read the old file too.
        self.assertEqual(
            cs._decode_candidate("bot.db.enc", before, cs.decryption_keys()), uploaded
        )

    def test_bot_startup_order_restore_then_migrations_keeps_progress(self):
        """bot.main() restores first, then on_ready runs db.setup() migrations.

        If the migrations ever wiped or recreated tables, the restored data
        would vanish one step after being downloaded."""
        self._set_key()
        gist = self._use(FakeGist())
        self.assertTrue(cs.backup_to_cloud_sync())

        for suffix in ("", ".tmp", "-wal", "-shm"):
            try:
                os.remove(db.DB_PATH + suffix)
            except FileNotFoundError:
                pass
        self._reset_module_state()

        self.assertTrue(cs.restore_from_cloud())   # bot.main()
        asyncio.run(db.setup())                    # on_ready()
        self._assert_progress_intact()

    def test_multiple_redeploys_keep_progress(self):
        self._set_key()
        gist = self._use(FakeGist())
        for _ in range(3):
            self.assertTrue(cs.backup_to_cloud_sync())
            self._wipe_local_db()
            self.assertTrue(cs.restore_from_cloud())
            self._assert_progress_intact()

    # -- failure modes must never destroy data --------------------------- #
    def test_corrupt_newest_snapshot_falls_back_to_an_older_good_one(self):
        key = self._set_key()
        with open(db.DB_PATH, "rb") as f:
            seed_bytes = f.read()
        good = base64.b64encode(cs.encrypt_bytes(seed_bytes, key)).decode()
        # A newer, corrupt "current" file plus an older, valid snapshot.
        gist = self._use(FakeGist({
            "bot.db.enc": base64.b64encode(b"corrupted rubbish").decode(),
            "bot-db-20260101-000000.enc": good,
        }))

        self._wipe_local_db()
        self.assertTrue(cs.restore_from_cloud())
        self._assert_progress_intact()

    def test_non_sqlite_payload_never_overwrites_the_live_database(self):
        gist = self._use(FakeGist({
            "bot.db.b64": base64.b64encode(b"this is definitely not a database").decode(),
        }))
        self.assertFalse(cs.restore_from_cloud())
        self._assert_progress_intact()

    def test_a_backup_written_with_an_unavailable_key_is_preserved(self):
        """Changing the key must not silently destroy the only good backup.

        The wrong key means the restore cannot read the snapshot; the upload path
        must then refuse to replace it, because that file may be the only copy of
        everyone's progress left in the world.
        """
        seed_bytes = cs.snapshot_db_bytes()
        old_key = base64.b64decode(cs.generate_key())
        encrypted = base64.b64encode(cs.encrypt_bytes(seed_bytes, old_key)).decode()
        os.environ["BACKUP_ENCRYPTION_KEY"] = cs.generate_key()  # a different, valid key
        gist = self._use(FakeGist({"bot.db.enc": encrypted}))

        self._wipe_local_db()
        self.assertFalse(cs.restore_from_cloud(), "restored with the wrong key")
        self.assertIsNotNone(cs.restore_blocked_reason(), "the key mismatch went unrecorded")

        before = dict(gist.files)
        self.assertFalse(cs.backup_to_cloud_sync())
        self.assertEqual(gist.patches, 0, "a refused upload must not call the API")
        self.assertEqual(gist.files, before, "the unreadable backup was overwritten")

    def test_a_failed_boot_restore_does_not_upload_over_the_backup(self):
        """The nastiest data-loss path: the restore fails, the bot keeps running.

        Anything earned after a failed restore makes the empty-database guard stop
        applying, and the old code would then upload that partial database over a
        backup holding everyone's real progress.
        """
        gist = self._use(FakeGist())
        self.assertTrue(cs.backup_to_cloud_sync())
        good_backup = dict(gist.files)

        # A redeploy whose restore cannot reach GitHub at all.
        self._wipe_local_db()
        with mock.patch.object(cs.urllib.request, "urlopen", UnreachableGist().urlopen):
            self.assertFalse(cs.restore_from_cloud())
        self.assertIsNotNone(cs.restore_blocked_reason())

        # Activity happens, so the database is no longer "empty"...
        self._seed()
        self.assertFalse(cs._db_looks_empty())

        # ...and the upload still must not fire.
        cs._remote_checked = True  # as if a later read had succeeded
        self.assertFalse(cs.backup_to_cloud_sync())
        self.assertEqual(gist.patches, 1, "only the setup upload was expected")
        self.assertEqual(gist.files, good_backup, "the good backup was overwritten")

    def test_upload_defers_until_the_remote_has_been_read_once(self):
        """Writing to a Gist we could not read could replace a backup blindly."""
        gist = self._use(FakeGist())
        with mock.patch.object(cs.urllib.request, "urlopen", UnreachableGist().urlopen):
            self.assertFalse(cs.backup_to_cloud_sync())
        self.assertEqual(gist.patches, 0, "wrote to an unread Gist")
        self.assertFalse(cs._remote_checked)

        # The outage ends: the very next cycle uploads normally.
        self.assertTrue(cs.backup_to_cloud_sync())
        self.assertEqual(gist.patches, 1)

    def test_failed_upload_is_retried_instead_of_being_marked_done(self):
        """A refused PATCH must not record the database as already uploaded."""
        gist = self._use(RejectingGist())
        cs._last_snapshot_ts = 0.0
        self.assertFalse(cs.backup_to_cloud_sync())
        self.assertEqual(gist.patches, 1, "precondition: the write was attempted")
        self.assertIsNone(cs._last_sync_hash)
        self.assertEqual(cs._last_snapshot_ts, 0.0, "a snapshot that was never stored was claimed")

        # Once the Gist accepts writes again, the same data goes up.
        healthy = self._use(FakeGist())
        self.assertTrue(cs.backup_to_cloud_sync())
        self.assertIn("bot.db.enc", healthy.names)
        self.assertTrue([n for n in healthy.names if n.startswith("bot-db-")])

    def test_upload_refuses_to_replace_a_backup_with_an_empty_database(self):
        """The signature of a failed restore: everything empty, backup present."""
        gist = self._use(FakeGist())
        self.assertTrue(cs.backup_to_cloud_sync())
        before = dict(gist.files)

        # Simulate the local file being lost and recreated empty, with no
        # in-process restore having happened.
        for suffix in ("", ".tmp", "-wal", "-shm"):
            try:
                os.remove(db.DB_PATH + suffix)
            except FileNotFoundError:
                pass
        asyncio.run(db.setup())
        self._reset_module_state()
        self.assertTrue(cs._db_looks_empty())

        self.assertFalse(cs.backup_to_cloud_sync())
        self.assertEqual(gist.files, before, "the good backup was overwritten")

    def test_a_brand_new_deployment_can_still_write_its_first_backup(self):
        gist = self._use(FakeGist())
        for suffix in ("", ".tmp", "-wal", "-shm"):
            try:
                os.remove(db.DB_PATH + suffix)
            except FileNotFoundError:
                pass
        asyncio.run(db.setup())  # empty local DB, empty Gist
        self._reset_module_state()
        self.assertTrue(cs.backup_to_cloud_sync())
        self.assertEqual(gist.patches, 1)

    def test_missing_credentials_disable_both_directions_safely(self):
        cs.GITHUB_TOKEN = ""
        cs.GIST_ID = ""
        gist = self._use(FakeGist({"bot.db.b64": "unused"}))
        self.assertFalse(cs.restore_from_cloud())
        self.assertFalse(cs.backup_to_cloud_sync())
        self.assertEqual(gist.patches, 0)
        self._assert_progress_intact()

    def test_concurrent_uploads_do_not_interleave(self):
        """The dashboard's on-demand backup must not race the periodic loop."""
        gist = self._use(FakeGist())
        self.assertTrue(cs._upload_lock.acquire(blocking=False))
        try:
            self.assertFalse(cs.backup_to_cloud_sync(), "two uploads ran at once")
            self.assertEqual(gist.patches, 0)
        finally:
            cs._upload_lock.release()
        self.assertTrue(cs.backup_to_cloud_sync(), "the lock was not released")
        self.assertEqual(gist.patches, 1)

    def test_change_detection_skips_redundant_uploads(self):
        gist = self._use(FakeGist())
        self.assertTrue(cs.backup_to_cloud_sync())
        self.assertTrue(cs.backup_to_cloud_sync())
        self.assertEqual(gist.patches, 1, "unchanged database uploaded twice")

    def test_prunes_old_snapshots_but_keeps_the_newest(self):
        self._set_key()
        key = cs.encryption_key()
        with open(db.DB_PATH, "rb") as f:
            seed_bytes = f.read()
        payload = base64.b64encode(cs.encrypt_bytes(seed_bytes, key)).decode()
        existing = {f"bot-db-2026{month:02d}01-000000.enc": payload for month in range(1, 8)}
        gist = self._use(FakeGist(existing))

        cs._last_snapshot_ts = 0.0
        self.assertTrue(cs.backup_to_cloud_sync())

        remaining = sorted(n for n in gist.names if n.startswith("bot-db-"))
        self.assertLessEqual(len(remaining), cs.BACKUP_KEEP)
        self.assertIn("bot.db.enc", gist.names)
        # The newest snapshots are the ones kept; the oldest were pruned.
        self.assertNotIn("bot-db-20260101-000000.enc", gist.names)
        self.assertIn("bot-db-20260701-000000.enc", gist.names)

    def test_restore_discards_a_stale_write_ahead_log(self):
        """Regression: bot.db-wal from the old database used to be replayed over
        the restored file, silently discarding everything that was just
downloaded."""
        gist = self._use(FakeGist())
        self.assertTrue(cs.backup_to_cloud_sync())

        self._wipe_local_db()
        # The leftover files a killed previous process leaves behind. Without the
        # fix these made SQLite replay the old log over the restored file.
        with open(db.DB_PATH + "-wal", "wb") as f:
            f.write(b"stale write-ahead log from the previous database")
        with open(db.DB_PATH + "-shm", "wb") as f:
            f.write(b"stale shared-memory index")

        self.assertTrue(cs.restore_from_cloud())
        self.assertFalse(
            os.path.exists(db.DB_PATH + "-wal"),
            "a stale write-ahead log survived the restore",
        )
        self._assert_progress_intact()

    def test_backup_includes_rows_that_are_still_only_in_the_wal(self):
        """The snapshot must use SQLite's backup API, not a raw file read."""
        gist = self._use(FakeGist())
        # Hold a connection open so the newest commit stays in the WAL and has
        # not been checkpointed into bot.db yet.
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?,?,?,?)",
                (999, GUILD, 555, 0),
            )
            conn.commit()
            self.assertTrue(os.path.exists(db.DB_PATH + "-wal"))

            self.assertTrue(cs.backup_to_cloud_sync())
        finally:
            conn.close()

        self._wipe_local_db()
        self.assertTrue(cs.restore_from_cloud())
        self._assert_progress_intact()
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (999, GUILD)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (555,), "a row committed into the WAL was not backed up")

    def test_snapshot_is_restorable_after_pruning(self):
        self._set_key()
        gist = self._use(FakeGist())
        cs._last_snapshot_ts = 0.0
        self.assertTrue(cs.backup_to_cloud_sync())
        snapshots = [n for n in gist.names if n.startswith("bot-db-")]
        self.assertTrue(snapshots, "no rotated snapshot was written")

        # Drop the always-current file; the timestamped snapshot must carry the day.
        gist.files.pop("bot.db.enc")
        self._wipe_local_db()
        self.assertTrue(cs.restore_from_cloud())
        self._assert_progress_intact()


if __name__ == "__main__":
    unittest.main()
