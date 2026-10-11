"""`/resetlevels` must refuse anyone without Administrator — publicly.

Same hole `/eco-admin set` had: no administrator gate, only an owner check the
bot owner passes in *every* server. A non-admin could wipe any server's levels,
and the refusal was ephemeral. Both gates now apply and the denial lands
visibly in the channel it was tried in.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import leveling as leveling_module
from tests.support import TempDatabaseMixin
from utils import db

GUILD = 6868


class _FakeResponse:
    def __init__(self):
        self.messages = []
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})
        self.done = True


class _FakeChannel:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


def _interaction(uid, *, admin, owner_id, bot_owner=False):
    async def _is_owner(*_a, **_k):
        return bot_owner

    return SimpleNamespace(
        user=SimpleNamespace(
            id=uid,
            display_name=f"User{uid}",
            mention=f"<@{uid}>",
            guild_permissions=SimpleNamespace(administrator=admin),
        ),
        guild=SimpleNamespace(id=GUILD, owner_id=owner_id),
        channel=_FakeChannel(),
        response=_FakeResponse(),
        client=SimpleNamespace(is_owner=_is_owner),
    )


def _member(uid):
    return SimpleNamespace(id=uid, display_name=f"User{uid}", mention=f"<@{uid}>")


def _row(uid):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT xp, level FROM user_levels WHERE user_id=? AND guild_id=?",
            (uid, GUILD),
        ).fetchone()
    finally:
        conn.close()


def _put(uid, xp=1_000, level=3):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO user_levels (guild_id, user_id, xp, level) VALUES (?, ?, ?, ?)",
            (GUILD, uid, xp, level),
        )
        conn.commit()
    finally:
        conn.close()


async def _noop(*_a, **_k):
    return None


class ResetlevelsRequiresAdmin(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("resetlevels-perms.db")
        leveling_module.DB_PATH = path
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()

    def _denials(self, interaction):
        where = list(interaction.response.messages) + list(interaction.channel.messages)
        return [m for m in where if (m.get("content") or "").startswith("❌")]

    async def test_non_admin_cannot_reset_a_member(self):
        _put(31)
        cog = leveling_module.Leveling(bot=None)
        interaction = _interaction(7, admin=False, owner_id=999)

        with mock.patch.object(leveling_module, "send_card", new=_noop):
            await leveling_module.Leveling.resetlevels.callback(cog, interaction, _member(31))

        self.assertIsNotNone(_row(31), "levels must survive a non-admin attempt")
        denials = self._denials(interaction)
        self.assertTrue(denials)
        for denial in denials:
            self.assertFalse(denial.get("ephemeral"))

    async def test_bot_owner_without_server_admin_cannot_reset(self):
        _put(32)
        cog = leveling_module.Leveling(bot=None)
        interaction = _interaction(7, admin=False, owner_id=999, bot_owner=True)

        with mock.patch.object(leveling_module, "send_card", new=_noop):
            await leveling_module.Leveling.resetlevels.callback(cog, interaction, _member(32))

        self.assertIsNotNone(_row(32))

    async def test_non_admin_cannot_reset_all(self):
        _put(33)
        _put(34)
        cog = leveling_module.Leveling(bot=None)
        interaction = _interaction(7, admin=False, owner_id=999)

        with mock.patch.object(leveling_module, "send_card", new=_noop):
            await leveling_module.Leveling.resetlevels.callback(
                cog, interaction, None, reset_all=True
            )

        self.assertIsNotNone(_row(33))
        self.assertIsNotNone(_row(34))

    async def test_admin_non_owner_is_still_refused(self):
        _put(35)
        cog = leveling_module.Leveling(bot=None)
        interaction = _interaction(8, admin=True, owner_id=999)

        with mock.patch.object(leveling_module, "send_card", new=_noop):
            await leveling_module.Leveling.resetlevels.callback(cog, interaction, _member(35))

        self.assertIsNotNone(_row(35))
        self.assertTrue(self._denials(interaction))

    async def test_server_owner_with_admin_still_works(self):
        _put(36)
        cog = leveling_module.Leveling(bot=None)
        interaction = _interaction(9, admin=True, owner_id=9)

        with mock.patch.object(leveling_module, "send_card", new=_noop):
            await leveling_module.Leveling.resetlevels.callback(cog, interaction, _member(36))

        self.assertIsNone(_row(36))

    async def test_resetlevels_carries_the_admin_check(self):
        command = leveling_module.Leveling.resetlevels
        checks = list(getattr(command, "checks", []))
        self.assertTrue(
            any("has_permissions" in repr(check) for check in checks),
            "resetlevels must carry has_permissions(administrator=True)",
        )


if __name__ == "__main__":
    unittest.main()
