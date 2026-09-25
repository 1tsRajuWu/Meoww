"""Shared loan math for DaCat Bank.

The bank used to be one hardcoded rule written inline in ``cogs/economy.py``:
a flat 50,000 cap, 10% interest, 7 days. Nothing else could ask what a member's
credit line actually was, and the stored term was never enforced at all — the
``due_ts`` column was written and then never read again.

The numbers now live here, the way the XP formula lives in ``utils/levels.py``,
so the slash commands and the test-suite agree by construction.

Credit is *earned*: a member's credit line is derived from their level, so
leveling up pays off somewhere outside the rank card. On top of that you pick a
plan, which trades the rate against the size of the loan and the length of term.
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Credit line — grows with level
# ---------------------------------------------------------------------------
#: Everyone starts with a small line so a level-0 member can still play.
BASE_CREDIT = 5_000
#: Every level adds this much available credit.
CREDIT_PER_LEVEL = 2_500
#: Nobody borrows more than this, however high they climb (MAX_LEVEL is 500).
MAX_CREDIT = 250_000
#: Smallest loan the bank will bother writing.
MIN_LOAN = 100


def credit_line(level: int) -> int:
    """Total credit available to a member at ``level`` (before the plan multiplier)."""
    try:
        level = max(0, int(level or 0))
    except (TypeError, ValueError):
        level = 0
    return min(BASE_CREDIT + level * CREDIT_PER_LEVEL, MAX_CREDIT)


def next_credit_line(level: int) -> int | None:
    """The credit line one level higher, or None once the ceiling is reached."""
    current = credit_line(level)
    if current >= MAX_CREDIT:
        return None
    return credit_line(int(level) + 1)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Plan:
    """One borrowing option."""

    id: str
    name: str
    emoji: str
    rate: float
    term_days: int
    credit_multiplier: float
    blurb: str


PLANS: tuple[Plan, ...] = (
    Plan(
        id="standard",
        name="Standard Loan",
        emoji="🏦",
        rate=0.10,
        term_days=7,
        credit_multiplier=1.0,
        blurb="The honest banker: your full credit line, 10% flat, a week to pay it back.",
    ),
    Plan(
        id="quick",
        name="Quick Cash",
        emoji="⚡",
        rate=0.05,
        term_days=3,
        credit_multiplier=0.35,
        blurb="Half the interest, a third of the credit, and only 3 days to pay.",
    ),
    Plan(
        id="shark",
        name="Shark Loan",
        emoji="🦈",
        rate=0.30,
        term_days=2,
        credit_multiplier=2.0,
        blurb="Double your credit line at 30% and 2 days. The shark knocks politely.",
    ),
)

DEFAULT_PLAN = "standard"


def get_plan(plan_id: str | None) -> Plan | None:
    """Look up a plan by id. Unknown ids return None (fails closed)."""
    for candidate in PLANS:
        if candidate.id == plan_id:
            return candidate
    return None


def plan(plan_id: str | None) -> Plan:
    """Look up a plan, falling back to the default for anything unrecognised."""
    return get_plan(plan_id) or get_plan(DEFAULT_PLAN)


def credit_limit(plan_id: str | None, level: int) -> int:
    """How much a member may borrow on ``plan_id`` at their current level."""
    selected = plan(plan_id)
    return max(MIN_LOAN, int(credit_line(level) * selected.credit_multiplier))


def interest(amount: int, plan_id: str | None) -> int:
    """Flat fee on ``amount``. Never zero — a loan is never free money.

    The old code used ``int(amount * 0.10)``, so borrowing fewer than 10 coins
    cost nothing at all and could be repeated to launder a free loan.
    """
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        amount = 0
    return max(1, round(amount * plan(plan_id).rate))


def repay_total(amount: int, plan_id: str | None) -> int:
    """``amount`` plus the flat fee — what the member owes back."""
    return int(amount) + interest(amount, plan_id)


# ---------------------------------------------------------------------------
# Term & overdue handling
# ---------------------------------------------------------------------------
#: Grace period after the due date before the penalty lands.
GRACE_SECONDS = 6 * 3600
#: One-time penalty applied to whatever is still outstanding.
LATE_FEE_RATE = 0.25
#: ...but never less than this, so a tiny loan still has teeth.
MIN_LATE_FEE = 50


def term_seconds(plan_id: str | None) -> float:
    return plan(plan_id).term_days * 86400.0


def due_at(now: float, plan_id: str | None) -> float:
    """Unix timestamp this loan falls due."""
    return float(now) + term_seconds(plan_id)


def secs_left(due_ts: float | None, now: float) -> float:
    """Seconds until the due date (negative once past it)."""
    if not due_ts:
        return 0.0
    return float(due_ts) - float(now)


def is_overdue(due_ts: float | None, now: float) -> bool:
    """True once a loan is past due *and* past the grace period."""
    if not due_ts:
        return False
    return float(now) > float(due_ts) + GRACE_SECONDS


def late_fee(outstanding: int, plan_id: str | None = None) -> int:
    """The one-time penalty for being overdue (display only — charged via SQL)."""
    del plan_id  # kept in the signature so callers can pass the plan through
    try:
        outstanding = max(0, int(outstanding))
    except (TypeError, ValueError):
        outstanding = 0
    return max(MIN_LATE_FEE, int(outstanding * LATE_FEE_RATE))


def late_fee_sql(column: str = "repay_amount") -> str:
    """SQLite expression computing the same penalty on a stored balance.

    Charged in SQL so it can be applied inside the same transaction that reads
    the debt, which means it lands exactly once no matter how many commands run
    at the same time. ``column`` is only ever a column name we control.
    """
    return f"MAX({MIN_LATE_FEE}, CAST({column} * {LATE_FEE_RATE} AS INTEGER))"


# ---------------------------------------------------------------------------
# Flavour: credit rating shown on the card
# ---------------------------------------------------------------------------
RATINGS: tuple[tuple[int, str, str], ...] = (
    (50, "Elite", "💎"),
    (30, "Trusted", "🥇"),
    (15, "Reliable", "🥈"),
    (5, "Rookie", "🥉"),
    (0, "Unknown", "👻"),
)


def rating(level: int) -> tuple[str, str]:
    """Return ``(rating_name, emoji)`` for a member's level."""
    try:
        level = max(0, int(level or 0))
    except (TypeError, ValueError):
        level = 0
    for threshold, name, emoji in RATINGS:
        if level >= threshold:
            return name, emoji
    return RATINGS[-1][1], RATINGS[-1][2]


def format_plan_line(selected: Plan, level: int) -> str:
    """One help-style line describing a plan at the member's current level."""
    cap = credit_limit(selected.id, level)
    return (
        f"> {selected.emoji} **{selected.name}** — up to **{cap:,}** 🪙 • "
        f"**{int(selected.rate * 100)}%** fee • **{selected.term_days}d** term"
    )
