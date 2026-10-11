"""Every casino game pays below 100% — like real life.

`/gamble` once paid a fair double on a fair coin, so three all-in wins turned
200k into 1.6M on camera. `/highlow` was worse: its fixed table paid up to
~122% to anyone who only picked the likely side. And `/slots` went the other
way at ~32% — more predatory than any real casino. This pins each game's
return-to-player: the even-money games pay 1.9x, slots pays a ~95% paytable,
high-low prices 97% of true odds off the live deck, and the Tic-Tac-Toe bot
plays perfectly, so the best possible outcome is a draw.
"""
import random
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import economy as economy_module
from cogs import games as games_module
from tests.support import TempDatabaseMixin
from utils import db

GUILD = 9202
USER = 5150


def _slot_rtp():
    triples = sum(games_module.SLOT_MULTIPLIERS.values()) / 7**3
    pairs = (3 * 7 * 6 / 7**3) * 0.5
    return triples + pairs


class PaytableMath(unittest.TestCase):
    def test_slots_returns_like_a_real_slot(self):
        rtp = _slot_rtp()
        self.assertGreaterEqual(rtp, 0.93)
        self.assertLessEqual(rtp, 0.97)

    def test_even_money_games_take_a_cut(self):
        self.assertEqual(games_module.EVEN_MONEY_PAYOUT, 1.9)

    def test_mines_still_prices_a_house_edge(self):
        view = games_module.MinesView(1, 1, 100, 3)
        first_gem = view.calc_next_multiplier(1)
        fair = 16 / 13
        self.assertLess(first_gem, fair)
        self.assertGreater(first_gem, 0.9 * fair)

    def test_roulette_keeps_its_zero(self):
        self.assertEqual(games_module.ROULETTE_NUMBERS[0], "green")
        self.assertEqual(len(games_module.ROULETTE_NUMBERS), 37)


class HighLowPricing(unittest.TestCase):
    def _view(self, current, deck):
        view = games_module.HighLowView(1, 1, 100)
        view.current_card = current
        view.deck = list(deck)
        return view

    def _full_deck_minus(self, rank, suit):
        from cogs.games import CARD_VALUES, SUITS

        return [(r, s) for r in CARD_VALUES for s in SUITS if not (r == rank and s == suit)]

    def test_mid_card_pays_about_double(self):
        view = self._view(("8", "♠️"), self._full_deck_minus("8", "♠️"))
        self.assertEqual(view.calc_multiplier_gain("higher"), 2.06)
        self.assertEqual(view.calc_multiplier_gain("lower"), 2.06)
        self.assertEqual(view.calc_multiplier_gain("equal"), 16.49)

    def test_no_pick_ever_pays_above_its_price(self):
        from cogs.games import CARD_VALUES, SUITS

        for rank in CARD_VALUES:
            for suit in SUITS[:1]:
                deck = self._full_deck_minus(rank, suit)
                view = self._view((rank, suit), deck)
                remaining = len(deck)
                cur = CARD_VALUES[rank]
                wins = {
                    "higher": sum(1 for (r, _s) in deck if CARD_VALUES[r] > cur),
                    "lower": sum(1 for (r, _s) in deck if CARD_VALUES[r] < cur),
                    "equal": sum(1 for (r, _s) in deck if CARD_VALUES[r] == cur),
                }
                for choice, w in wins.items():
                    if w == 0:
                        continue
                    ev = view.calc_multiplier_gain(choice) * (w / remaining)
                    self.assertLessEqual(ev, 0.98, f"{rank} {choice} pays {ev:.3f}")

    def test_impossible_picks_stay_unwinnable(self):
        view = self._view(("A", "♠️"), self._full_deck_minus("A", "♠️"))
        gain = view.calc_multiplier_gain("higher")
        self.assertGreater(gain, 0)
        # ...but nothing higher exists, so the price can never pay out.


class PerfectTicTacToe(unittest.TestCase):
    def _view(self):
        p1 = SimpleNamespace(id=1, display_name="P1", mention="@P1")
        return games_module.TicTacToeView(p1, None, GUILD, 0)

    def test_bot_blocks_an_immediate_loss(self):
        view = self._view()
        view.board = [[1, 1, 0], [0, 2, 0], [0, 0, 0]]
        self.assertEqual(view.bot_find_move(), (2, 0))

    def test_bot_takes_an_immediate_win(self):
        view = self._view()
        view.board = [[2, 2, 0], [1, 1, 0], [0, 0, 0]]
        self.assertEqual(view.bot_find_move(), (2, 0))

    def test_bot_refuses_the_classic_fork(self):
        # X corners, O center, X opposite corner: only an edge draws.
        view = self._view()
        view.board = [[1, 0, 0], [0, 2, 0], [0, 0, 1]]
        move = view.bot_find_move()
        self.assertIn(move, [(0, 1), (1, 0), (1, 2), (2, 1)])

    def test_bot_never_loses_to_random_play(self):
        rng = random.Random(7)
        losses = 0
        for _ in range(200):
            view = self._view()
            turn = 1
            while True:
                empties = [(x, y) for y in range(3) for x in range(3) if view.board[y][x] == 0]
                if not empties:
                    break
                if turn == 1:
                    x, y = rng.choice(empties)
                    view.board[y][x] = 1
                else:
                    x, y = view.bot_find_move()
                    view.board[y][x] = 2
                winner = view.check_winner()
                if winner == 1:
                    losses += 1
                    break
                if winner == 2 or view.is_full():
                    break
                turn = 2 if turn == 1 else 1
        self.assertEqual(losses, 0)


class _FakeResponse:
    def __init__(self):
        self.messages = []

    def is_done(self):
        return False

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


def _wallet():
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (USER, GUILD)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _seed(wallet):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?, ?, ?, 0)",
            (USER, GUILD, wallet),
        )
        conn.commit()
    finally:
        conn.close()


def _interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=USER, display_name="Gambler", mention=f"<@{USER}>"),
        guild=SimpleNamespace(id=GUILD),
        response=_FakeResponse(),
        client=SimpleNamespace(),
    )


async def _noop(*_a, **_k):
    return None


class EvenMoneyPayouts(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("house-edge.db")
        economy_module.DB_PATH = path
        await db.setup()
        self.cog = games_module.Games(bot=None)

    async def asyncTearDown(self):
        self.release_temp_database()

    def _profit(self, bet):
        return int(bet * games_module.EVEN_MONEY_PAYOUT) - bet

    async def test_gamble_win_pays_1_9x(self):
        _seed(100_000)
        with mock.patch.object(games_module, "send_card", new=_noop):
            with mock.patch("random.randint", side_effect=[100, 1]):
                await games_module.Games.gamble.callback(self.cog, _interaction(), 10_000)
        self.assertEqual(_wallet(), 100_000 + self._profit(10_000))

    async def test_dice_win_pays_1_9x(self):
        _seed(100_000)
        with mock.patch.object(games_module, "send_card", new=_noop):
            with mock.patch("random.randint", side_effect=[6, 1]):
                await games_module.Games.dice.callback(self.cog, _interaction(), 10_000)
        self.assertEqual(_wallet(), 100_000 + self._profit(10_000))

    async def test_coinflip_win_pays_1_9x(self):
        _seed(100_000)
        with mock.patch.object(games_module, "send_card", new=_noop):
            with mock.patch("random.choice", return_value="heads"):
                await games_module.Games.coinflip.callback(
                    self.cog, _interaction(), "heads", 10_000
                )
        self.assertEqual(_wallet(), 100_000 + self._profit(10_000))

    async def test_rps_win_pays_1_9x(self):
        _seed(100_000)
        with mock.patch.object(games_module, "send_card", new=_noop):
            with mock.patch("random.choice", return_value="scissors"):
                await games_module.Games.rps.callback(
                    self.cog, _interaction(), "rock", 10_000
                )
        self.assertEqual(_wallet(), 100_000 + self._profit(10_000))


if __name__ == "__main__":
    unittest.main()
