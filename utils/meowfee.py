"""The DaCat Fee: a small cut on coins moving between members.

The market's treat fee is **burned** — the coins leave the economy. This one is
**banked**: it lands in DaCat Bank, the server's house account, in the very same
transaction that moves the money. That difference is why this fee is deliberately
small, because a banked fee is not a sink. Every coin is still in the server, just
no longer in the sender's wallet, so there is no inflation argument for making it
bite — only the mild, intentional cost of passing coins to somebody else.

Two rules keep it honest:

* **It is charged on top of what was promised.** A member sending 1,000 🪙 pays
  1,020 🪙 and the recipient receives exactly the 1,000 🪙 they were sent. A fee
  taken out of the amount would quietly short the recipient, who did not agree to
  pay it.
* **It is capped.** "Small" has to keep meaning small as amounts grow, so the
  rate stops applying past :data:`MAX_FEE`. Without a cap, 2% of a whale's
  transfer would be a bounty rather than a fee.

Nothing here is invented: the fee is a single integer derived from the amount,
and the bank's balance is the sum of what was actually collected.
"""
from __future__ import annotations

from . import db

#: The share of a transfer that goes to DaCat Bank. The *word* is the brand's,
#: and the module, the table and the slash command that carry this fee keep the
#: older spelling on purpose — renaming a stored key is a migration, and
#: `brand.INTERNAL_NAMES` is where that list is written down.
RATE = 0.02
#: A transfer that happens owes at least this much — a fee that rounds away to
#: nothing is not a fee, and a fraction of a coin cannot be paid.
MIN_FEE = 1
#: And never more than this, however large the transfer.
MAX_FEE = 500
#: Where the per-guild switch lives in ``guild_settings``. Absent means **on**,
#: so a server that has never touched it gets the behaviour the cards describe.
SETTING_KEY = "meow_fee_enabled"

#: Who the bank's row belongs to. One row per guild, so a server's house account
#: is its own — the coins a member pays here are not somebody else's server's.
BANK_TABLE = "meow_bank"


def fee_for(coins: float) -> int:
    """The DaCat Fee owed on a transfer of ``coins``.

    Proportional, whole, never below :data:`MIN_FEE` and never above
    :data:`MAX_FEE`. A non-positive transfer owes nothing, because there is
    nothing to charge.
    """
    if coins <= 0:
        return 0
    return max(MIN_FEE, min(MAX_FEE, int(round(coins * RATE))))


def fee_label() -> str:
    """``2%`` — the rate as it is printed on a card."""
    return f"{RATE * 100:g}%"


def describe() -> str:
    """One sentence for a card, so no command has to restate the rules."""
    return (
        f"**{fee_label()}** of a transfer goes to DaCat Bank, at least "
        f"{MIN_FEE} 🪙 and never more than {MAX_FEE:,} 🪙, charged on top of "
        "what the sender promised."
    )


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #
async def is_enabled(guild_id: int) -> bool:
    """Whether this guild charges the fee. Unset means yes."""
    if not guild_id:
        return False
    stored = await db.get_setting(guild_id, SETTING_KEY, None)
    if stored is None:
        return True
    if isinstance(stored, str):
        # ``guild_settings`` predates JSON values and may hold a raw "0"/"off".
        return stored.strip().lower() not in ("0", "false", "off", "no", "")
    return bool(stored)


async def set_enabled(guild_id: int, enabled: bool) -> bool:
    """Turn the fee on or off for one guild. Returns whether it took."""
    if not guild_id:
        return False
    await db.set_setting(guild_id, SETTING_KEY, bool(enabled))
    return True


# --------------------------------------------------------------------------- #
# DaCat Bank
# --------------------------------------------------------------------------- #
#: The table is created at boot *and* defensively on first use, because a
#: database restored from a backup taken before this existed would otherwise
#: make every transfer fail rather than simply start an empty bank.
CREATE_BANK_TABLE = (
    f"CREATE TABLE IF NOT EXISTS {BANK_TABLE} ("
    "guild_id INTEGER PRIMARY KEY, coins INTEGER NOT NULL DEFAULT 0)"
)


def ensure_bank_table(cursor) -> None:
    """Create the bank if it is missing. Safe to call inside a transaction."""
    cursor.execute(CREATE_BANK_TABLE)


def credit(cursor, guild_id: int, coins: int) -> int:
    """Add ``coins`` to the guild's DaCat Bank and return the new balance.

    Runs on the caller's cursor so it lands in the caller's transaction: the
    money leaves a wallet and reaches the bank together or not at all.
    """
    if coins <= 0:
        return 0
    ensure_bank_table(cursor)
    # Upsert-and-add in one statement. ``guild_id`` is the primary key, so two
    # transfers racing can only ever add, never overwrite each other's balance.
    cursor.execute(
        f"INSERT INTO {BANK_TABLE} (guild_id, coins) VALUES (?, ?) "
        f"ON CONFLICT(guild_id) DO UPDATE SET coins = coins + excluded.coins",
        (guild_id, coins),
    )
    row = cursor.execute(
        f"SELECT coins FROM {BANK_TABLE} WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    return int(row[0]) if row else coins


async def bank_balance(guild_id: int) -> int:
    """What DaCat Bank holds for this guild, or 0 if nothing was ever collected."""
    if not guild_id:
        return 0
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(CREATE_BANK_TABLE)
        async with conn.execute(
            f"SELECT coins FROM {BANK_TABLE} WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        await conn.commit()
    return int(row[0]) if row else 0


def parse_enabled(value) -> bool | None:
    """Read a user's on/off word, or ``None`` if it is not one.

    Kept here rather than in the cog so the accepted spellings are testable
    without a Discord client.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("on", "true", "yes", "enable", "enabled", "1"):
        return True
    if text in ("off", "false", "no", "disable", "disabled", "0"):
        return False
    return None
