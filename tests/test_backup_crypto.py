"""The backup Gist used to hold the whole database in plaintext base64. These
tests pin the replacement: authenticated encryption plus a SHA-256 integrity
check, with a safe fallback for the legacy plaintext format."""
import base64
import os
import unittest

from utils import cloud_sync


class BackupCryptoCase(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("BACKUP_ENCRYPTION_KEY")
        os.environ.pop("BACKUP_ENCRYPTION_KEY", None)
        self.key = base64.b64decode(cloud_sync.generate_key())
        self.raw = b"SQLite format 3\x00" + bytes(range(256)) * 3

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("BACKUP_ENCRYPTION_KEY", None)
        else:
            os.environ["BACKUP_ENCRYPTION_KEY"] = self._saved_env

    def test_round_trip(self):
        blob = cloud_sync.encrypt_bytes(self.raw, self.key)
        self.assertNotEqual(blob, self.raw)
        self.assertNotIn(self.raw[:16], blob)
        self.assertEqual(cloud_sync.decrypt_bytes(blob, self.key), self.raw)

    def test_tampered_ciphertext_is_rejected(self):
        blob = bytearray(cloud_sync.encrypt_bytes(self.raw, self.key))
        blob[-1] ^= 0xFF
        with self.assertRaises(Exception):
            cloud_sync.decrypt_bytes(bytes(blob), self.key)

    def test_wrong_key_is_rejected(self):
        blob = cloud_sync.encrypt_bytes(self.raw, self.key)
        other = base64.b64decode(cloud_sync.generate_key())
        with self.assertRaises(Exception):
            cloud_sync.decrypt_bytes(blob, other)

    def test_truncated_ciphertext_is_rejected(self):
        blob = cloud_sync.encrypt_bytes(self.raw, self.key)
        with self.assertRaises(Exception):
            cloud_sync.decrypt_bytes(blob[: len(blob) // 2], self.key)

    def test_integrity_check_catches_body_swap(self):
        """Even with the key, a payload whose stored hash doesn't match fails."""
        from nacl.secret import SecretBox
        from nacl.utils import random as nacl_random

        forged = SecretBox(self.key).encrypt(
            b"0" * 64 + b"\n" + b"not the real database", nacl_random(SecretBox.NONCE_SIZE)
        )
        with self.assertRaises(ValueError):
            cloud_sync.decrypt_bytes(bytes(forged), self.key)

    def test_generated_keys_are_valid(self):
        for _ in range(5):
            raw = base64.b64decode(cloud_sync.generate_key())
            self.assertEqual(len(raw), 32)

    def test_missing_key_means_plaintext_mode(self):
        self.assertIsNone(cloud_sync.encryption_key())

    def test_malformed_keys_are_refused(self):
        os.environ["BACKUP_ENCRYPTION_KEY"] = "not base64!!"
        self.assertIsNone(cloud_sync.encryption_key())
        os.environ["BACKUP_ENCRYPTION_KEY"] = base64.b64encode(b"too short").decode()
        self.assertIsNone(cloud_sync.encryption_key())
        os.environ["BACKUP_ENCRYPTION_KEY"] = cloud_sync.generate_key()
        self.assertEqual(len(cloud_sync.encryption_key()), 32)

    def test_candidate_decoding(self):
        encrypted = base64.b64encode(
            cloud_sync.encrypt_bytes(self.raw, self.key)
        ).decode()
        self.assertEqual(
            cloud_sync._decode_candidate("bot.db.enc", encrypted, self.key), self.raw
        )
        # Encrypted payload without a key can't be used, but must not crash.
        self.assertIsNone(cloud_sync._decode_candidate("bot.db.enc", encrypted, None))
        # Legacy plaintext still restores.
        legacy = base64.b64encode(self.raw).decode()
        self.assertEqual(
            cloud_sync._decode_candidate("bot.db.b64", legacy, self.key), self.raw
        )

    def test_snapshots_sort_newest_first(self):
        names = cloud_sync._snapshot_names({
            "bot-db-20260101-000000.enc": {},
            "bot-db-20260913-120000.enc": {},
            "bot-db-20260505-090000.enc": {},
            "bot.db.enc": {},
            "notes.txt": {},
        })
        self.assertEqual(names, [
            "bot-db-20260913-120000.enc",
            "bot-db-20260505-090000.enc",
            "bot-db-20260101-000000.enc",
        ])


if __name__ == "__main__":
    unittest.main()
