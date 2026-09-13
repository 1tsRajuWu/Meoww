"""Meow Bank: loan math, atomicity, penalties, and the funny commands.

The loan system used to be a single hardcoded rule with four defects that these
tests pin:

* the cap was a flat 50,000 no matter who you were, and the level system had no
  effect on the bank at all — the credit line is now derived from your level;
* borrowing read "no active loan" on one connection and inserted on another, so
  two simultaneous `/loan` calls both credited the wallet, and the loser of the
  race got an unhandled ``IntegrityError`` instead of a message;
* the stored ``due_ts`` was never read again, so a "7-day term" meant nothing;
* ``int(amount * 0.10)`` made any loan under 10 coins completely free.

Everything here drives the real slash-command callbacks, the way the other
money tests do. ``cogs.economy`` and ``cogs.fun`` capture ``DB_PATH`` at import,
so the module attributes are patched alongside ``utils.db``.
"""
import asyncio
import random
import unittest
from types import SimpleNamespace
from unittest import mock

from discord import app_commands

from cogs import economy as economy_module
from cogs import fun as fun_module
from tests.support import TempDatabaseMixin
from utils import db, loans

GUILD = 4242
USER = 77


def _wallet(user_id: int = USER, guild_id: int = GUILD) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (user_id, guild_id)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _bank(user_id: int = USER, guild_id: int = GUILD) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT bank FROM economy WHERE user_id=? AND guild_id=?", (user_id, guild_id)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _loan_row(user_id: int = USER, guild_id: int = GUILD):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT amount, repay_amount, due_ts, plan, status, late_fee_applied"
            " FROM loans WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        ).fetchone()
    finally:
        conn.close()


class LoanMath(unittest.TestCase):
    """The pure arithmetic, checked the way the card promises it."""

    def test_credit_line_grows_with_level(self):
        self.assertEqual(loans.credit_line(0), loans.BASE_CREDIT)
        self.assertEqual(
            loans.credit_line(1), loans.BASE_CREDIT + loans.CREDIT_PER_LEVEL
        )
        self.assertEqual(
            loans.credit_line(9), loans.BASE_CREDIT + 9 * loans.CREDIT_PER_LEVEL
        )

    def test_credit_line_is_capped(self):
        self.assertEqual(loans.credit_line(10_000), loans.MAX_CREDIT)
        self.assertIsNone(loans.next_credit_line(10_000))

    def test_credit_line_survives_rubbish_input(self):
        self.assertEqual(loans.credit_line(None), loans.BASE_CREDIT)
        self.assertEqual(loans.credit_line(-5), loans.BASE_CREDIT)

    def test_plans_scale_the_credit_line(self):
        base = loans.credit_line(4)
        self.assertEqual(loans.credit_limit("standard", 4), base)
        self.assertEqual(loans.credit_limit("shark", 4), base * 2)
        self.assertLess(loans.credit_limit("quick", 4), base)

    def test_unknown_plan_falls_back_to_the_default(self):
        self.assertEqual(loans.plan("definitely-not-a-plan").id, loans.DEFAULT_PLAN)

    def test_a_tiny_loan_is_never_free(self):
        """`int(amount * 0.10)` charged nothing below 10 coins."""
        for plan_id in ("standard", "quick", "shark"):
            for amount in range(1, 30):
                self.assertGreaterEqual(
                    loans.interest(amount, plan_id), 1,
                    f"{amount} coins on {plan_id} was free money",
                )

    def test_repay_total_is_principal_plus_fee(self):
        amount = 10_000
        self.assertEqual(
            loans.repay_total(amount, "standard"),
            amount + loans.interest(amount, "standard"),
        )
        # A shark loan really is more expensive than a standard one.
        self.assertGreater(
            loans.repay_total(amount, "shark"), loans.repay_total(amount, "standard")
        )

    def test_overdue_respects_the_grace_window(self):
        now = 1_000_000.0
        self.assertFalse(loans.is_overdue(now - 60, now), "just past due is not overdue")
        self.assertFalse(loans.is_overdue(0, now), "a loan with no due date never expires")
        self.assertTrue(loans.is_overdue(now - loans.GRACE_SECONDS - 1, now))

    def test_late_fee_has_a_floor_and_a_rate(self):
        self.assertEqual(loans.late_fee(10), loans.MIN_LATE_FEE)
        self.assertEqual(loans.late_fee(10_000), int(10_000 * loans.LATE_FEE_RATE))

    def test_due_date_uses_the_plan_term(self):
        now = 500.0
        self.assertEqual(
            loans.due_at(now, "quick"), now + loans.plan("quick").term_days * 86400
        )


class _FakeUser:
    def __init__(self, uid: int = USER):
        self.id = uid
        self.display_name = f"User{uid}"
        self.name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.bot = False
        self.display_avatar = SimpleNamespace(url="https://example.invalid/a.png")


class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    def is_done(self) -> bool:
        return bool(self.messages)


class _FakeInteraction:
    def __init__(self, uid: int = USER, gid: int = GUILD):
        self.user = _FakeUser(uid)
        self.guild = SimpleNamespace(id=gid, owner_id=1)
        self.guild_id = gid
        self.response = _FakeResponse()
        self.client = None


async def _async_noop(*_args, **_kwargs):
    return None


class _BankCase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("loans.db")
        # Both cogs captured the path at import time.
        economy_module.DB_PATH = path
        fun_module.DB_PATH = path
        await db.setup()
        self.cog = economy_module.Economy(bot=None)
        self.cards: list = []
        # Most cases borrow 10,000, which the level-0 line (5,000) would rightly
        # refuse. Give the member a level first; the tests that are *about* the
        # level gate set their own.
        self._seed_level(20)

    async def asyncTearDown(self):
        self.release_temp_database()

    async def _fake_send_card(self, _target, *args, **kwargs):
        self.cards.append(kwargs.get("embed"))
        return SimpleNamespace(id=1)

    def _seed_wallet(self, wallet: int, bank: int = 0, user_id: int = USER):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?, ?, ?, ?)",
                (user_id, GUILD, wallet, bank),
            )
            conn.commit()
        finally:
            conn.close()

    def _seed_level(self, level: int, user_id: int = USER):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO user_levels (guild_id, user_id, xp, level) VALUES (?, ?, 0, ?)",
                (GUILD, user_id, level),
            )
            conn.commit()
        finally:
            conn.close()

    def _backdate_due(self, hours_ago: float, user_id: int = USER):
        """Push a loan's due date into the past, past the grace window."""
        import time

        conn = db.connect()
        try:
            conn.execute(
                "UPDATE loans SET due_ts=? WHERE user_id=? AND guild_id=?",
                (time.time() - hours_ago * 3600, user_id, GUILD),
            )
            conn.commit()
        finally:
            conn.close()

    async def _loan(self, interaction, amount=None, plan=None):
        choice = app_commands.Choice(name=plan, value=plan) if plan else None
        with mock.patch.object(economy_module, "send_card", new=self._fake_send_card):
            await economy_module.Economy.loan.callback(
                self.cog, interaction, amount, choice
            )

    async def _repay(self, interaction, amount=None, from_bank: bool = False):
        with mock.patch.object(economy_module, "send_card", new=self._fake_send_card):
            await economy_module.Economy.repay.callback(
                self.cog, interaction, amount, from_bank
            )


class BorrowingIsAtomic(_BankCase):
    async def test_borrowing_credits_the_wallet_and_records_the_debt(self):
        self._seed_wallet(0)
        interaction = _FakeInteraction()

        await self._loan(interaction, 10_000)

        self.assertEqual(_wallet(), 10_000, "the loan was not disbursed")
        row = _loan_row()
        self.assertIsNotNone(row, "no loan row was written")
        borrowed, owed, _due, plan_id, status, penalty = row
        self.assertEqual(borrowed, 10_000)
        self.assertEqual(owed, 11_000, "the 10% standard fee was not charged")
        self.assertEqual(plan_id, "standard")
        self.assertEqual(status, "active")
        self.assertEqual(penalty, 0)

    async def test_two_simultaneous_borrows_pay_out_once(self):
        """The old code passed its 'no active loan' read twice and both paid."""
        self._seed_wallet(0)
        first, second = _FakeInteraction(), _FakeInteraction()

        await asyncio.gather(self._loan(first, 10_000), self._loan(second, 10_000))

        self.assertEqual(_wallet(), 10_000, "the same coins were borrowed twice")
        conn = db.connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM loans WHERE user_id=? AND guild_id=?", (USER, GUILD)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1, "two loans were written for the same member")

    async def test_a_loan_above_the_level_limit_is_refused(self):
        self._seed_wallet(0)
        self._seed_level(0)
        interaction = _FakeInteraction()

        # Level 0: the standard line is BASE_CREDIT.
        await self._loan(interaction, loans.BASE_CREDIT + 1)

        self.assertEqual(_wallet(), 0)
        self.assertIsNone(_loan_row(), "an over-limit loan was written")
        self.assertIn("limit", interaction.response.messages[-1]["content"])

    async def test_leveling_up_raises_the_credit_line(self):
        self._seed_wallet(0)
        self._seed_level(20)
        interaction = _FakeInteraction()
        want = loans.credit_limit("standard", 20)

        await self._loan(interaction, want)

        self.assertEqual(_wallet(), want, "the level-based credit line was not honoured")
        self.assertIsNotNone(_loan_row())

    async def test_the_shark_plan_lends_double_and_charges_more(self):
        self._seed_wallet(0)
        interaction = _FakeInteraction()
        want = loans.credit_limit("shark", 0)

        await self._loan(interaction, want, plan="shark")

        row = _loan_row()
        self.assertEqual(row[0], want)
        self.assertEqual(row[1], loans.repay_total(want, "shark"))
        self.assertEqual(row[3], "shark")
        self.assertGreater(row[1], loans.repay_total(want, "standard"))

    async def test_a_second_loan_is_refused_until_the_first_is_cleared(self):
        self._seed_wallet(0)
        await self._loan(_FakeInteraction(), 5_000)
        second = _FakeInteraction()

        await self._loan(second, 5_000)

        self.assertEqual(_wallet(), 5_000, "a second loan was disbursed")
        self.assertEqual(len(second.response.messages), 1)


class RepayingIsAtomic(_BankCase):
    async def test_partial_repayment_keeps_the_loan_open(self):
        self._seed_wallet(0)
        await self._loan(_FakeInteraction(), 10_000)   # owes 11,000
        interaction = _FakeInteraction()

        await self._repay(interaction, 4_000)

        self.assertEqual(_wallet(), 6_000)
        self.assertEqual(_loan_row()[1], 7_000, "the outstanding balance is wrong")

    async def test_a_full_repayment_clears_the_loan(self):
        self._seed_wallet(0)
        await self._loan(_FakeInteraction(), 10_000)   # owes 11,000
        # Borrow gave 10,000; top up so the full debt is affordable.
        self._seed_wallet(12_000)

        await self._repay(_FakeInteraction())

        self.assertEqual(_wallet(), 1_000)
        self.assertIsNone(_loan_row(), "the loan row outlived the debt")

    async def test_repaying_more_than_you_owe_cannot_overpay(self):
        self._seed_wallet(0)
        await self._loan(_FakeInteraction(), 10_000)
        self._seed_wallet(1_000_000)

        await self._repay(_FakeInteraction(), 999_999)

        self.assertEqual(_wallet(), 1_000_000 - 11_000, "the fee was not included once")
        self.assertIsNone(_loan_row())

    async def test_the_bank_can_pay_the_loan_directly(self):
        self._seed_wallet(0)
        await self._loan(_FakeInteraction(), 10_000)
        self._seed_wallet(0, bank=12_000)

        await self._repay(_FakeInteraction(), from_bank=True)

        self.assertEqual(_bank(), 1_000)
        self.assertIsNone(_loan_row())

    async def test_an_unaffordable_repayment_is_refused_without_moving_coins(self):
        self._seed_wallet(0)
        await self._loan(_FakeInteraction(), 10_000)   # wallet 10,000, owes 11,000
        interaction = _FakeInteraction()

        await self._repay(interaction)

        self.assertEqual(_wallet(), 10_000, "coins moved despite the refusal")
        self.assertEqual(_loan_row()[1], 11_000)
        self.assertIn("Insufficient", interaction.response.messages[-1]["content"])

    async def test_parallel_repayments_cannot_clear_the_loan_for_free(self):
        """A clamped debit let both of two repayments 'pay' the same coins.

        Only 6,000 of the 11,000 debt is affordable, so with a clamped
        `wallet = wallet - ?` both calls would have succeeded and the balance
        would have absorbed 12,000 of debt that was only paid for once.
        """
        self._seed_wallet(0)
        await self._loan(_FakeInteraction(), 10_000)   # owes 11,000
        self._seed_wallet(6_000)
        first, second = _FakeInteraction(), _FakeInteraction()

        await asyncio.gather(self._repay(first, 6_000), self._repay(second, 6_000))

        self.assertEqual(_wallet(), 0, "the wallet was not debited exactly once")
        row = _loan_row()
        self.assertIsNotNone(row, "the loan was cleared without being paid for")
        self.assertEqual(row[1], 5_000, "the debt absorbed two payments, not one")
        self.assertEqual(
            sum(1 for i in (first, second) if i.response.messages), 1,
            "both repayments reported success",
        )


class OverdueLoansBite(_BankCase):
    async def test_an_overdue_loan_is_penalised_exactly_once(self):
        self._seed_wallet(0)
        await self._loan(_FakeInteraction(), 10_000)   # owes 11,000
        self._backdate_due(hours_ago=48)

        await self._loan(_FakeInteraction())           # status card charges the fee
        after_first = _loan_row()
        self.assertEqual(after_first[5], 1, "the penalty was never applied")
        self.assertEqual(
            after_first[1], 11_000 + loans.late_fee(11_000), "the wrong penalty was charged"
        )
        self.assertEqual(after_first[4], "overdue")

        await self._loan(_FakeInteraction())           # and never twice
        self.assertEqual(_loan_row()[1], after_first[1], "the penalty was charged twice")

    async def test_a_fresh_loan_is_not_penalised(self):
        self._seed_wallet(0)
        await self._loan(_FakeInteraction(), 10_000)

        await self._loan(_FakeInteraction())

        row = _loan_row()
        self.assertEqual(row[5], 0)
        self.assertEqual(row[1], 11_000)

    async def test_the_penalty_is_reported_on_repayment(self):
        self._seed_wallet(0)
        await self._loan(_FakeInteraction(), 10_000)
        self._backdate_due(hours_ago=48)
        self._seed_wallet(0, bank=100_000)

        interaction = _FakeInteraction()
        await self._repay(interaction, from_bank=True)

        self.assertIsNone(_loan_row())
        self.assertIn("late fee", self.cards[-1].description.replace("**", ""))


class TheLoanCardTellsTheTruth(_BankCase):
    async def test_the_centre_shows_the_level_based_credit_line(self):
        self._seed_wallet(0)
        self._seed_level(12)   # overrides the shared level-20 seed

        await self._loan(_FakeInteraction())

        description = self.cards[-1].description
        self.assertIn(f"{loans.credit_line(12):,}", description)
        self.assertIn("Level 12", description)
        self.assertIn("No active loans", description)

    async def test_an_active_loan_is_summarised_with_its_plan(self):
        self._seed_wallet(0)
        await self._loan(_FakeInteraction(), 10_000, plan="quick")

        await self._loan(_FakeInteraction())

        description = self.cards[-1].description.replace("**", "")
        self.assertIn("Quick Cash", description)
        self.assertIn(f"{loans.repay_total(10_000, 'quick'):,}", description)


class FunnyCommands(_BankCase):
    async def test_beg_pays_what_it_reports(self):
        random.seed(1234)
        self._seed_wallet(0)

        payouts = []
        for _ in range(40):
            interaction = _FakeInteraction()
            before = _wallet()
            with mock.patch.object(economy_module, "send_card", new=self._fake_send_card):
                await economy_module.Economy.beg.callback(self.cog, interaction)
            payout = _wallet() - before
            payouts.append(payout)
            self.assertIn(payout, [0, 100] + list(range(1, 6)) + list(range(5, 41)))
            self.assertIn(f"{payout:,}", self.cards[-1].description.replace("**", ""))

        self.assertGreater(sum(payouts), 0, "begging never paid anything in 40 tries")

    async def test_flex_calls_out_an_outstanding_loan(self):
        self._seed_wallet(0)
        await self._loan(_FakeInteraction(), 10_000)   # wallet 10,000, debt 11,000
        cog = fun_module.Fun(bot=None)
        interaction = _FakeInteraction()

        with mock.patch.object(fun_module, "send_card", new=self._fake_send_card):
            await fun_module.Fun.flex.callback(cog, interaction, None)

        embed = self.cards[-1]
        description = embed.description.replace("**", "")
        self.assertIn("11,000", description, "the debt was not mentioned")
        self.assertIn("Standard Loan", description)
        self.assertEqual(embed.fields[2].value, "**11,000** 🪙")

    async def test_flex_of_a_debt_free_member_says_so(self):
        self._seed_wallet(1_500, bank=300)
        cog = fun_module.Fun(bot=None)

        with mock.patch.object(fun_module, "send_card", new=self._fake_send_card):
            await fun_module.Fun.flex.callback(cog, _FakeInteraction(), None)

        values = [field.value for field in self.cards[-1].fields]
        self.assertEqual(values[0], "**1,500** 🪙")
        self.assertEqual(values[1], "**300** 🪙")
        self.assertEqual(values[2], "**0** 🪙")

    async def test_aura_always_produces_a_verdict(self):
        cog = fun_module.Fun(bot=None)
        random.seed(7)
        seen = set()

        for _ in range(12):
            with mock.patch.object(fun_module, "send_card", new=self._fake_send_card):
                await fun_module.Fun.aura.callback(cog, _FakeInteraction(), None)
            embed = self.cards[-1]
            seen.add(embed.description.splitlines()[-1])
            self.assertIn("Aura:", embed.description.replace("**", ""))

        self.assertGreater(len(seen), 1, "every aura reading was identical")


if __name__ == "__main__":
    unittest.main()
