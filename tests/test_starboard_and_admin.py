"""The starboard's reaction-remove path and the admin coin removal.

Both were found by a static pass over the tree after the previous round of fixes:

* ``on_raw_reaction_remove`` edited the card inside a bare ``except: pass`` and
  returned, so a card that had been deleted left the row pointing at a dead
  message forever (the add path self-heals; this one did not) and the stored
  ``stars_count`` was never refreshed.
* Retiring a card deleted its row even when the message itself could not be
  deleted, so the next star posted a duplicate next to the card that was still
  there.
* ``/eco-admin remove`` debited with ``update_wallet(-amount)``, which clamps at
  zero — the confirmation then claimed it removed coins that never existed.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from cogs import economy as economy_module
from cogs import starboard as starboard_module
from tests.support import TempDatabaseMixin
from utils import db

GUILD = 4242
ORIGIN = 100
BOARD = 200
MESSAGE = 333


def _row():
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT starboard_message_id, stars_count FROM starboard_posts WHERE original_message_id=?",
            (MESSAGE,),
        ).fetchone()
    finally:
        conn.close()


def _wallet(user_id: int) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (user_id, GUILD)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


class _Reaction:
    def __init__(self, emoji, count):
        self.emoji = emoji
        self.count = count


class _Msg:
    def __init__(self, mid=MESSAGE, stars=5):
        self.id = mid
        self.reactions = [_Reaction("⭐", stars)]
        self.content = "a starred message"
        self.attachments = []
        self.embeds = []
        self.created_at = discord.utils.utcnow()
        self.author = SimpleNamespace(display_name="Author", display_avatar=SimpleNamespace(url="https://x.invalid/a.png"))
        self.jump_url = "https://discord.invalid/jump"
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _Channel:
    def __init__(self, mid=MESSAGE, stars=5, missing=False, delete_error=None):
        self.id = 0            # set by _remove_star
        self.mention = "#origin"
        self.mid = mid
        self.stars = stars
        self.missing = missing
        self.delete_error = delete_error
        self.fetched = None

    async def fetch_message(self, _mid):
        if self.missing:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        msg = _Msg(self.mid, self.stars)
        if self.delete_error is not None:
            async def boom():
                raise self.delete_error
            msg.delete = boom
        self.fetched = msg
        return msg


class _Guild:
    def __init__(self, board, origin):
        self.id = GUILD
        self._channels = {BOARD: board, ORIGIN: origin}
        self.owner_id = 1

    def get_channel(self, cid):
        return self._channels.get(cid)


class _Bot:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, _gid):
        return self._guild

    def is_module_enabled(self, _gid, _mod):
        return True


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


class _StarboardCase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("starboard.db")
        starboard_module.DB_PATH = path
        economy_module.DB_PATH = path
        await db.setup()
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO starboard_settings (guild_id, channel_id, threshold) VALUES (?, ?, 3)",
                (GUILD, BOARD),
            )
            conn.execute(
                "INSERT OR REPLACE INTO starboard_posts (original_message_id, starboard_message_id, guild_id, stars_count)"
                " VALUES (?, ?, ?, ?)",
                (MESSAGE, 999, GUILD, 3),
            )
            conn.commit()
        finally:
            conn.close()

    async def asyncTearDown(self):
        self.release_temp_database()

    def _payload(self):
        return SimpleNamespace(
            emoji="⭐", guild_id=GUILD, channel_id=ORIGIN, message_id=MESSAGE
        )

    async def _remove_star(self, board_channel, origin_channel, sent):
        board_channel.id = BOARD
        origin_channel.id = ORIGIN
        guild = _Guild(board_channel, origin_channel)
        cog = starboard_module.Starboard(_Bot(guild))

        async def fake_send_card(_channel, **_kw):
            sent.append(_kw)
            return SimpleNamespace(id=12345)

        with mock.patch.object(starboard_module, "send_card", new=fake_send_card), \
             mock.patch.object(starboard_module, "edit_card", new=_async_noop):
            await starboard_module.Starboard.on_raw_reaction_remove(cog, self._payload())


async def _async_noop(*_a, **_k):
    return None


class RemoveResyncsTheCard(_StarboardCase):
    async def test_a_deleted_card_is_replaced_and_the_row_repointed(self):
        """The add path already recovered from a dead card; this path did not."""
        sent = []
        board = _Channel(mid=999, missing=True)      # the stored card is gone
        origin = _Channel(stars=5)                   # still above the threshold

        await self._remove_star(board, origin, sent)

        self.assertEqual(len(sent), 1, "the card was not re-posted")
        row = _row()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 12345, "the row still points at the dead message")
        self.assertEqual(row[1], 5, "the stored count was not refreshed")

    async def test_a_healthy_card_is_edited_and_its_count_refreshed(self):
        sent = []
        board = _Channel(mid=999)

        await self._remove_star(board, _Channel(stars=4), sent)

        self.assertEqual(sent, [], "a healthy card must not be duplicated")
        row = _row()
        self.assertEqual(row[0], 999)
        self.assertEqual(row[1], 4)

    async def test_falling_below_the_threshold_retires_the_card_and_its_row(self):
        sent = []
        board = _Channel(mid=999)

        await self._remove_star(board, _Channel(stars=2), sent)

        self.assertTrue(board.fetched.deleted, "the card was not removed")
        self.assertIsNone(_row(), "the row was not dropped")

    async def test_a_card_that_cannot_be_deleted_keeps_its_row(self):
        """Otherwise the next star posts a duplicate beside the surviving card."""
        sent = []
        forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no perms")
        board = _Channel(mid=999, delete_error=forbidden)

        await self._remove_star(board, _Channel(stars=1), sent)

        row = _row()
        self.assertIsNotNone(row, "the row was dropped while the card is still visible")
        self.assertEqual(row[0], 999)


class AdminRemoveNeverLies(_StarboardCase):
    async def _remove(self, member, amount):
        cog = economy_module.Economy(bot=None)
        interaction = _Interaction()
        with mock.patch.object(economy_module, "send_card", new=_async_noop):
            await economy_module.Economy.removemoney.callback(cog, interaction, member, amount)
        return interaction

    async def test_removing_more_than_the_wallet_holds_takes_only_what_is_there(self):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (7, ?, 50, 0)",
                (GUILD,),
            )
            conn.commit()
        finally:
            conn.close()

        await self._remove(_Member(7, 50), 10_000)

        self.assertEqual(_wallet(7), 0, "clamping must not leave a hidden balance")

    async def test_a_wallet_with_no_coins_is_refused(self):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (8, ?, 0, 0)",
                (GUILD,),
            )
            conn.commit()
        finally:
            conn.close()

        interaction = await self._remove(_Member(8, 0), 100)

        self.assertEqual(_wallet(8), 0)
        self.assertIn("no coins", interaction.response.messages[-1]["content"])

    async def test_a_full_removal_moves_exactly_the_requested_amount(self):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (9, ?, 500, 0)",
                (GUILD,),
            )
            conn.commit()
        finally:
            conn.close()

        await self._remove(_Member(9, 500), 200)

        self.assertEqual(_wallet(9), 300)


if __name__ == "__main__":
    unittest.main()
