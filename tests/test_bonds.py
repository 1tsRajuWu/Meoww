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
        self.sent_dms = []

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

    async def send(self, content=None, **kwargs):
        self.sent_dms.append({"content": content, **kwargs})


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


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _FakeInteraction:
    def __init__(self, user, guild):
        self.user = user
        self.guild = guild
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
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
        await self._propose(self.raj, self.ice, "sibling")
        await self._accept(self.raj, self.ice, "sibling")
        interaction = await self._propose(self.raj, self.ice, "dating")
        self.assertIn("Family", interaction.response.messages[-1]["content"])
        kinds = {r[2] for r in _bond_rows()}
        self.assertEqual(kinds, {"sibling"})

    async def test_dating_own_twin_invokes_loyalty(self):
        await self._propose(self.raj, self.ice, "twin")
        await self._accept(self.raj, self.ice, "twin")
        interaction = await self._propose(self.raj, self.ice, "dating")
        self.assertIn("twin", interaction.response.messages[-1]["content"].lower())
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

    async def test_propose_names_each_member_once(self):
        interaction = await self._propose(self.raj, self.ice, "twin")
        sent = interaction.response.messages[-1]
        embed = sent["embed"]
        self.assertEqual(embed.description.count("<@1>"), 1)
        self.assertEqual(embed.description.count("<@2>"), 1)

    async def test_propose_tags_the_target(self):
        interaction = await self._propose(self.raj, self.ice, "dating")
        sent = interaction.response.messages[-1]
        self.assertEqual(sent["content"], "<@2>")
        allowed = sent.get("allowed_mentions")
        self.assertIsNotNone(allowed)
        self.assertIn(self.ice, getattr(allowed, "users", []))

    async def test_accept_pings_the_proposer(self):
        await self._propose(self.raj, self.ice, "dating")
        _view, interaction = await self._accept(self.raj, self.ice, "dating")
        pings = interaction.followup.messages
        self.assertTrue(pings, "accept must notify the proposer")
        self.assertIn("<@1>", pings[-1]["content"])
        allowed = pings[-1].get("allowed_mentions")
        self.assertIn(self.raj, getattr(allowed, "users", []))

    async def test_decline_pings_the_proposer(self):
        view = social_module.BondView(self.raj, self.ice, "dating")
        interaction = _FakeInteraction(self.ice, self.guild)
        await view.decline.callback(interaction)
        self.assertTrue(interaction.followup.messages)
        self.assertIn("<@1>", interaction.followup.messages[-1]["content"])

    async def test_proposals_and_breakups_are_rate_limited(self):
        from discord.app_commands.errors import CommandOnCooldown

        async def _use_twice(command):
            import datetime

            for _ in range(2):
                interaction = _FakeInteraction(self.raj, self.guild)
                interaction.guild_id = GUILD
                interaction.created_at = datetime.datetime.now(datetime.timezone.utc)
                for check in command.checks:
                    result = check(interaction)
                    if hasattr(result, "__await__"):
                        await result

        cog = self.cog
        for command in (cog.marry, cog.divorce, cog.bond_propose, cog.break_bond):
            with self.assertRaises(
                CommandOnCooldown, msg=f"/{command.name} can be spammed"
            ):
                await _use_twice(command)

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

    async def test_crush_confession_is_secret(self):
        interaction = await self._propose(self.raj, self.ice, "crush")
        sent = interaction.response.messages[-1]
        self.assertTrue(sent.get("ephemeral"), "crush must never be public")
        self.assertNotIn("view", sent, "no ceremony for a secret")
        self.assertNotIn("embed", sent, "no card for a secret")
        rows = {(r[0], r[1], r[2]) for r in _bond_rows()}
        self.assertIn((1, 2, "crush"), rows)
        self.assertIn((2, 1, "crush"), rows)
        self.assertEqual(self.guild.roles, [], "a secret gets no role")
        self.assertEqual(self.ice.sent_dms, [], "the target must never know")

    async def test_crush_on_taken_target_records_and_warns_crusher(self):
        await self._propose(self.zed, self.ice, "dating")
        await self._accept(self.zed, self.ice, "dating")
        interaction = await self._propose(self.raj, self.ice, "crush")
        sent = interaction.response.messages[-1]
        self.assertTrue(sent.get("ephemeral"))
        self.assertIn("taken", (sent.get("content") or "").lower())
        self.assertTrue(any(r[2] == "crush" and r[0] == 1 for r in _bond_rows()))
        self.assertEqual(self.ice.sent_dms, [])

    async def test_taken_crusher_notifies_partner_not_target(self):
        await self._propose(self.raj, self.zed, "dating")
        await self._accept(self.raj, self.zed, "dating")
        xeno = self.guild.add(_FakeMember(4, "XENO"))
        interaction = await self._propose(self.raj, xeno, "crush")
        sent = interaction.response.messages[-1]
        self.assertTrue(sent.get("ephemeral"))
        # The partner hears; the crush target hears nothing and is unnamed.
        self.assertEqual(len(self.zed.sent_dms), 1)
        dm = self.zed.sent_dms[0]["content"]
        self.assertNotIn("XENO", dm)
        self.assertEqual(xeno.sent_dms, [])
        self.assertTrue(any(r[2] == "crush" and r[0] == 1 for r in _bond_rows()))

    async def test_dating_notifies_pining_crusher_and_closes_it(self):
        await self._propose(self.zed, self.ice, "crush")
        await self._propose(self.raj, self.ice, "dating")
        await self._accept(self.raj, self.ice, "dating")
        self.assertEqual(len(self.zed.sent_dms), 1)
        dm = self.zed.sent_dms[0]["content"]
        self.assertIn("ICE", dm)
        kinds = {(r[0], r[2]) for r in _bond_rows()}
        self.assertNotIn((3, "crush"), kinds)
        self.assertIn((1, "dating"), kinds)

    async def test_new_partner_learns_of_stray_crushes(self):
        xeno = self.guild.add(_FakeMember(4, "XENO"))
        await self._propose(self.raj, xeno, "crush")
        # RAJ is free here, so no partner news yet; now he dates ICE.
        self.assertEqual(self.ice.sent_dms, [])
        await self._propose(self.raj, self.ice, "dating")
        await self._accept(self.raj, self.ice, "dating")
        self.assertEqual(len(self.ice.sent_dms), 1)
        dm = self.ice.sent_dms[0]["content"]
        self.assertNotIn("XENO", dm)
        kinds = {(r[0], r[2]) for r in _bond_rows()}
        self.assertNotIn((1, "crush"), kinds)
        self.assertIn((1, "dating"), kinds)

    async def test_mutual_crushes_becoming_official_make_no_news(self):
        await self._propose(self.raj, self.ice, "crush")
        await self._propose(self.ice, self.raj, "crush")
        await self._propose(self.raj, self.ice, "dating")
        await self._accept(self.raj, self.ice, "dating")
        self.assertEqual(self.raj.sent_dms, [])
        self.assertEqual(self.ice.sent_dms, [])
        kinds = {(r[0], r[1], r[2]) for r in _bond_rows()}
        self.assertIn((1, 2, "dating"), kinds)
        self.assertFalse(any(r[2] == "crush" for r in kinds))

    async def test_twins_cannot_date_or_marry(self):
        await self._propose(self.raj, self.ice, "twin")
        await self._accept(self.raj, self.ice, "twin")
        dating = await self._propose(self.raj, self.zed, "dating")
        self.assertIn("twin", dating.response.messages[-1]["content"].lower())
        bond = await self._propose(self.zed, self.raj, "dating")
        self.assertIn("twin", bond.response.messages[-1]["content"].lower())
        interaction = _FakeInteraction(self.raj, self.guild)
        await social_module.Social.marry.callback(self.cog, interaction, self.zed)
        self.assertIn("twin", interaction.response.messages[-1]["content"].lower())
        interaction2 = _FakeInteraction(self.raj, self.guild)
        await social_module.Social.marry.callback(self.cog, interaction2, self.ice)
        self.assertIn("Family", interaction2.response.messages[-1]["content"])

    async def test_twin_demands_a_free_heart(self):
        await self._propose(self.raj, self.zed, "dating")
        await self._accept(self.raj, self.zed, "dating")
        taken = await self._propose(self.raj, self.ice, "twin")
        self.assertIn("whole heart", taken.response.messages[-1]["content"])

    async def test_list_hides_other_peoples_crushes(self):
        await self._propose(self.raj, self.ice, "crush")
        seen = {}

        async def _capture(target, **kwargs):
            seen["desc"] = kwargs.get("embed").description

        snooping = _FakeInteraction(self.zed, self.guild)
        with mock.patch.object(social_module, "send_card", new=_capture):
            await social_module.Social.bond_list.callback(self.cog, snooping, self.raj)
        self.assertNotIn("Crush", seen.get("desc", ""))
        own = _FakeInteraction(self.raj, self.guild)
        with mock.patch.object(social_module, "send_card", new=_capture):
            await social_module.Social.bond_list.callback(self.cog, own, None)
        self.assertIn("Crush", seen.get("desc", ""))

    async def test_breaking_a_crush_stays_secret(self):
        await self._propose(self.raj, self.ice, "crush")
        calls = []

        async def _loud(target, **kwargs):
            calls.append(kwargs)

        interaction = _FakeInteraction(self.raj, self.guild)
        with mock.patch.object(social_module, "send_card", new=_loud):
            await social_module.Social.break_bond.callback(
                self.cog, interaction, _choice("crush"))
        self.assertEqual(calls, [], "no public card for a broken secret")
        self.assertTrue(interaction.response.messages[-1].get("ephemeral"))
        self.assertEqual(_bond_rows(), [])

    async def test_homegirl_homeboy_coexist_with_own_roles(self):
        await self._propose(self.raj, self.ice, "homegirl")
        await self._accept(self.raj, self.ice, "homegirl")
        await self._propose(self.raj, self.zed, "homeboy")
        await self._accept(self.raj, self.zed, "homeboy")
        kinds = {(r[0], r[2]) for r in _bond_rows()}
        self.assertIn((1, "homegirl"), kinds)
        self.assertIn((1, "homeboy"), kinds)
        names = sorted(r.name for r in self.guild.roles)
        self.assertTrue(any("🩷" in n for n in names))
        self.assertTrue(any("🩵" in n for n in names))

    async def test_proposals_last_an_hour(self):
        self.assertEqual(social_module.BondView(self.raj, self.ice, "dating").timeout, 3600)
        self.assertEqual(social_module.MarriageView(self.raj, self.ice).timeout, 3600)

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
