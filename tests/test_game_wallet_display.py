"""Instant games must show the wallet they actually left behind.

`/slots`, `/coinflip`, `/dice`, `/rps`, `/roulette` and `/gamble` used to
compute "New Wallet" from the balance read *before* the bet settled. Anything
landing mid-command (activity coins, a second game) made the card lie — and a
lying wallet card is how real missing coins get reported as a display quirk
instead of investigated. The card now re-reads after the settle, so this test
drops +9 mid-flight coins into the settle and demands the card show them.
"""
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import economy as economy_module
from cogs import games as games_module
from tests.support import TempDatabaseMixin
from utils import db

GUILD = 9101
USER = 4242


class _FakeResponse:
    def __init__(self):
        self.messages = []

    def is_done(self):
        return False

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


def _interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=USER, display_name="Gambler", mention=f"<@{USER}>"),
        guild=SimpleNamespace(id=GUILD),
        response=_FakeResponse(),
        client=SimpleNamespace(),
    )


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


def _wallet_figure(embed):
    text = embed.description or ""
    for field in getattr(embed, "fields", []) or []:
        text += "\n" + str(field.value)
    match = re.search(r"New Wallet:\W*([\d,]+)", text)
    assert match, f"no New Wallet figure in card: {text[:200]!r}"
    return int(match.group(1).replace(",", ""))


class GameWalletDisplay(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("game-display.db")
        economy_module.DB_PATH = path
        await db.setup()
        self.cog = games_module.Games(bot=None)

    async def asyncTearDown(self):
        self.release_temp_database()

    def _mid_flight_settle(self):
        real_settle = economy_module.settle_bet

        async def settle_then_earn(user_id, guild_id, bet, mult):
            ok = await real_settle(user_id, guild_id, bet, mult)
            # Activity coins landing while the dice are rolling.
            await economy_module.update_wallet(user_id, guild_id, 9)
            return ok

        return mock.patch.object(games_module, "settle_bet", new=settle_then_earn)

    async def _run_gamble(self, bet, rolls):
        seen = {}

        async def _capture(target, **kwargs):
            seen["embed"] = kwargs.get("embed")

        interaction = _interaction()
        with mock.patch.object(games_module, "send_card", new=_capture):
            with mock.patch("random.randint", side_effect=rolls):
                with self._mid_flight_settle():
                    await games_module.Games.gamble.callback(self.cog, interaction, bet)
        return seen["embed"]

    async def test_gamble_win_shows_post_settle_wallet(self):
        _seed(50_000)
        embed = await self._run_gamble(10_000, [100, 1])
        # 50,000 + 9,000 profit (1.9x) + 9 mid-flight. The old math showed the
        # pre-settle wallet plus the full bet.
        self.assertEqual(_wallet(), 59_009)
        self.assertEqual(_wallet_figure(embed), 59_009)

    async def test_gamble_loss_shows_post_settle_wallet(self):
        _seed(50_000)
        embed = await self._run_gamble(10_000, [1, 100])
        self.assertEqual(_wallet(), 40_009)
        self.assertEqual(_wallet_figure(embed), 40_009)


if __name__ == "__main__":
    unittest.main()
