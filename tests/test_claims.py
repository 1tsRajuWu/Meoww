"""The one-time claim: two ceilings, opt-in, and once per member per server.

Three layers, because any one alone would pass while the claim misbehaved:

* :class:`TheCeilings` drives ``utils/claims.claim_preview`` directly. It is
  pure, so the ceilings are pinned as arithmetic — including the two cases that
  matter most: a member already above the floor must be granted **nothing**, and
  no input at all may produce a grant above ``MAX_CLAIM_MONEY`` / ``MAX_CLAIM_XP``.
* :class:`TheClaimHappensOnce` drives the real write against a throwaway
  database, because a ceiling that is right but not enforced changes nothing.
* :class:`TheCommandAnswers` drives the real cog: the two refusals, the ladder
  the claim honours, and a ladder that fails without taking the claim with it.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import claims as claims_cog
from tests.support import TempDatabaseMixin
from utils import claims, db, levels

GUILD = 5_001
OTHER_GUILD = 5_002
MEMBER = 101
NEIGHBOUR = 202


def _scalar(sql: str, params: tuple = ()):
    conn = db.connect()
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _wallet(user_id: int, guild_id: int = GUILD) -> int:
    return _scalar(
        "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (user_id, guild_id)
    ) or 0


def _xp(user_id: int, guild_id: int = GUILD) -> int:
    return _scalar(
        "SELECT xp FROM user_levels WHERE guild_id=? AND user_id=?", (guild_id, user_id)
    ) or 0


def _level(user_id: int, guild_id: int = GUILD) -> int:
    return _scalar(
        "SELECT level FROM user_levels WHERE guild_id=? AND user_id=?", (guild_id, user_id)
    ) or 0


def _claims_row(user_id: int, guild_id: int = GUILD):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT money, xp, level FROM claim_grants WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
    finally:
        conn.close()


def _put(user_id: int, *, wallet: int = 0, xp: int = 0, level: int = 0, guild_id: int = GUILD):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?, ?, ?, 0)",
            (user_id, guild_id, wallet),
        )
        conn.execute(
            "INSERT OR REPLACE INTO user_levels (guild_id, user_id, xp, level) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, xp, level),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The pure half
# --------------------------------------------------------------------------- #
class TheCeilings(unittest.TestCase):
    def test_the_money_ceiling_is_never_exceeded_by_any_balance(self):
        for wallet in (-9_999, -1, 0, 1, claims.MAX_CLAIM_MONEY - 1,
                       claims.MAX_CLAIM_MONEY, claims.MAX_CLAIM_MONEY + 1, 10 ** 12):
            with self.subTest(wallet=wallet):
                grant = claims.claim_preview(wallet, 0)
                self.assertGreaterEqual(grant.money, 0)
                self.assertLessEqual(grant.money, claims.MAX_CLAIM_MONEY)

    def test_the_xp_ceiling_is_never_exceeded_by_any_holding(self):
        for xp in (-9_999, 0, 1, claims.MAX_CLAIM_XP - 1, claims.MAX_CLAIM_XP,
                   claims.MAX_CLAIM_XP + 1, levels.MAX_XP + 1, 2_407_979_200):
            with self.subTest(xp=xp):
                grant = claims.claim_preview(0, xp)
                self.assertGreaterEqual(grant.xp, 0)
                self.assertLessEqual(grant.xp, claims.MAX_CLAIM_XP)

    def test_the_xp_ceiling_is_the_curve_at_the_declared_level(self):
        """One ceiling, derived — not a level and an XP figure that can drift."""
        self.assertEqual(claims.MAX_CLAIM_XP, levels.total_xp_for_level(claims.MAX_CLAIM_LEVEL))
        self.assertLessEqual(claims.MAX_CLAIM_LEVEL, levels.MAX_LEVEL)
        self.assertLessEqual(claims.MAX_CLAIM_XP, levels.MAX_XP)

    def test_a_claim_never_reduces_what_a_member_holds(self):
        for wallet in (0, 100, claims.MAX_CLAIM_MONEY, 10 ** 9):
            for xp in (0, 5_000, claims.MAX_CLAIM_XP, levels.MAX_XP):
                with self.subTest(wallet=wallet, xp=xp):
                    grant = claims.claim_preview(wallet, xp)
                    self.assertGreaterEqual(wallet + grant.money, wallet)
                    self.assertGreaterEqual(levels.clamp_xp(xp + grant.xp), levels.clamp_xp(xp))

    def test_a_member_above_both_ceilings_has_nothing_to_claim(self):
        grant = claims.claim_preview(claims.MAX_CLAIM_MONEY + 1, claims.MAX_CLAIM_XP + 1)
        self.assertTrue(grant.empty)
        self.assertEqual((grant.money, grant.xp), (0, 0))

    def test_a_member_at_either_ceiling_is_top_up_to_and_not_past_it(self):
        at_money = claims.claim_preview(claims.MAX_CLAIM_MONEY, 0)
        self.assertEqual(at_money.money, 0)
        self.assertEqual(at_money.xp, claims.MAX_CLAIM_XP)
        at_xp = claims.claim_preview(0, claims.MAX_CLAIM_XP)
        self.assertEqual(at_xp.xp, 0)
        self.assertEqual(at_xp.money, claims.MAX_CLAIM_MONEY)

    def test_the_grant_lands_on_the_level_it_says_it_does(self):
        grant = claims.claim_preview(0, 0)
        self.assertEqual(grant.level, claims.MAX_CLAIM_LEVEL)
        self.assertEqual(
            levels.calculate_level_from_xp(grant.xp)[0], grant.level,
            "the level a claim reports must be the level its XP stands for",
        )

    def test_a_member_above_the_ceiling_keeps_their_own_level(self):
        """A claim is a floor: a member beyond it is not moved and not lowered."""
        beyond = levels.total_xp_for_level(claims.MAX_CLAIM_LEVEL + 10)
        grant = claims.claim_preview(0, beyond)
        self.assertEqual(grant.xp, 0)
        self.assertEqual(grant.level, levels.calculate_level_from_xp(beyond)[0])


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #
class TheClaimHappensOnce(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("claim.db")
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def test_the_latch_table_is_made_on_a_database_that_predates_it(self):
        """The rescued/restored file arrives without this table; the claim heals it."""
        conn = db.connect()
        try:
            conn.execute("DROP TABLE claim_grants")
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(
            _scalar("SELECT name FROM sqlite_master WHERE type='table' AND name='claim_grants'")
        )

        outcome = claims.claim_once(GUILD, MEMBER)

        self.assertTrue(outcome.granted)
        self.assertIsNotNone(
            _scalar("SELECT name FROM sqlite_master WHERE type='table' AND name='claim_grants'")
        )

    async def test_a_member_with_nothing_is_brought_up_to_both_ceilings(self):
        _put(MEMBER, wallet=0, xp=0)

        outcome = claims.claim_once(GUILD, MEMBER)

        self.assertTrue(outcome.granted)
        self.assertEqual(_wallet(MEMBER), claims.MAX_CLAIM_MONEY)
        self.assertEqual(_xp(MEMBER), claims.MAX_CLAIM_XP)
        self.assertEqual(_level(MEMBER), claims.MAX_CLAIM_LEVEL)

    async def test_a_member_with_no_rows_at_all_is_given_the_floor(self):
        """Nothing in the bot creates the rows for them: the claim must."""
        outcome = claims.claim_once(GUILD, MEMBER)

        self.assertTrue(outcome.granted)
        self.assertEqual(_wallet(MEMBER), claims.MAX_CLAIM_MONEY)
        self.assertEqual(_xp(MEMBER), claims.MAX_CLAIM_XP)

    async def test_a_member_below_the_floor_is_topped_up_only_to_it(self):
        _put(MEMBER, wallet=claims.MAX_CLAIM_MONEY - 40, xp=claims.MAX_CLAIM_XP - 40, level=24)

        claims.claim_once(GUILD, MEMBER)

        self.assertEqual(_wallet(MEMBER), claims.MAX_CLAIM_MONEY)
        self.assertEqual(_xp(MEMBER), claims.MAX_CLAIM_XP)

    async def test_the_second_claim_is_refused_and_pays_nothing_more(self):
        _put(MEMBER, wallet=0, xp=0)
        first = claims.claim_once(GUILD, MEMBER)
        self.assertTrue(first.granted)
        after_first = (_wallet(MEMBER), _xp(MEMBER), _level(MEMBER))
        latch = _claims_row(MEMBER)

        second = claims.claim_once(GUILD, MEMBER)

        self.assertEqual(second.status, claims.ALREADY_CLAIMED)
        self.assertFalse(second.granted)
        self.assertEqual((_wallet(MEMBER), _xp(MEMBER), _level(MEMBER)), after_first)
        self.assertEqual(_claims_row(MEMBER), latch, "the refusal rewrote the record")

    async def test_every_attempt_after_the_first_is_refused(self):
        _put(MEMBER, wallet=0, xp=0)
        claims.claim_once(GUILD, MEMBER)

        for attempt in range(3):
            with self.subTest(attempt=attempt):
                self.assertEqual(
                    claims.claim_once(GUILD, MEMBER).status, claims.ALREADY_CLAIMED
                )

    async def test_a_member_who_does_not_claim_is_untouched(self):
        _put(NEIGHBOUR, wallet=7, xp=11, level=1)

        claims.claim_once(GUILD, MEMBER)

        self.assertEqual(_wallet(NEIGHBOUR), 7)
        self.assertEqual(_xp(NEIGHBOUR), 11)
        self.assertEqual(_level(NEIGHBOUR), 1)
        self.assertIsNone(_claims_row(NEIGHBOUR))

    async def test_a_member_above_the_floor_keeps_their_one_chance(self):
        """Refusing somebody who has more costs them nothing — and gives nothing back."""
        _put(MEMBER, wallet=claims.MAX_CLAIM_MONEY + 5_000, xp=claims.MAX_CLAIM_XP + 5_000,
             level=claims.MAX_CLAIM_LEVEL + 3)

        refusal = claims.claim_once(GUILD, MEMBER)

        self.assertEqual(refusal.status, claims.NOTHING_TO_CLAIM)
        self.assertFalse(refusal.granted)
        self.assertIsNone(_claims_row(MEMBER), "a refusal must not spend the chance")
        self.assertEqual(_xp(MEMBER), claims.MAX_CLAIM_XP + 5_000)
        self.assertEqual(_level(MEMBER), claims.MAX_CLAIM_LEVEL + 3)

        # And once they are below it, the chance is still theirs.
        _put(MEMBER, wallet=0, xp=0, level=0)
        self.assertEqual(claims.claim_once(GUILD, MEMBER).status, claims.GRANTED)

    async def test_the_claimed_level_matches_the_claimed_xp(self):
        """The invariant the leveling repair exists for, kept by the writer."""
        _put(MEMBER, wallet=0, xp=0)

        outcome = claims.claim_once(GUILD, MEMBER)

        self.assertEqual(
            _level(MEMBER),
            levels.calculate_level_from_xp(outcome.grant.xp)[0],
        )

    async def test_claiming_here_does_not_spend_the_chance_there(self):
        """The two figures are per server, so the chance is per server too."""
        claims.claim_once(GUILD, MEMBER)

        elsewhere = claims.claim_once(OTHER_GUILD, MEMBER)

        self.assertTrue(elsewhere.granted)
        self.assertEqual(_wallet(MEMBER, OTHER_GUILD), claims.MAX_CLAIM_MONEY)

    async def test_the_record_says_what_was_handed_over(self):
        _put(MEMBER, wallet=claims.MAX_CLAIM_MONEY - 100, xp=0, level=0)

        outcome = claims.claim_once(GUILD, MEMBER)

        self.assertEqual(_claims_row(MEMBER), (100, claims.MAX_CLAIM_XP, claims.MAX_CLAIM_LEVEL))
        self.assertEqual(
            _claims_row(MEMBER), (outcome.grant.money, outcome.grant.xp, outcome.grant.level)
        )


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = 0

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    async def defer(self, *args, **kwargs):
        self.deferred += 1

    def is_done(self):
        return bool(self.messages) or bool(self.deferred)


class _FakeInteraction:
    def __init__(self, guild_id=GUILD, user_id=MEMBER):
        self.user = SimpleNamespace(id=user_id, display_name=f"User{user_id}")
        self.guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
        self.guild_id = guild_id
        self.response = _FakeResponse()


class _Role:
    def __init__(self, name):
        self.name = name


class _FakeLeveling:
    def __init__(self, roles=("Champion",), boom=False):
        self.roles = [_Role(name) for name in roles]
        self.boom = boom
        self.calls = []

    async def check_and_award_roles(self, guild, member, level):
        self.calls.append(level)
        if self.boom:
            raise RuntimeError("Discord said no")
        return self.roles


def _bot(leveling=None, leveling_on=True):
    return SimpleNamespace(
        get_cog=lambda name: leveling if name == "Leveling" else None,
        is_module_enabled=lambda guild_id, module: leveling_on,
    )


class TheCommandAnswers(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("claim-command.db")
        await db.setup()
        self.cards = []

        async def _record(target, *, embed=None, ephemeral=False, **kwargs):
            self.cards.append({"embed": embed, "ephemeral": ephemeral})

        self._patch = mock.patch.object(claims_cog, "send_card", new=_record)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    async def asyncTearDown(self):
        self.release_temp_database()

    def _last(self):
        return self.cards[-1] if self.cards else {}

    def _text(self) -> str:
        embed = self._last().get("embed")
        return " ".join(
            [str(embed.title or ""), str(embed.description or "")]
            + [f"{field.name} {field.value}" for field in embed.fields]
        )

    async def _claim(self, interaction, bot):
        cog = claims_cog.Claims(bot)
        await claims_cog.Claims.claim.callback(cog, interaction)
        return cog

    async def test_the_first_claim_publicly_names_what_was_added(self):
        interaction = _FakeInteraction()

        await self._claim(interaction, _bot())

        text = self._text()
        self.assertIn(f"{claims.MAX_CLAIM_MONEY:,}", text)
        self.assertIn(f"{claims.MAX_CLAIM_XP:,}", text)
        self.assertIn(f"Level {claims.MAX_CLAIM_LEVEL}", text)
        self.assertFalse(self._last()["ephemeral"], "the claim is the member's moment")
        self.assertEqual(interaction.response.deferred, 1)

    async def test_the_second_claim_is_answered_privately_and_pays_nothing(self):
        await self._claim(_FakeInteraction(), _bot())
        once = (_wallet(MEMBER), _xp(MEMBER))

        await self._claim(_FakeInteraction(), _bot())

        self.assertTrue(self._last()["ephemeral"])
        self.assertIn("once", self._text().lower())
        self.assertEqual((_wallet(MEMBER), _xp(MEMBER)), once)

    async def test_a_member_above_the_floor_is_told_their_chance_is_unused(self):
        _put(MEMBER, wallet=10 ** 6, xp=levels.MAX_XP, level=levels.MAX_LEVEL)

        await self._claim(_FakeInteraction(), _bot())

        self.assertTrue(self._last()["ephemeral"])
        self.assertIn("unused", self._text().lower())
        self.assertIsNone(_claims_row(MEMBER))

    async def test_it_refuses_outside_a_server(self):
        interaction = _FakeInteraction(guild_id=None)

        await self._claim(interaction, _bot())

        self.assertEqual(interaction.response.messages[-1]["ephemeral"], True)
        self.assertIn("/claim", interaction.response.messages[-1]["content"])
        self.assertEqual(_wallet(MEMBER), 0)

    async def test_the_level_ladder_is_honoured_when_that_system_is_on(self):
        leveling = _FakeLeveling(roles=("Champion",))

        await self._claim(_FakeInteraction(), _bot(leveling=leveling, leveling_on=True))

        self.assertEqual(leveling.calls, [claims.MAX_CLAIM_LEVEL])
        self.assertIn("Champion", self._text())

    async def test_a_server_with_leveling_off_gets_no_level_roles(self):
        leveling = _FakeLeveling()

        await self._claim(_FakeInteraction(), _bot(leveling=leveling, leveling_on=False))

        self.assertEqual(leveling.calls, [])
        self.assertEqual(_xp(MEMBER), claims.MAX_CLAIM_XP, "the claim itself still paid")

    async def test_a_failing_ladder_does_not_take_the_claim_with_it(self):
        leveling = _FakeLeveling(boom=True)

        await self._claim(_FakeInteraction(), _bot(leveling=leveling))

        self.assertEqual(_wallet(MEMBER), claims.MAX_CLAIM_MONEY)
        self.assertIn("One-time claim", self._text())

    async def test_the_claim_does_not_need_a_leveling_cog_at_all(self):
        await self._claim(_FakeInteraction(), None)

        self.assertEqual(_wallet(MEMBER), claims.MAX_CLAIM_MONEY)


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
class TheRegistryKnowsIt(unittest.TestCase):
    def test_it_is_a_core_command_and_not_a_catch(self):
        from utils import features, permissions

        self.assertIn("claim", features.ALWAYS_ALLOWED_COMMANDS)
        self.assertNotIn("claim", features.MODULE_COMMAND_MAP, "a switch would make it disappear")
        self.assertEqual(permissions.COMMAND_ACCESS.get("claim"), permissions.EVERYONE)

    def test_the_core_card_advertises_it(self):
        from utils import features

        names = [name for name, _note in features.feature_entries(features.get("system"))]
        self.assertIn("/claim", names)

    def test_the_cog_loads_without_a_feature_of_its_own(self):
        from utils import features

        self.assertIn("cogs.claims", features.CORE_COGS)
        self.assertNotIn("cogs.claims", [f.cog for f in features.FEATURES])

    def test_the_help_panel_names_it(self):
        """Discoverability, as a law rather than a hope: the panel is hand-written.

        It stays hand-written on purpose (``cogs/help.py`` says so), and the law
        beside it only checks that every command the panel *names* exists — so a
        command added without touching the prose is simply never mentioned.
        """
        from cogs.help import HELP_MODULES

        desc = HELP_MODULES["system"]["desc"]
        self.assertIn("`/claim`", desc)
        self.assertLessEqual(len(desc), 4_096, "Discord caps an embed description")

    def test_the_real_gate_answers_it_with_every_system_off(self):
        """The reason it is core, asked of the bot's own gate rather than the table.

        A server that switched the economy and the leveling off still has a
        member with a wallet and a level, and after a restore those two figures
        are the ones that came back wrong. So the gate every command passes
        through is driven with a bot whose module reader answers *off* to
        everything: `/claim` must come back allowed, and nothing may be said to
        the member about a system being switched off.
        """
        import asyncio

        import bot as bot_module
        from utils import pause

        sent = []

        class _Response:
            def is_done(self):
                return False

            async def send_message(self, content, ephemeral=False):
                sent.append((content, ephemeral))

        class _Interaction:
            def __init__(self):
                self.response = _Response()
                self.guild_id = GUILD
                self.command = type("C", (), {"name": "claim", "root_parent": None})()

        with mock.patch.object(pause, "is_paused", return_value=False), mock.patch.object(
            bot_module.bot, "is_module_enabled", return_value=False
        ):
            allowed = asyncio.run(bot_module.global_module_check(_Interaction()))

        self.assertTrue(allowed, "a server with every system off was refused /claim")
        self.assertEqual(sent, [], "the member was told a system was switched off")


if __name__ == "__main__":
    unittest.main()
