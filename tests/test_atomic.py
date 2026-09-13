"""Every balance move now goes through utils.db.run_atomic. These tests pin the
property that mattered: concurrent operations can never mint coins or push a
wallet below zero."""
import asyncio
import threading
import unittest

from tests.support import TempDatabaseMixin
from utils import db


class AtomicCase(TempDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.use_temp_database()
        asyncio.run(db.setup())

    def tearDown(self):
        self.release_temp_database()

    # -- helpers ---------------------------------------------------------- #
    def _seed(self, user_id, wallet):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?, ?, ?, 0)",
                (user_id, 1, wallet),
            )
            conn.commit()
        finally:
            conn.close()

    def _wallet(self, user_id):
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (user_id, 1)
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    # -- tests ------------------------------------------------------------ #
    def test_returns_value_and_commits(self):
        self._seed(1, 100)

        def work(cur):
            cur.execute("UPDATE economy SET wallet = wallet + 50 WHERE user_id=? AND guild_id=?", (1, 1))
            return cur.rowcount

        self.assertEqual(db.run_atomic(work), 1)
        self.assertEqual(self._wallet(1), 150)

    def test_exception_rolls_everything_back(self):
        self._seed(1, 100)

        def work(cur):
            cur.execute("UPDATE economy SET wallet = wallet - 100 WHERE user_id=? AND guild_id=?", (1, 1))
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            db.run_atomic(work)
        self.assertEqual(self._wallet(1), 100, "partial transaction was committed")

    def test_conditional_debit_reports_no_match(self):
        self._seed(1, 10)

        def work(cur):
            cur.execute(
                "UPDATE economy SET wallet = wallet - ? WHERE user_id=? AND guild_id=? AND wallet >= ?",
                (999, 1, 1, 999),
            )
            return cur.rowcount

        self.assertEqual(db.run_atomic(work), 0)
        self.assertEqual(self._wallet(1), 10)

    def test_concurrent_transfers_conserve_coins(self):
        """The original bug: parallel /pay calls both passed the balance check."""
        self._seed(1, 1000)
        self._seed(2, 0)
        moves = 200
        errors = []

        def transfer():
            def work(cur):
                cur.execute(
                    "UPDATE economy SET wallet = wallet - ? WHERE user_id=? AND guild_id=? AND wallet >= ?",
                    (10, 1, 1, 10),
                )
                if cur.rowcount != 1:
                    return 0
                cur.execute(
                    "UPDATE economy SET wallet = wallet + ? WHERE user_id=? AND guild_id=?",
                    (10, 2, 1),
                )
                return 1

            try:
                db.run_atomic(work)
            except Exception as exc:  # pragma: no cover
                errors.append(str(exc))

        threads = [threading.Thread(target=transfer) for _ in range(moves)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "concurrent writers hit lock errors")
        wallet_a = self._wallet(1)
        wallet_b = self._wallet(2)
        self.assertEqual(wallet_a + wallet_b, 1000, "coins were minted or destroyed")
        self.assertGreaterEqual(wallet_a, 0)
        self.assertEqual(wallet_b, 1000 - wallet_a)
        self.assertEqual(wallet_b % 10, 0)

    def test_concurrent_debits_never_go_negative(self):
        self._seed(1, 300)
        errors = []

        def spend():
            def work(cur):
                cur.execute(
                    "UPDATE economy SET wallet = wallet - ? WHERE user_id=? AND guild_id=? AND wallet >= ?",
                    (100, 1, 1, 100),
                )
                return cur.rowcount

            try:
                db.run_atomic(work)
            except Exception as exc:  # pragma: no cover
                errors.append(str(exc))

        threads = [threading.Thread(target=spend) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        remaining = self._wallet(1)
        self.assertGreaterEqual(remaining, 0)
        self.assertEqual(remaining % 100, 0)
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
