"""The backup Gist used to hold the whole database in plaintext base64. These

tests pin the replacement: authenticated encryption plus a SHA-256 integrity
check, encryption that is on by default (the key is derived from the backup token
when no dedicated key is configured), and a safe read path for the legacy
plaintext format."""
import base64
import os
import unittest

from utils import cloud_sync


class BackupCryptoCase(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("BACKUP_ENCRYPTION_KEY")
        os.environ.pop("BACKUP_ENCRYPTION_KEY", None)
        self._saved_token = cloud_sync.GITHUB_TOKEN
        cloud_sync.GITHUB_TOKEN = ""
        self.key = base64.b64decode(cloud_sync.generate_key())
        self.raw = b"SQLite format 3\x00" + bytes(range(256)) * 3

    def tearDown(self):
        cloud_sync.GITHUB_TOKEN = self._saved_token
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

    def test_no_key_anywhere_means_no_write_key(self):
        """No explicit key and no token: nothing to encrypt with, so no uploads."""
        self.assertIsNone(cloud_sync.encryption_key())
        self.assertEqual(cloud_sync.decryption_keys(), [])

    def test_key_is_derived_from_the_backup_token(self):
        """The reported warning is gone: backups encrypt themselves by default."""
        cloud_sync.GITHUB_TOKEN = "token-one"
        derived = cloud_sync.derived_encryption_key()
        self.assertEqual(len(derived), 32)
        # Stable across calls, so tomorrow's deploy can still read today's file.
        self.assertEqual(cloud_sync.encryption_key(), derived)
        self.assertEqual(cloud_sync.derived_encryption_key(), derived)
        # A different token is a different key.
        cloud_sync.GITHUB_TOKEN = "token-two"
        self.assertNotEqual(cloud_sync.derived_encryption_key(), derived)

    def test_explicit_key_wins_but_the_derived_one_still_reads(self):
        cloud_sync.GITHUB_TOKEN = "token-one"
        os.environ["BACKUP_ENCRYPTION_KEY"] = cloud_sync.generate_key()
        explicit = cloud_sync.explicit_encryption_key()
        self.assertEqual(cloud_sync.encryption_key(), explicit)
        # Writing with the explicit key must not strand snapshots written before
        # it existed under the derived key.
        self.assertEqual(cloud_sync.decryption_keys(), [explicit, cloud_sync.derived_encryption_key()])

    def test_malformed_keys_fall_back_to_the_derived_one(self):
        """A typo must never silently disable encryption."""
        cloud_sync.GITHUB_TOKEN = "token-one"
        os.environ["BACKUP_ENCRYPTION_KEY"] = "not base64!!"
        self.assertIsNone(cloud_sync.explicit_encryption_key())
        self.assertEqual(cloud_sync.encryption_key(), cloud_sync.derived_encryption_key())

        os.environ["BACKUP_ENCRYPTION_KEY"] = base64.b64encode(b"too short").decode()
        self.assertIsNone(cloud_sync.explicit_encryption_key())
        self.assertEqual(cloud_sync.encryption_key(), cloud_sync.derived_encryption_key())

        os.environ["BACKUP_ENCRYPTION_KEY"] = cloud_sync.generate_key()
        self.assertEqual(len(cloud_sync.explicit_encryption_key()), 32)

    def test_a_rotated_token_can_be_recovered_through_the_explicit_key(self):
        """Rotating the token strands old snapshots unless they stay readable.

        The bot refuses to upload while a backup it cannot read exists, so an
        operator needs a way back in: the value this helper prints.
        """
        old_token = "the-token-that-wrote-the-backups"
        stranded = cloud_sync.encrypt_bytes(
            self.raw, cloud_sync.derive_key_from_token(old_token)
        )

        cloud_sync.GITHUB_TOKEN = "a-brand-new-token"
        self.assertIsNone(
            cloud_sync._decode_candidate("bot.db.enc", base64.b64encode(stranded).decode(),
                                         cloud_sync.decryption_keys()),
            "precondition: the new token must not read the old snapshot",
        )

        os.environ["BACKUP_ENCRYPTION_KEY"] = cloud_sync.base64_key_for_token(old_token)
        self.assertEqual(
            cloud_sync._decode_candidate("bot.db.enc", base64.b64encode(stranded).decode(),
                                         cloud_sync.decryption_keys()),
            self.raw,
        )

    def test_key_id_is_short_stable_and_does_not_reveal_the_key(self):
        cloud_sync.GITHUB_TOKEN = "token-one"
        key = cloud_sync.encryption_key()
        fingerprint = cloud_sync.key_id(key)
        self.assertEqual(fingerprint, cloud_sync.key_id(key))
        self.assertEqual(len(fingerprint), 8)
        self.assertNotIn(fingerprint, base64.b64encode(key).decode())
        cloud_sync.GITHUB_TOKEN = "token-two"
        self.assertNotEqual(cloud_sync.key_id(cloud_sync.encryption_key()), fingerprint)
        self.assertEqual(cloud_sync.key_id(None), "none")

    def test_candidate_decoding(self):
        encrypted = base64.b64encode(
            cloud_sync.encrypt_bytes(self.raw, self.key)
        ).decode()
        self.assertEqual(
            cloud_sync._decode_candidate("bot.db.enc", encrypted, [self.key]), self.raw
        )
        # Each key is tried in turn: the wrong one first must not defeat the payload.
        other = base64.b64decode(cloud_sync.generate_key())
        self.assertEqual(
            cloud_sync._decode_candidate("bot.db.enc", encrypted, [other, self.key]), self.raw
        )
        # Encrypted payload with no key at all can't be used, but must not crash.
        self.assertIsNone(cloud_sync._decode_candidate("bot.db.enc", encrypted, None))
        self.assertIsNone(cloud_sync._decode_candidate("bot.db.enc", encrypted, []))
        # A bare key (not wrapped in a list) still works.
        self.assertEqual(cloud_sync._decode_candidate("bot.db.enc", encrypted, self.key), self.raw)
        # Legacy plaintext still restores, with or without keys.
        legacy = base64.b64encode(self.raw).decode()
        self.assertEqual(
            cloud_sync._decode_candidate("bot.db.b64", legacy, [self.key]), self.raw
        )
        self.assertEqual(cloud_sync._decode_candidate("bot.db.b64", legacy, None), self.raw)

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
