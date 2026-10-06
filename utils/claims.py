"""The one-time claim of level and coins, for a member who asks for it.

The database is rebuilt from a cloud snapshot, and a snapshot is a *moment*:
whatever a member earned after it is simply absent, and nothing in the bot can
say what it was — the rows are not wrong, they are gone. What can be said
honestly is a floor, so that is what ``/claim`` grants: bring anybody who asks
up to at least this many coins and this level.

Three rules, and each one is a way this could go wrong:

* **the amount is a floor, not a gift.** A member already above it is granted
  nothing, so the ceiling is a ceiling on what a claim can *add* and a claim can
  never reduce anybody. Two ceilings is the most any one member can cost, and
  the XP figure is derived from the curve in ``utils/levels`` rather than typed
  a second time.
* **it is opt-in.** Nothing is written for a member until they run the command,
  so a member who never claims keeps exactly the balance and level they had and
  has no row here at all.
* **it happens once.** One row per member, keyed by the primary key of
  ``claim_grants`` and written inside the same ``BEGIN IMMEDIATE`` transaction
  that moves the coins — so a second claim, or two arriving together, cannot pay
  twice. A member above the floor has nothing to claim, so their answer says so
  and their one chance is *not* spent: a refusal that consumed the claim would
  take the chance away from the member who needs it least, and give nothing back
  for it.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass

from utils import db, levels

# --------------------------------------------------------------------------- #
# The ceilings
# --------------------------------------------------------------------------- #
#: The least a member who claims ends up with in their wallet.
#:
#: A floor rather than a present, which is what makes it safe to advertise: it
#: cannot be farmed (the latch is one row), it cannot be repeated (the second
#: claim is refused), and it cannot be *unfair* — a member with more than this
#: is left exactly where they are.
MAX_CLAIM_MONEY = 25_000

#: The least level a member who claims ends up on.
#:
#: Deliberately a level and not a number of XP: a level is what a member asks
#: for, and the XP it stands for is what the curve in ``utils/levels`` says it
#: is — the same constant ``/setlevel`` and ``/givexp`` clamp against, so the
#: claim cannot land anywhere the rest of the bot considers out of range.
MAX_CLAIM_LEVEL = min(25, levels.MAX_LEVEL)

#: The XP ceiling, derived from the level ceiling above.
MAX_CLAIM_XP = levels.total_xp_for_level(MAX_CLAIM_LEVEL)


@dataclass(frozen=True)
class Grant:
    """What one claim adds, and the level it leaves the member on."""

    money: int
    xp: int
    level: int

    @property
    def empty(self) -> bool:
        """True when there is nothing to claim — the member is already above it."""
        return self.money <= 0 and self.xp <= 0


#: The three answers a claim can have. Strings rather than an enum so a caller
#: (and a test) can print one without importing a second name.
GRANTED = "granted"
ALREADY_CLAIMED = "already"
NOTHING_TO_CLAIM = "above"


@dataclass(frozen=True)
class Outcome:
    """The status of one attempt, and the grant it did (or did not) make."""

    status: str
    grant: Grant

    @property
    def granted(self) -> bool:
        return self.status == GRANTED


def _whole(value) -> int:
    """A stored figure as a non-negative whole number.

    A negative balance is not a thing this bot writes, but it is exactly the
    thing a hand-edited row is, and a claim that read ``-500`` as a balance
    would pay 500 *more* than the ceiling. Refusing to trust it is the whole job
    of this function.
    """
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def claim_preview(wallet: int, xp: int) -> Grant:
    """What a claim adds to a member holding ``wallet`` coins and ``xp`` XP.

    Pure, so the ceilings are a law about arithmetic before they are a law about
    the database. The money is the ceiling minus the balance and the XP is the
    ceiling minus the XP, each floored at zero — that floor is what makes the
    claim a top-up rather than a gift, and it is also the only thing standing
    between a typo in a ceiling and a number the bot can no longer add to.

    The level is *derived* from the XP the member would end on rather than added
    to their stored one, because a stored level that disagrees with its XP is
    how this server once ended up with a Level 1351 rank card and a
    2.4-billion-XP leaderboard entry.
    """
    wallet = _whole(wallet)
    xp = levels.clamp_xp(_whole(xp))
    money = max(0, MAX_CLAIM_MONEY - wallet)
    granted_xp = max(0, MAX_CLAIM_XP - xp)
    end_xp = levels.clamp_xp(xp + granted_xp)
    level, _, _ = levels.calculate_level_from_xp(end_xp)
    return Grant(money=money, xp=granted_xp, level=level)


def _read_balances(cur, guild_id: int, user_id: int) -> tuple[int, int]:
    """The two figures a claim is measured against, as stored."""
    cur.execute(
        "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (user_id, guild_id)
    )
    row = cur.fetchone()
    wallet = _whole(row[0]) if row else 0
    cur.execute(
        "SELECT xp FROM user_levels WHERE guild_id=? AND user_id=?", (guild_id, user_id)
    )
    row = cur.fetchone()
    xp = levels.clamp_xp(_whole(row[0]) if row else 0)
    return wallet, xp


def _write_claim(cur, guild_id: int, user_id: int, grant: Grant) -> None:
    """The three writes one granted claim makes: the latch, then the two tables.

    The latch goes first and the balances after it, all on this one cursor, so
    the transaction that pays is the transaction that spends the chance. Each
    balance is written only when it moved: a member who is above the coin floor
    and below the level floor gets the level and no empty wallet row.
    """
    cur.execute(
        "INSERT INTO claim_grants (guild_id, user_id, claimed_at, money, xp, level) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            guild_id,
            user_id,
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
            grant.money,
            grant.xp,
            grant.level,
        ),
    )
    if grant.money:
        cur.execute(
            """INSERT INTO economy (user_id, guild_id, wallet, bank) VALUES (?, ?, ?, 0)
               ON CONFLICT(user_id, guild_id) DO UPDATE SET wallet = wallet + ?""",
            (user_id, guild_id, grant.money, grant.money),
        )
    if grant.xp:
        # ``xp = xp + ?`` rather than a value computed here: what this row holds
        # was read in this same transaction, and adding the grant is the same
        # statement ``/givexp`` makes. ``level`` is set rather than added, because
        # ``grant.level`` is the level the *ending* XP stands for.
        cur.execute(
            """INSERT INTO user_levels (guild_id, user_id, xp, level) VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = xp + ?, level=?""",
            (guild_id, user_id, grant.xp, grant.level, grant.xp, grant.level),
        )


def claim_once(guild_id: int, user_id: int) -> Outcome:
    """Grant this member's one claim, or say why it cannot be granted.

    One ``BEGIN IMMEDIATE`` transaction for the whole thing — the latch, the
    coins and the level — because they have to succeed or fail together. A claim
    that wrote the latch and then lost the coins would spend a member's one
    chance on nothing, and a claim that paid without the latch could simply be
    run again. ``BEGIN IMMEDIATE`` also takes the write lock before the balances
    are read, so the figure the top-up is measured against cannot change between
    the read and the write.

    The table is (re)created inside that transaction for the reason
    ``utils.meetings`` does the same: the cloud restore replaces the file under
    a running process, and a boot that already happened is not a promise that a
    table exists.
    """

    def _work(cur):
        for statement in db.CLAIM_SCHEMA:
            cur.execute(statement)
        cur.execute(
            "SELECT 1 FROM claim_grants WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        if cur.fetchone() is not None:
            return Outcome(ALREADY_CLAIMED, Grant(0, 0, 0))
        grant = claim_preview(*_read_balances(cur, guild_id, user_id))
        if grant.empty:
            return Outcome(NOTHING_TO_CLAIM, grant)
        _write_claim(cur, guild_id, user_id, grant)
        return Outcome(GRANTED, grant)

    return db.run_atomic(_work)
