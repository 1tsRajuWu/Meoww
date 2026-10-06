"""`/eco-admin remove` must never claim it took coins that were not there.

Found by a static pass over the tree: the command debited with
``update_wallet(-amount)``, which clamps at zero — so removing 10,000 coins from
a wallet holding 50 said it had removed 10,000.

This is the live half of ``tests/test_starboard_and_admin.py``; the starboard
half went to ``deleted_features/tests/test_starboard.py`` with the cog.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import economy as economy_module
from tests.support import TempDatabaseMixin
from utils import db

GUILD = 4242


def _wallet(user_id: int) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (user_id, GUILD)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _put(user_id: int, wallet: int) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?, ?, ?, 0)",
            (user_id, GUILD, wallet),
        )
        conn.commit()
    finally:
        conn.close()


class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _Member:
    def __init__(self, uid, wallet):
        self.id = uid
        self.display_name = f"User{uid}"
        self.mention = f"<@{uid}>"
        self._wallet = wallet

    @property
    def roles(self):
        return []


class _Interaction:
    def __init__(self, uid=1):
        self.user = SimpleNamespace(id=uid, display_name=f"Admin{uid}")
        self.guild = SimpleNamespace(id=GUILD, owner_id=999)   # someone else owns it
        self.response = _FakeResponse()
        self.client = SimpleNamespace(is_owner=_async_false)


async def _async_false(*_a, **_k):
    return False


async def _async_noop(*_a, **_k):
    return None


class AdminRemoveNeverLies(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("admin-remove.db")
        economy_module.DB_PATH = path
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def _remove(self, member, amount):
        cog = economy_module.Economy(bot=None)
        interaction = _Interaction()
        with mock.patch.object(economy_module, "send_card", new=_async_noop):
            await economy_module.Economy.removemoney.callback(cog, interaction, member, amount)
        return interaction

    async def test_removing_more_than_the_wallet_holds_takes_only_what_is_there(self):
        _put(7, 50)

        await self._remove(_Member(7, 50), 10_000)

        self.assertEqual(_wallet(7), 0, "clamping must not leave a hidden balance")

    async def test_a_wallet_with_no_coins_is_refused(self):
        _put(8, 0)

        interaction = await self._remove(_Member(8, 0), 100)

        self.assertEqual(_wallet(8), 0)
        self.assertIn("no coins", interaction.response.messages[-1]["content"])

    async def test_a_full_removal_moves_exactly_the_requested_amount(self):
        _put(9, 500)

        await self._remove(_Member(9, 500), 200)

        self.assertEqual(_wallet(9), 300)


if __name__ == "__main__":
    unittest.main()
