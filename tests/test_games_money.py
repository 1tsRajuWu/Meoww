"""Casino bets must be backed by coins that actually exist.

The instant games used to read a balance, apply a *net delta* afterwards, and
rely on ``MAX(0, wallet + ?)`` to stop the wallet going negative. That clamping is
asymmetric: losses were capped at the coins you had, while wins paid in full — so
several simultaneous invocations could wager the same coins several times over.
``settle_bet`` takes the stake first (refusing when the wallet can't cover it) and
pays out second, inside one transaction. These tests pin both the arithmetic and
the leverage case.
"""
import asyncio
import unittest
from unittest import mock

from cogs import economy as economy_module
from cogs.games import BlackjackView, TicTacToeInviteView
from tests.support import TempDatabaseMixin
from utils import db


async def _make_tables():
    import aiosqlite

    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("""CREATE TABLE IF NOT EXISTS economy (
            user_id INTEGER, guild_id INTEGER, wallet INTEGER DEFAULT 0, bank INTEGER DEFAULT 0,
            last_daily TEXT, last_weekly TEXT, last_work TEXT, PRIMARY KEY (user_id, guild_id))""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, guild_id INTEGER, item_name TEXT)""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS user_levels (
            guild_id INTEGER, user_id INTEGER, xp INTEGER DEFAULT 0, level INTEGER DEFAULT 0,
            total_messages INTEGER DEFAULT 0, voice_minutes INTEGER DEFAULT 0,
            total_earned INTEGER DEFAULT 0, last_message_ts REAL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id))""")
        await conn.commit()


def _wallet(user_id: int, guild_id: int) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (user_id, guild_id)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


class _FakeMember:
    def __init__(self, uid: int):
        self.id = uid
        self.display_name = f"User{uid}"
        self.name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.bot = False


class CasinoMoneyCase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    GUILD = 999
    USER = 42

    async def asyncSetUp(self):
        path = self.use_temp_database("casino.db")
        economy_module.DB_PATH = path          # the cog captured the path at import
        await _make_tables()

    async def asyncTearDown(self):
        self.release_temp_database()

    def _seed(self, wallet: int, user_id: int | None = None):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?, ?, ?, 0)",
                (user_id or self.USER, self.GUILD, wallet),
            )
            conn.commit()
        finally:
            conn.close()


class SettleBetMath(CasinoMoneyCase):
    async def test_a_win_returns_the_stake_plus_the_profit(self):
        self._seed(500)
        self.assertTrue(await economy_module.settle_bet(self.USER, self.GUILD, 100, 2.0))
        self.assertEqual(_wallet(self.USER, self.GUILD), 600)

    async def test_a_loss_takes_exactly_the_stake(self):
        self._seed(500)
        self.assertTrue(await economy_module.settle_bet(self.USER, self.GUILD, 100, 0.0))
        self.assertEqual(_wallet(self.USER, self.GUILD), 400)

    async def test_a_push_returns_the_stake_unchanged(self):
        self._seed(500)
        self.assertTrue(await economy_module.settle_bet(self.USER, self.GUILD, 100, 1.0))
        self.assertEqual(_wallet(self.USER, self.GUILD), 500)

    async def test_a_partial_payout_returns_half_the_stake(self):
        self._seed(500)
        self.assertTrue(await economy_module.settle_bet(self.USER, self.GUILD, 100, 0.5))
        self.assertEqual(_wallet(self.USER, self.GUILD), 450)

    async def test_a_bet_larger_than_the_wallet_is_refused_untouched(self):
        self._seed(50)
        self.assertFalse(await economy_module.settle_bet(self.USER, self.GUILD, 100, 2.0))
        self.assertEqual(_wallet(self.USER, self.GUILD), 50)

    async def test_take_stake_refuses_when_the_wallet_is_short(self):
        self._seed(99)
        self.assertFalse(await economy_module.take_stake(self.USER, self.GUILD, 100))
        self.assertEqual(_wallet(self.USER, self.GUILD), 99)

    async def test_take_stake_holds_the_whole_stake(self):
        self._seed(100)
        self.assertTrue(await economy_module.take_stake(self.USER, self.GUILD, 100))
        self.assertEqual(_wallet(self.USER, self.GUILD), 0)


class NoLeverageOnParallelBets(CasinoMoneyCase):
    async def test_parallel_bets_cannot_pledge_the_same_coins(self):
        """Three simultaneous all-in losses with 100 coins may only fund one bet.

        Under the old net-delta code all three "succeeded" — each subtracted the
        stake from a balance it had read earlier and the clamp at zero absorbed the
        shortfall — so a player could wager the same coins repeatedly and keep every
        win in full.
        """
        self._seed(100)
        results = await asyncio.gather(*(
            economy_module.settle_bet(self.USER, self.GUILD, 100, 0.0) for _ in range(3)
        ))
        self.assertEqual(sum(1 for r in results if r), 1, "only one bet can be backed")
        self.assertEqual(_wallet(self.USER, self.GUILD), 0)


class TicTacToeStakes(CasinoMoneyCase):
    async def test_both_stakes_move_or_neither_does(self):
        self._seed(500, user_id=1)
        self._seed(30, user_id=2)          # second player can't match the bet
        view = TicTacToeInviteView(_FakeMember(1), _FakeMember(2), self.GUILD, 100)
        paid = await asyncio.to_thread(db.run_atomic, view._take_both_stakes)
        self.assertFalse(paid)
        self.assertEqual(_wallet(1, self.GUILD), 500, "the first player must not be charged alone")
        self.assertEqual(_wallet(2, self.GUILD), 30)

    async def test_both_stakes_are_taken_together(self):
        self._seed(500, user_id=1)
        self._seed(500, user_id=2)
        view = TicTacToeInviteView(_FakeMember(1), _FakeMember(2), self.GUILD, 100)
        paid = await asyncio.to_thread(db.run_atomic, view._take_both_stakes)
        self.assertTrue(paid)
        self.assertEqual(_wallet(1, self.GUILD), 400)
        self.assertEqual(_wallet(2, self.GUILD), 400)


class BlackjackTimeoutSettles(CasinoMoneyCase):
    def _view(self, player, dealer, deck, bet=100):
        return BlackjackView(player, dealer, deck, bet, self.USER, self.GUILD)

    async def test_a_winning_hand_is_paid_when_the_timer_expires(self):
        self._seed(500)
        await economy_module.take_stake(self.USER, self.GUILD, 100)   # dealt
        view = self._view(["10", "K"], ["5"], ["2"])
        await view.on_timeout()
        self.assertTrue(view.finished)
        self.assertEqual(_wallet(self.USER, self.GUILD), 400 + 200, "20 vs 7 must pay out")

    async def test_a_losing_hand_forfeits_only_the_stake(self):
        self._seed(500)
        await economy_module.take_stake(self.USER, self.GUILD, 100)
        view = self._view(["5"], ["10", "K"], ["2"])
        await view.on_timeout()
        self.assertEqual(_wallet(self.USER, self.GUILD), 400)

    async def test_timeout_never_settles_twice(self):
        self._seed(500)
        await economy_module.take_stake(self.USER, self.GUILD, 100)
        view = self._view(["10", "K"], ["5"], ["2"])
        await view.on_timeout()
        await view.on_timeout()
        self.assertEqual(_wallet(self.USER, self.GUILD), 600, "a second timeout must not pay again")


class SlotOutcomesRemainCorrect(CasinoMoneyCase):
    async def test_slots_pays_the_stake_back_on_a_jackpot(self):
        self._seed(1000)
        # Force three identical reels, then check the wallet move matches the payout.
        with mock.patch("random.choice", return_value="🍒"):
            paid = await economy_module.settle_bet(
                self.USER, self.GUILD, 100, 2  # 🍒 pays 2x
            )
        self.assertTrue(paid)
        self.assertEqual(_wallet(self.USER, self.GUILD), 1000 - 100 + 200)


if __name__ == "__main__":
    unittest.main()
