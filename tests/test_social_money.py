"""The social cog's money paths, driven through their real callbacks.

``/crash`` and ``/sellanimals`` live in ``cogs/social.py``, so the earlier
"every bet goes through settle_bet" fix (which only covered ``cogs/games.py``)
missed both. These tests pin the four defects that were there:

* ``/crash``  took the stake with ``update_wallet(-bet)``, which clamps at zero
  instead of refusing, so two games at once could each start unpaid;
* ``/crash``  returned from its update loop when an edit failed, leaving the
  buttons live — a player could then cash out at a multiplier the rocket may
  never have reached, and a player who did nothing simply lost the stake with no
  chance to play;
* ``/sellanimals`` read the rows on one connection and credited the coins on
  another, so two simultaneous sales of the same animals were both paid;
* ``/sellanimals`` deleted the animals *before* the payment, so a failure cost
  the player the animals and the coins.

``cogs/social.py`` captures ``DB_PATH`` at import (like every cog), so the tests
patch the module attribute as well as ``utils.db``.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from cogs import economy as economy_module
from cogs import social as social_module
from tests.support import TempDatabaseMixin
from utils import db

GUILD = 777
USER = 55


def _wallet(user_id: int, guild_id: int = GUILD) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (user_id, guild_id)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _animal_count(user_id: int, guild_id: int = GUILD) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM user_animals WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _http_error(message: str = "boom") -> discord.HTTPException:
    """A real discord.HTTPException, as discord.py raises on a failed edit."""
    return discord.HTTPException(SimpleNamespace(status=500, reason="Server Error"), message)


class _FakeUser:
    def __init__(self, uid: int):
        self.id = uid
        self.display_name = f"User{uid}"
        self.display_avatar = SimpleNamespace(url="https://example.invalid/a.png")
        self.mention = f"<@{uid}>"


class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    def is_done(self) -> bool:
        return bool(self.messages)


class _FakeMessage:
    """Stands in for the game card; can fail its first N edits."""

    def __init__(self, fail_times: int = 0):
        self.edits = []
        self.fail_times = fail_times

    async def edit(self, *, embed=None, content=None, view=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise _http_error()
        self.edits.append({"embed": embed, "content": content, "view": view})

    @property
    def last_title(self):
        return self.edits[-1]["embed"].title if self.edits else None


class _FakeInteraction:
    def __init__(self, uid: int = USER, gid: int = GUILD, fail_edits: int = 0):
        self.user = _FakeUser(uid)
        self.guild = SimpleNamespace(id=gid)
        self.response = _FakeResponse()
        self.client = None
        self.card = _FakeMessage(fail_times=fail_edits)

    async def original_response(self):
        return self.card


class _SocialCase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("social.db")
        # Both cogs captured the path at import time.
        social_module.DB_PATH = path
        economy_module.DB_PATH = path
        await db.setup()
        self.cog = social_module.Social(bot=None)

    async def asyncTearDown(self):
        self.release_temp_database()

    def _seed_wallet(self, wallet: int, user_id: int = USER):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?, ?, ?, 0)",
                (user_id, GUILD, wallet),
            )
            conn.commit()
        finally:
            conn.close()

    def _seed_animals(self, name: str, count: int, tier: str = "common", user_id: int = USER):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO user_animals (guild_id, user_id, animal_name, emoji, tier, count)"
                " VALUES (?, ?, ?, '🐭', ?, ?)",
                (GUILD, user_id, name, tier, count),
            )
            conn.commit()
        finally:
            conn.close()

    async def _sell(self, interaction, tier: str = "all"):
        with mock.patch.object(social_module, "send_card", new=_async_noop):
            await social_module.Social.sellanimals.callback(self.cog, interaction, tier)

    async def _crash(self, interaction, bet: int):
        await social_module.Social.crash.callback(self.cog, interaction, bet)


async def _async_noop(*args, **kwargs):
    return None


class SellAnimalsIsPaidOnce(_SocialCase):
    async def test_selling_pays_the_full_price_and_removes_the_animals(self):
        # Mouse is a real common animal in ANIMALS and is worth 50.
        self._seed_wallet(0)
        self._seed_animals("Mouse", 3)
        interaction = _FakeInteraction()

        await self._sell(interaction)

        self.assertEqual(_wallet(USER), 150)
        self.assertEqual(_animal_count(USER), 0)

    async def test_unknown_animals_fall_back_to_the_default_price(self):
        self._seed_wallet(0)
        self._seed_animals("Not A Real Animal", 2)

        await self._sell(_FakeInteraction())

        self.assertEqual(_wallet(USER), 100)

    async def test_selling_nothing_says_so_and_pays_nothing(self):
        self._seed_wallet(500)
        interaction = _FakeInteraction()

        await self._sell(interaction)

        self.assertEqual(_wallet(USER), 500)
        self.assertTrue(interaction.response.messages)
        self.assertIn("don't have any animals", interaction.response.messages[-1]["content"])

    async def test_parallel_sales_of_the_same_animals_can_only_be_paid_once(self):
        """The old code read the rows, deleted, then paid on another connection.

        Both sales saw the same rows, computed the same total, and were both paid
        — the first deletion took the animals, the second deleted nothing.
        """
        self._seed_wallet(0)
        self._seed_animals("Mouse", 4)          # 4 * 50 = 200
        first, second = _FakeInteraction(), _FakeInteraction()

        await asyncio.gather(self._sell(first), self._sell(second))

        self.assertEqual(_wallet(USER), 200, "the same animals were paid for twice")
        self.assertEqual(_animal_count(USER), 0)


class CrashHoldsTheStake(_SocialCase):
    async def test_a_bet_the_wallet_cannot_cover_is_refused_untouched(self):
        self._seed_wallet(50)
        interaction = _FakeInteraction()

        await self._crash(interaction, 100)

        self.assertEqual(_wallet(USER), 50)
        self.assertIn("in your wallet", interaction.response.messages[-1]["content"])

    async def test_a_losing_round_really_takes_the_stake(self):
        self._seed_wallet(100)
        with mock.patch.object(social_module.random, "random", return_value=0.0):
            # 0.0 < 0.08 => the rocket busts instantly, so the update loop is skipped.
            await self._crash(_FakeInteraction(), 100)
        self.assertEqual(_wallet(USER), 0)

    async def test_two_all_in_rounds_at_once_can_only_fund_one(self):
        """The old ``update_wallet(-bet)`` clamped at zero, so two games that both
        read a 100-coin wallet each started while only one was actually paid for.
        Both could then cash out; the stake is now taken conditionally instead.
        """
        self._seed_wallet(100)
        first, second = _FakeInteraction(), _FakeInteraction()

        with mock.patch.object(social_module.random, "random", return_value=0.0):
            await asyncio.gather(self._crash(first, 100), self._crash(second, 100))

        started = [i for i in (first, second) if any("view" in m for m in i.response.messages)]
        self.assertEqual(len(started), 1, "both rounds started off one stake")
        self.assertEqual(_wallet(USER), 0)

    async def test_a_failed_card_update_refunds_instead_of_leaving_live_buttons(self):
        """The old code returned on an edit failure with the view still active.

        The player had paid, could not see the rocket, and the Cash Out button was
        still clickable — so a later click could be paid at a multiplier beyond
        where the round actually stopped. Now the round is cancelled and refunded.
        """
        self._seed_wallet(100)
        interaction = _FakeInteraction(fail_edits=1)

        with mock.patch.object(social_module.random, "random", return_value=0.5), \
             mock.patch.object(social_module.random, "expovariate", return_value=1.5), \
             mock.patch.object(social_module.asyncio, "sleep", new=_async_noop):
            await self._crash(interaction, 100)

        self.assertEqual(_wallet(USER), 100, "the stake was not refunded")
        self.assertEqual(interaction.card.last_title, "🚀 Launch Aborted")
        self.assertTrue(interaction.card.edits, "the player was never told")


if __name__ == "__main__":
    unittest.main()
