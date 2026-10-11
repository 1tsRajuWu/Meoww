"""`/eco-admin` must refuse anyone without Administrator — publicly.

`/eco-admin set` once had no administrator gate at all: only the owner check,
which the bot owner passes in *every* server. A non-admin could rewrite any
server's balances, and the refusal (where one existed) was ephemeral, so the
abuse read as success to everyone watching. These tests pin both halves: no
admin means no effect, and the denial is a visible same-channel message.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from discord import app_commands

from cogs import economy as economy_module
from tests.support import TempDatabaseMixin
from utils import db

GUILD = 5757


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


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


def _interaction(uid, *, admin, owner_id, bot_owner=False):
    user = SimpleNamespace(
        id=uid,
        display_name=f"User{uid}",
        mention=f"<@{uid}>",
        guild_permissions=SimpleNamespace(administrator=admin),
    )
    interaction = SimpleNamespace(
        user=user,
        guild=SimpleNamespace(id=GUILD, owner_id=owner_id),
        response=_FakeResponse(),
        channel=_FakeChannel(),
        followup=_FakeFollowup(),
        client=SimpleNamespace(is_owner=_make_is_owner(bot_owner)),
    )
    return interaction


def _make_is_owner(value):
    async def _is_owner(*_a, **_k):
        return value

    return _is_owner


def _member(uid):
    return SimpleNamespace(id=uid, display_name=f"User{uid}", mention=f"<@{uid}>")


def _balances(uid):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT wallet, bank FROM economy WHERE user_id=? AND guild_id=?",
            (uid, GUILD),
        ).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)
    finally:
        conn.close()


def _put(uid, wallet, bank=0):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?, ?, ?, ?)",
            (uid, GUILD, wallet, bank),
        )
        conn.commit()
    finally:
        conn.close()


async def _noop(*_a, **_k):
    return None


class EcoAdminRequiresAdmin(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("eco-admin-perms.db")
        economy_module.DB_PATH = path
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()

    def _public_denials(self, interaction):
        where = list(interaction.response.messages) + list(interaction.channel.messages)
        return [m for m in where if (m.get("content") or "").startswith("❌")]

    async def test_set_by_non_admin_is_refused_and_changes_nothing(self):
        _put(21, 100, 50)
        cog = economy_module.Economy(bot=None)
        interaction = _interaction(7, admin=False, owner_id=999)

        with mock.patch.object(economy_module, "send_card", new=_noop):
            await economy_module.Economy.setmoney.callback(cog, interaction, _member(21), 99_999)

        self.assertEqual(_balances(21), (100, 50))
        denials = self._public_denials(interaction)
        self.assertTrue(denials, "non-admin set must be refused")
        for denial in denials:
            self.assertFalse(denial.get("ephemeral"), "denial must be visible in-channel, not ephemeral")

    async def test_set_by_bot_owner_without_server_admin_is_refused(self):
        _put(22, 100, 0)
        cog = economy_module.Economy(bot=None)
        interaction = _interaction(7, admin=False, owner_id=999, bot_owner=True)

        with mock.patch.object(economy_module, "send_card", new=_noop):
            await economy_module.Economy.setmoney.callback(cog, interaction, _member(22), 99_999)

        self.assertEqual(_balances(22), (100, 0))

    async def test_set_by_admin_who_is_not_owner_is_refused(self):
        _put(23, 100, 0)
        cog = economy_module.Economy(bot=None)
        interaction = _interaction(8, admin=True, owner_id=999)

        with mock.patch.object(economy_module, "send_card", new=_noop):
            await economy_module.Economy.setmoney.callback(cog, interaction, _member(23), 99_999)

        self.assertEqual(_balances(23), (100, 0))
        self.assertTrue(self._public_denials(interaction))

    async def test_set_by_server_owner_with_admin_still_works(self):
        _put(24, 100, 50)
        cog = economy_module.Economy(bot=None)
        interaction = _interaction(9, admin=True, owner_id=9)

        with mock.patch.object(economy_module, "send_card", new=_noop):
            await economy_module.Economy.setmoney.callback(cog, interaction, _member(24), 99_999)

        self.assertEqual(_balances(24), (99_999, 50))

    async def test_reset_by_non_admin_changes_nothing(self):
        _put(25, 400, 100)
        cog = economy_module.Economy(bot=None)
        interaction = _interaction(7, admin=False, owner_id=999)

        with mock.patch.object(economy_module, "send_card", new=_noop):
            await economy_module.Economy.resetmoney.callback(cog, interaction, _member(25))

        self.assertEqual(_balances(25), (400, 100))
        self.assertTrue(self._public_denials(interaction))

    async def test_add_by_non_admin_grants_nothing(self):
        _put(26, 10, 0)
        cog = economy_module.Economy(bot=None)
        interaction = _interaction(7, admin=False, owner_id=999)

        with mock.patch.object(economy_module, "send_card", new=_noop):
            await economy_module.Economy.addmoney.callback(cog, interaction, _member(26), 5_000)

        self.assertEqual(_balances(26), (10, 0))
        self.assertTrue(self._public_denials(interaction))

    async def test_command_error_handler_posts_public_denial(self):
        cog = economy_module.Economy(bot=None)
        interaction = _interaction(7, admin=False, owner_id=999)
        error = app_commands.MissingPermissions(["administrator"])

        await cog.eco_admin_perm_error(interaction, error)

        denials = self._public_denials(interaction)
        self.assertTrue(denials, "decorator-level denial must land in-channel")
        for denial in denials:
            self.assertFalse(denial.get("ephemeral"))

    async def test_set_and_reset_carry_the_admin_check(self):
        for command in (
            economy_module.Economy.setmoney,
            economy_module.Economy.resetmoney,
        ):
            checks = list(getattr(command, "checks", []))
            self.assertTrue(
                any(getattr(check, "__name__", "") == "has_permissions_check" for check in checks)
                or any("has_permissions" in repr(check) for check in checks),
                f"{command.name} must carry has_permissions(administrator=True)",
            )


if __name__ == "__main__":
    unittest.main()
