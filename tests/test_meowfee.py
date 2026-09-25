"""The DaCat Fee: what it costs, where it goes, and how to switch it off.

The fee is the one place coins leave a member's wallet for somewhere that is
*not* another member and *not* the void — DaCat Bank. So these tests care about
three things above all: the arithmetic is the arithmetic (2%, floor of one, a
real ceiling), the bank actually receives what the sender paid, and no path
mints or destroys a coin while the fee is moving. The last one is checked by
summing every balance before and after, which is the only assertion that would
catch a fee charged twice or banked while the transfer was refused.
"""
import asyncio
import math
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.support import TempDatabaseMixin
from utils import db, meowfee

import cogs.economy as economy_module

GUILD = 111
SENDER = 900
TARGET = 901


def _wallet(user_id: int) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (user_id, GUILD)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _bank() -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT coins FROM meow_bank WHERE guild_id=?", (GUILD,)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _seed(user_id: int, wallet: int) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) "
            "VALUES (?, ?, ?, 0)",
            (user_id, GUILD, wallet),
        )
        conn.commit()
    finally:
        conn.close()


class TestTheFeeMaths(unittest.TestCase):
    def test_it_is_proportional_with_a_floor_and_a_ceiling(self):
        self.assertEqual(meowfee.RATE, 0.02)
        self.assertEqual(meowfee.fee_for(1_000), 20)
        self.assertEqual(meowfee.fee_for(10_000), 200)
        # A fee that rounds away to nothing is not a fee, and a fraction of a
        # coin cannot be paid.
        self.assertEqual(meowfee.fee_for(1), meowfee.MIN_FEE)
        self.assertEqual(meowfee.fee_for(10), meowfee.MIN_FEE)
        # …and "small" has to keep meaning small as amounts grow.
        self.assertEqual(meowfee.fee_for(100_000), meowfee.MAX_FEE)
        self.assertEqual(meowfee.fee_for(10 ** 12), meowfee.MAX_FEE)

    def test_a_transfer_that_does_not_happen_owes_nothing(self):
        for amount in (0, -1, -50_000):
            self.assertEqual(meowfee.fee_for(amount), 0)

    def test_the_ceiling_is_actually_reached_and_binds_from_then_on(self):
        """Otherwise ``MAX_FEE`` could be a number nothing ever hits, which is the
        kind of constant that stops being true when the rate changes."""
        first = next(
            n for n in range(1, 1_000_000) if meowfee.fee_for(n) >= meowfee.MAX_FEE
        )
        # Strictly below the cap before the crossing, and welded to it after.
        self.assertLess(meowfee.fee_for(first - 1), meowfee.MAX_FEE)
        self.assertEqual(meowfee.fee_for(first), meowfee.MAX_FEE)
        self.assertEqual(meowfee.fee_for(first * 100), meowfee.MAX_FEE)
        # The crossing point is the cap divided by the rate, so this test fails
        # if either constant moves without the other being reconsidered.
        self.assertEqual(first, math.ceil((meowfee.MAX_FEE - 0.5) / meowfee.RATE))

    def test_the_label_and_the_description_agree_with_the_maths(self):
        self.assertEqual(meowfee.fee_label(), "2%")
        text = meowfee.describe()
        self.assertIn(meowfee.fee_label(), text)
        self.assertIn(f"{meowfee.MAX_FEE:,}", text)

    def test_on_and_off_are_read_and_junk_is_refused(self):
        for word in ("on", "ON", "true", "yes", "enabled", "1"):
            self.assertIs(meowfee.parse_enabled(word), True, word)
        for word in ("off", "OFF", "false", "no", "disabled", "0"):
            self.assertIs(meowfee.parse_enabled(word), False, word)
        # Not a word we know must not be guessed at — silently turning the fee
        # off would be the wrong answer to a typo.
        for junk in ("maybe", "", "  ", "banana", 7):
            self.assertIsNone(meowfee.parse_enabled(junk), repr(junk))
        self.assertIsNone(meowfee.parse_enabled(None))


class TestTheSwitch(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("meowfee_switch.db")
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def test_a_server_that_never_touched_it_charges_the_fee(self):
        """Absent has to mean on — the cards promise the fee."""
        self.assertTrue(await meowfee.is_enabled(GUILD))

    async def test_it_can_be_turned_off_and_back_on(self):
        self.assertTrue(await meowfee.set_enabled(GUILD, False))
        self.assertFalse(await meowfee.is_enabled(GUILD))
        self.assertTrue(await meowfee.set_enabled(GUILD, True))
        self.assertTrue(await meowfee.is_enabled(GUILD))

    async def test_one_servers_switch_is_not_anothers(self):
        await meowfee.set_enabled(GUILD, False)
        self.assertFalse(await meowfee.is_enabled(GUILD))
        self.assertTrue(await meowfee.is_enabled(GUILD + 1))

    async def test_a_raw_string_row_still_reads_as_off(self):
        """``guild_settings`` predates JSON values, and a restored backup can hold
        a plain "0" that ``json.loads`` would hand back as the string."""
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
                (GUILD, meowfee.SETTING_KEY, "0"),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertFalse(await meowfee.is_enabled(GUILD))

    async def test_an_empty_guild_is_never_charged(self):
        self.assertFalse(await meowfee.is_enabled(0))
        self.assertFalse(await meowfee.set_enabled(0, True))
        self.assertEqual(await meowfee.bank_balance(0), 0)

    async def test_the_bank_starts_empty_and_adds_up(self):
        self.assertEqual(await meowfee.bank_balance(GUILD), 0)
        await asyncio.to_thread(db.run_atomic, lambda cur: meowfee.credit(cur, GUILD, 25))
        await asyncio.to_thread(db.run_atomic, lambda cur: meowfee.credit(cur, GUILD, 75))
        self.assertEqual(await meowfee.bank_balance(GUILD), 100)
        # A zero credit is not a deposit and must not create a row.
        await asyncio.to_thread(db.run_atomic, lambda cur: meowfee.credit(cur, GUILD + 9, 0))
        self.assertEqual(await meowfee.bank_balance(GUILD + 9), 0)


class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _FakeMember:
    def __init__(self, uid: int):
        self.id = uid
        self.bot = False
        self.name = "Target"
        self.display_name = "Target"
        self.mention = f"<@{uid}>"


class _FakeUser(_FakeMember):
    pass


class _FakeClient:
    """``check_is_owner`` asks the bot for its owner list, so the fake needs one."""

    async def is_owner(self, _user) -> bool:
        return False


class _FakeInteraction:
    def __init__(self, uid: int = SENDER, gid: int = GUILD):
        self.user = _FakeUser(uid)
        self.guild = SimpleNamespace(id=gid, owner_id=1)
        self.guild_id = gid
        self.client = _FakeClient()
        self.response = _FakeResponse()


class _Commands(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("meowfee_cmd.db")
        # ``cogs.economy`` copies the path at import time, so it has to be
        # pointed at the throwaway file too or the commands read the real one.
        self._original_economy_path = economy_module.DB_PATH
        economy_module.DB_PATH = path
        await db.setup()
        self.cog = economy_module.Economy(bot=None)
        self.cards = []

    async def asyncTearDown(self):
        economy_module.DB_PATH = self._original_economy_path
        self.release_temp_database()

    async def _fake_send_card(self, _target, *args, **kwargs):
        self.cards.append(kwargs.get("embed"))
        return SimpleNamespace(id=1)

    def _card(self, interaction):
        if self.cards:
            card = self.cards[-1]
            return SimpleNamespace(
                title=getattr(card, "title", "") or "",
                description=getattr(card, "description", "") or "",
                fields=[f.name for f in getattr(card, "fields", [])],
                values=[f.value for f in getattr(card, "fields", [])],
                footer=getattr(getattr(card, "footer", None), "text", "") or "",
            )
        content = interaction.response.messages[-1]["content"] if interaction.response.messages else ""
        return SimpleNamespace(title="", description=content, fields=[], values=[], footer="")

    async def _call(self, command, interaction, *args):
        self.cards.clear()
        with mock.patch.object(economy_module, "send_card", new=self._fake_send_card):
            await command.callback(self.cog, interaction, *args)
        return self._card(interaction)

    async def pay(self, amount: int, interaction=None):
        return await self._call(
            economy_module.Economy.pay,
            interaction or _FakeInteraction(),
            _FakeMember(TARGET),
            amount,
        )

    async def meowfee_command(self, state=None, interaction=None):
        return await self._call(
            economy_module.Economy.meowfee_cmd,
            interaction or _FakeInteraction(),
            state,
        )


class TestPaying(_Commands):
    async def test_the_sender_pays_the_fee_and_the_recipient_gets_it_all(self):
        _seed(SENDER, 10_000)
        card = await self.pay(1_000)
        self.assertEqual(_wallet(SENDER), 10_000 - 1_020)
        self.assertEqual(_wallet(TARGET), 1_000, "the recipient must not be shorted")
        self.assertEqual(_bank(), 20)
        self.assertIn("1,000", card.description)
        self.assertIn("20", card.description)

    async def test_the_card_says_what_was_paid_and_what_the_bank_took(self):
        _seed(SENDER, 10_000)
        card = await self.pay(5_000)
        total = 5_000 + meowfee.fee_for(5_000)
        self.assertIn(meowfee.fee_label(), card.description)
        self.assertIn(f"{meowfee.fee_for(5_000):,}", card.description)
        self.assertIn(f"{total:,}", card.description)
        self.assertEqual(_wallet(SENDER), 10_000 - total)

    async def test_nothing_moves_if_the_wallet_cannot_cover_amount_plus_fee(self):
        """The old check compared against the amount alone, so a sender one coin
        short of the fee would have been refused *after* the debit."""
        _seed(SENDER, 1_010)
        card = await self.pay(1_000)
        self.assertIn("only have", card.description.lower())
        self.assertEqual(_wallet(SENDER), 1_010)
        self.assertEqual(_wallet(TARGET), 0)
        self.assertEqual(_bank(), 0)

    async def test_an_exact_wallet_is_enough(self):
        _seed(SENDER, 1_020)
        await self.pay(1_000)
        self.assertEqual(_wallet(SENDER), 0)
        self.assertEqual(_bank(), 20)

    async def test_no_coins_are_minted_or_destroyed(self):
        _seed(SENDER, 10_000)
        _seed(TARGET, 500)
        before = _wallet(SENDER) + _wallet(TARGET) + _bank()
        await self.pay(1_234)
        after = _wallet(SENDER) + _wallet(TARGET) + _bank()
        self.assertEqual(before, after)

    async def test_turning_the_fee_off_moves_the_exact_amount(self):
        _seed(SENDER, 10_000)
        await meowfee.set_enabled(GUILD, False)
        await self.pay(1_000)
        self.assertEqual(_wallet(SENDER), 9_000)
        self.assertEqual(_wallet(TARGET), 1_000)
        self.assertEqual(_bank(), 0, "an off switch means the bank takes nothing")

    async def test_two_simultaneous_payments_cannot_share_one_wallet(self):
        """The read-then-write guard is only worth anything if two payments fired
        at once cannot both act on the same balance. 2,000 covers one 1,000
        transfer plus its 20 coin fee and not a coin more, so the second must be
        refused rather than served from money that has already left."""
        _seed(SENDER, 2_000)
        self.cards.clear()
        first, second = _FakeInteraction(), _FakeInteraction()
        # One patch around both callbacks: nesting two would let the first one's
        # exit restore the real ``send_card`` while the second was still running.
        with mock.patch.object(economy_module, "send_card", new=self._fake_send_card):
            await asyncio.gather(
                economy_module.Economy.pay.callback(
                    self.cog, first, _FakeMember(TARGET), 1_000
                ),
                economy_module.Economy.pay.callback(
                    self.cog, second, _FakeMember(TARGET), 1_000
                ),
            )
        # Exactly one payment happened, and the loser was told why.
        self.assertEqual(_wallet(SENDER), 2_000 - 1_020)
        self.assertEqual(_wallet(TARGET), 1_000, "only one transfer was delivered")
        self.assertEqual(_bank(), 20, "the bank took one fee, not two")
        self.assertEqual(len(self.cards), 1, "exactly one receipt was sent")
        refusals = [
            interaction.response.messages[-1]["content"]
            for interaction in (first, second)
            if interaction.response.messages
        ]
        self.assertEqual(len(refusals), 1, refusals)
        # Refused either by the pre-check (which now names the fee) or by the
        # transaction's own guard. Both are correct; silence is not.
        self.assertTrue(
            "DaCat Fee" in refusals[0] or "changed" in refusals[0], refusals[0]
        )

    async def test_the_bank_receives_exactly_the_fees_collected(self):
        _seed(SENDER, 100_000)
        for amount in (1_000, 2_500, 10_000):
            await self.pay(amount)
        expected = sum(meowfee.fee_for(a) for a in (1_000, 2_500, 10_000))
        self.assertEqual(_bank(), expected)


class TestRobbing(_Commands):
    def _force_outcome(self, success: bool):
        return mock.patch.object(
            economy_module.random, "random", return_value=0.0 if success else 1.0
        )

    async def test_a_successful_heist_pays_the_bank_out_of_the_loot(self):
        _seed(SENDER, 5_000)      # the robber
        _seed(TARGET, 50_000)     # the victim
        before = _wallet(SENDER) + _wallet(TARGET) + _bank()
        # 0.0 guarantees success against any base chance below 1.
        with self._force_outcome(True):
            card = await self._call(
                economy_module.Economy.rob, _FakeInteraction(), _FakeMember(TARGET)
            )
        taken = 50_000 - _wallet(TARGET)
        self.assertGreater(taken, 0)
        self.assertEqual(_wallet(SENDER), 5_000 + taken - meowfee.fee_for(taken))
        self.assertEqual(_bank(), meowfee.fee_for(taken))
        self.assertEqual(before, _wallet(SENDER) + _wallet(TARGET) + _bank())
        self.assertIn("DaCat Fee", card.description)

    async def test_the_victim_loses_the_same_amount_with_or_without_the_fee(self):
        """The fee comes out of the robber's proceeds, never the victim's."""
        _seed(SENDER, 5_000)
        _seed(TARGET, 50_000)
        await meowfee.set_enabled(GUILD, False)
        with self._force_outcome(True):
            await self._call(
                economy_module.Economy.rob, _FakeInteraction(), _FakeMember(TARGET)
            )
        without_fee = 50_000 - _wallet(TARGET)
        self.assertEqual(_bank(), 0)
        self.assertEqual(_wallet(SENDER), 5_000 + without_fee)

    async def test_a_failed_heist_banks_nothing(self):
        _seed(SENDER, 5_000)
        _seed(TARGET, 50_000)
        with self._force_outcome(False):
            await self._call(
                economy_module.Economy.rob, _FakeInteraction(), _FakeMember(TARGET)
            )
        self.assertEqual(_bank(), 0)


class TestTheCommand(_Commands):
    async def test_no_argument_shows_the_state_the_bank_and_the_maths(self):
        card = await self.meowfee_command(None)
        self.assertIn("DaCat Fee", card.title)
        self.assertIn("on", card.description)
        self.assertIn(meowfee.fee_label(), card.description)
        self.assertTrue(any("DaCat Bank" in name for name in card.fields), card.fields)

    async def test_turning_it_off_says_so_and_keeps_the_bank(self):
        _seed(SENDER, 1_000)
        await self.pay(500)
        banked = _bank()
        self.assertGreater(banked, 0)
        card = await self.meowfee_command(SimpleNamespace(value="off"))
        self.assertIn("Off", card.title)
        self.assertFalse(await meowfee.is_enabled(GUILD))
        self.assertIn(f"{banked:,}", card.description)
        self.assertEqual(_bank(), banked, "turning it off must not empty the bank")

    async def test_turning_it_back_on_says_so(self):
        await meowfee.set_enabled(GUILD, False)
        card = await self.meowfee_command(SimpleNamespace(value="on"))
        self.assertIn("On", card.title)
        self.assertTrue(await meowfee.is_enabled(GUILD))

    async def test_a_direct_message_is_told_bank_is_per_server(self):
        interaction = _FakeInteraction()
        interaction.guild = None
        card = await self.meowfee_command(None, interaction)
        self.assertIn("server", card.description)

    async def test_the_admin_check_is_a_real_permission_check(self):
        """Declared as administrator in the permissions table, so it must carry
        the framework check rather than trusting its own body."""
        command = economy_module.Economy.meowfee_cmd
        names = [check.__qualname__ for check in command.checks]
        self.assertTrue(
            any("has_permissions" in name for name in names),
            f"no permission check on /meowfee: {names}",
        )


class TestTheRegistry(unittest.TestCase):
    def test_the_command_is_registered_under_the_economy_feature(self):
        """It is `/eco-admin fee` now: one setting does not need a command slot of
        its own, and the group it joined is gated by the same feature."""
        from utils import features

        economy = next(f for f in features.FEATURES if f.id == "economy")
        self.assertIn("eco-admin", economy.gate_commands)
        self.assertIn("/eco-admin fee", economy.commands)

    def test_the_audience_is_declared_as_administrator(self):
        from utils import permissions

        self.assertEqual(
            permissions.COMMAND_ACCESS.get("eco-admin fee"), permissions.ADMIN
        )
