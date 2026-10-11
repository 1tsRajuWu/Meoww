"""Relationship bonds (dating, besties, twins & more) beside `/marry`.

A bond is a proposal ceremony, a pair role and a breakup flow keyed by kind —
the same shape as marriage, without the wedding. These tests pin the rules
that keep the system coherent: one bond per kind, no dating the married (or
married the dating), no dating family, and marrying your bf/gf absorbs the
dating bond instead of stacking a second role.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import social as social_module
from tests.support import TempDatabaseMixin
from utils import db

GUILD = 7979


class _FakeRole:
    def __init__(self, name, colour=0xFF6B81):
        self.name = name
        self.colour = SimpleNamespace(value=colour)
        self.position = 1
        self.members = []
        self.deleted = False

    @property
    def mention(self):
        return f"@{self.name}"

    async def edit(self, **kwargs):
        if "position" in kwargs:
            self.position = kwargs["position"]

    async def delete(self, **kwargs):
        self.deleted = True


class _FakeMember:
    def __init__(self, uid, name=None, bot=False):
        self.id = uid
        self.name = name or f"User{uid}"
        self.display_name = self.name
        self.mention = f"<@{uid}>"
        self.bot = bot
        self.roles = []

    async def add_roles(self, role, **kwargs):
        if role not in self.roles:
            self.roles.append(role)
        if self not in role.members:
            role.members.append(self)

    async def remove_roles(self, role, **kwargs):
        if role in self.roles:
            self.roles.remove(role)
        if self in role.members:
            role.members.remove(self)


class _FakeGuild:
    def __init__(self, gid=GUILD):
        self.id = gid
        self.roles = []
        self._members = {}
        bot_top = SimpleNamespace(position=10)
        self.me = SimpleNamespace(top_role=bot_top, guild_permissions=SimpleNamespace(manage_roles=True))

    def add(self, member):
        self._members[member.id] = member
        return member

    def get_member(self, uid):
        return self._members.get(uid)

    async def fetch_member(self, uid):
        member = self._members.get(uid)
        if member is None:
            raise Exception("NotFound")
        return member

    async def create_role(self, *, name, colour=None, hoist=False, mentionable=False, reason=None):
        role = _FakeRole(name, colour.value if colour else 0)
        self.roles.append(role)
        return role


class _FakeResponse:
    def __init__(self):
        self.messages = []
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})
        self.done = True

    async def edit_message(self, **kwargs):
        self.messages.append({"edit": True, **kwargs})
        self.done = True


class _FakeInteraction:
    def __init__(self, user, guild):
        self.user = user
        self.guild = guild
        self.response = _FakeResponse()
        self.channel = SimpleNamespace(send=_noop_send)
        self.client = SimpleNamespace()
        self._original = SimpleNamespace(id=1)

    async def original_response(self):
        return self._original


async def _noop_send(*_a, **_k):
    return None


async def _noop(*_a, **_k):
    return None


def _choice(value):
    return SimpleNamespace(value=value)


def _bond_rows(guild_id=GUILD):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT user1_id, user2_id, kind FROM bonds WHERE guild_id=?", (guild_id,)
        ).fetchall()
    finally:
        conn.close()


def _marry(a_id, b_id, guild_id=GUILD):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO marriages (guild_id, user1_id, user2_id, married_at) VALUES (?, ?, ?, ?)",
            (guild_id, a_id, b_id, "now"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO marriages (guild_id, user1_id, user2_id, married_at) VALUES (?, ?, ?, ?)",
            (guild_id, b_id, a_id, "now"),
        )
        conn.commit()
    finally:
        conn.close()


class Bonds(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("bonds.db")
        social_module.DB_PATH = path
        await db.setup()
        self.guild = _FakeGuild()
        self.raj = self.guild.add(_FakeMember(1, "RAJ"))
        self.ice = self.guild.add(_FakeMember(2, "ICE"))
        self.zed = self.guild.add(_FakeMember(3, "ZED"))
        self.cog = social_module.Social(bot=None)

    async def asyncTearDown(self):
        self.release_temp_database()

    async def _propose(self, proposer, target, kind):
        interaction = _FakeInteraction(proposer, self.guild)
        await social_module.Social.bond_propose.callback(
            self.cog, interaction, target, _choice(kind)
        )
        return interaction

    async def _accept(self, proposer, target, kind):
        view = social_module.BondView(proposer, target, kind)
        interaction = _FakeInteraction(target, self.guild)
        with mock.patch.object(social_module, "send_card", new=_noop):
            await view.accept.callback(interaction)
        return view, interaction

    async def test_cannot_propose_to_self(self):
        interaction = await self._propose(self.raj, self.raj, "dating")
        self.assertIn("own", interaction.response.messages[-1]["content"])

    async def test_cannot_propose_to_a_bot(self):
        bot = self.guild.add(_FakeMember(9, "Bot", bot=True))
        interaction = await self._propose(self.raj, bot, "dating")
        self.assertIn("Bots", interaction.response.messages[-1]["content"])

    async def test_dating_is_refused_while_married(self):
        _marry(1, 3)
        interaction = await self._propose(self.raj, self.ice, "dating")
        self.assertIn("married", interaction.response.messages[-1]["content"].lower())
        self.assertEqual(_bond_rows(), [])

    async def test_dating_family_is_refused(self):
        await self._propose(self.raj, self.ice, "twin")
        await self._accept(self.raj, self.ice, "twin")
        interaction = await self._propose(self.raj, self.ice, "dating")
        self.assertIn("Family", interaction.response.messages[-1]["content"])
        kinds = {r[2] for r in _bond_rows()}
        self.assertEqual(kinds, {"twin"})

    async def test_twin_is_refused_while_dating(self):
        await self._propose(self.raj, self.ice, "dating")
        await self._accept(self.raj, self.ice, "dating")
        interaction = await self._propose(self.raj, self.ice, "twin")
        self.assertIn("dating", interaction.response.messages[-1]["content"].lower())

    async def test_accept_writes_both_directions_and_roles_both(self):
        await self._propose(self.raj, self.ice, "dating")
        await self._accept(self.raj, self.ice, "dating")
        rows = {(r[0], r[1], r[2]) for r in _bond_rows()}
        self.assertIn((1, 2, "dating"), rows)
        self.assertIn((2, 1, "dating"), rows)
        roles = [r for r in self.guild.roles if "❤" in r.name]
        self.assertEqual(len(roles), 1)
        self.assertIn(roles[0], self.raj.roles)
        self.assertIn(roles[0], self.ice.roles)
        self.assertEqual(roles[0].position, 9)

    async def test_decline_writes_nothing(self):
        view = social_module.BondView(self.raj, self.ice, "bestie")
        interaction = _FakeInteraction(self.ice, self.guild)
        await view.decline.callback(interaction)
        self.assertEqual(_bond_rows(), [])
        self.assertEqual(self.guild.roles, [])

    async def test_second_bond_of_same_kind_is_refused(self):
        await self._propose(self.raj, self.ice, "bestie")
        await self._accept(self.raj, self.ice, "bestie")
        interaction = await self._propose(self.raj, self.zed, "bestie")
        self.assertIn("already have", interaction.response.messages[-1]["content"])

    async def test_break_removes_rows_and_deletes_role(self):
        await self._propose(self.raj, self.ice, "bestie")
        await self._accept(self.raj, self.ice, "bestie")
        role = next(r for r in self.guild.roles if "🤝" in r.name)
        interaction = _FakeInteraction(self.raj, self.guild)
        with mock.patch.object(social_module, "send_card", new=_noop):
            await social_module.Social.break_bond.callback(self.cog, interaction, _choice("bestie"))
        self.assertEqual(_bond_rows(), [])
        self.assertNotIn(role, self.raj.roles)
        self.assertNotIn(role, self.ice.roles)
        self.assertTrue(role.deleted)

    async def test_break_without_kind_asks_when_several(self):
        await self._propose(self.raj, self.ice, "bestie")
        await self._accept(self.raj, self.ice, "bestie")
        await self._propose(self.raj, self.ice, "rival")
        await self._accept(self.raj, self.ice, "rival")
        interaction = _FakeInteraction(self.raj, self.guild)
        with mock.patch.object(social_module, "send_card", new=_noop):
            await social_module.Social.break_bond.callback(self.cog, interaction, None)
        self.assertEqual(len(_bond_rows()), 4)
        self.assertIn("several", interaction.response.messages[-1]["content"])

    async def test_break_without_kind_breaks_the_only_one(self):
        await self._propose(self.raj, self.ice, "crush")
        await self._accept(self.raj, self.ice, "crush")
        interaction = _FakeInteraction(self.raj, self.guild)
        with mock.patch.object(social_module, "send_card", new=_noop):
            await social_module.Social.break_bond.callback(self.cog, interaction, None)
        self.assertEqual(_bond_rows(), [])

    async def test_list_shows_bonds(self):
        await self._propose(self.raj, self.ice, "twin")
        await self._accept(self.raj, self.ice, "twin")
        seen = {}

        async def _capture(target, **kwargs):
            embed = kwargs.get("embed")
            seen["title"] = embed.title
            seen["desc"] = embed.description

        interaction = _FakeInteraction(self.raj, self.guild)
        with mock.patch.object(social_module, "send_card", new=_capture):
            await social_module.Social.bond_list.callback(self.cog, interaction, None)
        self.assertIn("Twin", seen["desc"])
        self.assertIn("ICE", seen["desc"])

    async def test_marry_is_refused_while_dating_someone_else(self):
        await self._propose(self.raj, self.zed, "dating")
        await self._accept(self.raj, self.zed, "dating")
        interaction = _FakeInteraction(self.raj, self.guild)
        with mock.patch.object(social_module, "send_card", new=_noop):
            await social_module.Social.marry.callback(self.cog, interaction, self.ice)
        self.assertIn("dating", interaction.response.messages[-1]["content"].lower())

    async def test_marrying_dating_partner_upgrades_the_bond(self):
        await self._propose(self.raj, self.ice, "dating")
        await self._accept(self.raj, self.ice, "dating")
        dating_role = next(r for r in self.guild.roles if "❤" in r.name)
        view = social_module.MarriageView(self.raj, self.ice)
        interaction = _FakeInteraction(self.ice, self.guild)
        with mock.patch.object(social_module, "send_card", new=_noop):
            with mock.patch.object(social_module.gifs, "action_gif", return_value=None):
                await view.accept.callback(interaction)
        self.assertEqual(_bond_rows(), [])
        self.assertTrue(dating_role.deleted)
        couple_roles = [r for r in self.guild.roles if " x " in r.name and not r.deleted]
        self.assertEqual(len(couple_roles), 1)

    async def test_proposal_expiry_disables(self):
        view = social_module.BondView(self.raj, self.ice, "dating")
        view.message = SimpleNamespace(edit=_noop_edit())
        await view.on_timeout()
        self.assertTrue(all(item.disabled for item in view.children))

    async def test_bonds_heal_a_database_missing_the_table(self):
        conn = db.connect()
        try:
            conn.execute("DROP TABLE bonds")
            conn.commit()
        finally:
            conn.close()
        interaction = await self._propose(self.raj, self.ice, "dating")
        self.assertTrue(
            any("boyfriend" in (m.get("content") or "") for m in interaction.response.messages)
            or interaction.response.done,
            "propose must survive a database that predates the bonds table",
        )
        await self._accept(self.raj, self.ice, "dating")
        rows = {(r[0], r[1], r[2]) for r in _bond_rows()}
        self.assertIn((1, 2, "dating"), rows)

    async def test_propose_reads_in_a_single_connection(self):
        connects = []
        real_connect = social_module.aiosqlite.connect

        def counting(*args, **kwargs):
            connects.append(1)
            return real_connect(*args, **kwargs)

        with mock.patch.object(social_module.aiosqlite, "connect", new=counting):
            with mock.patch.object(social_module.gifs, "action_gif", return_value=None):
                await self._propose(self.raj, self.ice, "dating")
        # One connection for every check (five opens before). The GIF flies
        # alongside it, so it is not counted here.
        self.assertLessEqual(len(connects), 1)

    async def test_marry_reads_in_a_single_connection(self):
        connects = []
        real_connect = social_module.aiosqlite.connect

        def counting(*args, **kwargs):
            connects.append(1)
            return real_connect(*args, **kwargs)

        with mock.patch.object(social_module.aiosqlite, "connect", new=counting):
            with mock.patch.object(social_module.gifs, "action_gif", return_value=None):
                interaction = _FakeInteraction(self.raj, self.guild)
                await social_module.Social.marry.callback(self.cog, interaction, self.ice)
        self.assertLessEqual(len(connects), 1)

def _noop_edit():
    async def _edit(**_kwargs):
        return None

    return _edit


if __name__ == "__main__":
    unittest.main()
