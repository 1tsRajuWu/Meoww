"""The community shop: what is for sale, what each perk actually does, and the
arithmetic behind the prices.

Two properties this module exists to guarantee.

**Prices follow a ladder, and a pricier item is never the worse buy.** Five
tiers anchor at 500 / 1,500 / 3,000 / 10,000 / 50,000 coins, and the four slots
inside a tier cost 1x, 1.25x, 1.5x and 1.75x that anchor. A new item's price is
therefore a decision about its tier and its slot rather than a number invented
on the spot, and the ladder is visible to players because `/shop` groups the
items by tier.

The top slot multiplier is deliberately **below the tightest anchor ratio**
(the Everyday anchor of 1,500 is only 2x the Starter one), because a deeper
ladder made two listings in different tiers cost exactly the same — 3x of 1,500
and the Pro anchor are both 3,000 — and the higher tier's perk was strictly
better, so the cheaper tier sold a strictly worse item at the same price.
`tests/test_shop.py` fails if any listing is dominated by a cheaper one, and
that guard is what keeps the ladder honest as items are added.

**Every perk is real.** `/buy` used to insert the item into ``inventory`` and do
nothing else: the Rob Shield shielded nothing, the XP Booster boosted nothing.
Each item now carries an :class:`Effect` that a command actually consults —
activity multipliers in ``cogs/leveling``, plus rob odds and robbery shields in
``cogs/economy``.

Effect kinds, in the order the multiplier is applied:

``xp`` / ``coins``
    Timed multipliers on what chatting and voice earn. Deliberately *not* applied
    to gambling or robbing, so a booster can never be turned into a house-edge
    exploit.
``shield``
    Timed robbery immunity.
``luck``
    Timed percentage points added to your ``/rob`` success chance. Deliberately
    *not* applied to the casino commands: a wager pays double, so any edge there
    above the base odds would be a coin printer rather than a perk. ``/rob`` is a
    transfer between two members with a 30-minute cooldown, which luck can swing
    without minting anything.
``payout``
    Instant coins, rolled once at purchase — a ticket, not an investment.
``badge``
    Permanent, one-per-member multipliers on activity earnings.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

import aiosqlite

from utils import db

# --------------------------------------------------------------------------- #
# Pricing ladder
# --------------------------------------------------------------------------- #
#: Coins for slot 1 of each tier. Every other price is derived from these. The
#: five values are fixed: players already know what the original items cost.
TIER_BASE: dict[int, int] = {1: 500, 2: 1_500, 3: 3_000, 4: 10_000, 5: 50_000}
#: What the four slots inside a tier cost, as a multiple of the tier anchor.
#:
#: The widest multiplier must stay below the narrowest *anchor ratio* (1,500 to
#: 3,000 is only 2x), or a tier's top slot lands on the next tier's anchor: 3x of
#: 1,500 and the Pro anchor were both 3,000, so a tier-2 item and a strictly
#: better tier-3 item carried the same price tag. 1.75 keeps every tier strictly
#: below the next one, which `tests/test_shop.py` asserts listing by listing.
SLOT_MULTIPLIER: dict[int, float] = {1: 1.0, 2: 1.25, 3: 1.5, 4: 1.75}
TIER_NAMES: dict[int, str] = {
    1: "Starter",
    2: "Everyday",
    3: "Pro",
    4: "Elite",
    5: "Prestige",
}

# --------------------------------------------------------------------------- #
# Limits — the guards that stop a perk becoming an exploit
# --------------------------------------------------------------------------- #
#: Timed effects extend from whatever is left, up to this much in total, so
#: buying five boosters at once cannot bank a week of double XP.
MAX_EFFECT_MINUTES = 24 * 60
#: Hard ceiling on any timed multiplier, whatever the catalog says.
MAX_BOOST_MULTIPLIER = 5.0
#: Luck points do not stack beyond this (rob + gamble combined).
MAX_LUCK_POINTS = 40
#: Hard ceiling on a member's *combined* multiplier: the strongest timed boost
#: times every badge they own. Doubling ``MAX_BOOST_MULTIPLIER`` leaves room for
#: a few badges, and the test-suite pins that the live catalog cannot reach it —
#: an unbounded product of badges would otherwise be the one number nobody
#: checked as the catalog grew.
MAX_TOTAL_MULTIPLIER = MAX_BOOST_MULTIPLIER * 2
#: Active effects are only read once every this many seconds per member.
CACHE_TTL_SECONDS = 15.0
#: Minimum seconds between one member's purchases, per guild. It exists to stop
#: a double-click buying the same thing twice and a script hammering the command;
#: it is far shorter than reading a purchase card takes.
PURCHASE_COOLDOWN_SECONDS = 3.0

EFFECT_XP = "xp"
EFFECT_COINS = "coins"
EFFECT_LUCK = "luck"
EFFECT_SHIELD = "shield"


@dataclass(frozen=True)
class Effect:
    """What an item does. ``minutes`` is the duration of its timed part."""

    minutes: int = 0
    xp_multiplier: float = 1.0
    coin_multiplier: float = 1.0
    luck_bonus: int = 0
    shield_minutes: int = 0
    payout: tuple[int, int] | None = None
    badge: bool = False

    @property
    def timed(self) -> bool:
        """True when buying this starts (or extends) a clock.

        A shield item stores its duration in ``shield_minutes`` rather than
        ``minutes``, so both have to be consulted — a shield that reported itself
        as untimed granted nothing at all.
        """
        if self.badge or self.payout is not None:
            return False
        return bool(self.minutes or self.shield_minutes)

    def grants(self) -> list[tuple[str, float, int]]:
        """The timed effects this item starts, as ``(key, value, minutes)``.

        A badge and a payout item grant nothing timed: the first is read from
        ``inventory`` forever, the second is rolled once at purchase.
        """
        if not self.timed:
            return []
        grants: list[tuple[str, float, int]] = []
        if self.xp_multiplier > 1.0:
            grants.append((EFFECT_XP, min(self.xp_multiplier, MAX_BOOST_MULTIPLIER), self.minutes))
        if self.coin_multiplier > 1.0:
            grants.append(
                (EFFECT_COINS, min(self.coin_multiplier, MAX_BOOST_MULTIPLIER), self.minutes)
            )
        if self.luck_bonus:
            grants.append((EFFECT_LUCK, float(self.luck_bonus), self.minutes))
        if self.shield_minutes:
            grants.append((EFFECT_SHIELD, 1.0, self.shield_minutes))
        return grants


@dataclass(frozen=True)
class Item:
    """One shop listing. ``price`` is derived, never stored."""

    id: str
    name: str
    tier: int
    slot: int
    summary: str
    effect: Effect

    @property
    def price(self) -> int:
        return int(round(TIER_BASE[self.tier] * SLOT_MULTIPLIER[self.slot]))

    @property
    def unique(self) -> bool:
        """Badges are one per member: buying a second would just burn coins."""
        return self.effect.badge


# --------------------------------------------------------------------------- #
# The catalog
# --------------------------------------------------------------------------- #
_ITEMS: tuple[Item, ...] = (
    # -- tier 1: Starter (500 / 625 / 750 / 875) --------------------------- #
    Item(
        id="lucky_clover", name="🍀 Lucky Clover", tier=1, slot=1,
        summary="A four-leaf clover for good fortune — sharper odds when you go robbing.",
        effect=Effect(minutes=30, luck_bonus=8),
    ),
    Item(
        id="sugar_rush", name="🍬 Sugar Rush", tier=1, slot=2,
        summary="A quick sweet hit of rank progress.",
        effect=Effect(minutes=30, xp_multiplier=1.25),
    ),
    Item(
        id="coin_charm", name="🧧 Coin Charm", tier=1, slot=3,
        summary="A charm that pulls in a few extra coins per message.",
        effect=Effect(minutes=30, coin_multiplier=1.25),
    ),
    Item(
        id="scratch_card", name="🎟️ Scratch Card", tier=1, slot=4,
        summary="Rip it open for a random payout. A ticket, never an investment.",
        effect=Effect(payout=(50, 1_000)),
    ),
    # -- tier 2: Everyday (1,500 / 1,875 / 2,250 / 2,625) ------------------ #
    Item(
        id="rob_shield", name="🛡️ Rob Shield", tier=2, slot=1,
        summary="Robbers bounce off you while it holds.",
        effect=Effect(shield_minutes=4 * 60),
    ),
    Item(
        id="lockpick", name="🥷 Lockpick", tier=2, slot=2,
        summary="Bent in all the right places, for longer than a clover lasts.",
        effect=Effect(minutes=45, luck_bonus=12),
    ),
    # NOTE: luck is deliberately rob-only. The casino commands pay double on a
    # win, so any perk that raised those odds above the base would mint coins
    # instead of granting a perk.
    Item(
        id="canned_tuna", name="🐟 Canned Tuna", tier=2, slot=3,
        summary="A modest meal with a serious XP kick.",
        effect=Effect(minutes=30, xp_multiplier=1.5),
    ),
    Item(
        id="feast_platter", name="🍖 Feast Platter", tier=2, slot=4,
        summary="A proper spread, and the coins to match.",
        effect=Effect(minutes=60, coin_multiplier=1.5),
    ),
    # -- tier 3: Pro (3,000 / 3,750 / 4,500 / 5,250) ----------------------- #
    Item(
        id="xp_booster", name="⚡ XP Booster", tier=3, slot=1,
        summary="Supercharge your rank progression.",
        effect=Effect(minutes=120, xp_multiplier=1.5),
    ),
    Item(
        id="coin_doubler", name="💰 Coin Doubler", tier=3, slot=2,
        summary="Every coin your activity earns, nearly doubled.",
        effect=Effect(minutes=120, coin_multiplier=1.75),
    ),
    Item(
        id="ward_charm", name="🧿 Ward Charm", tier=3, slot=4,
        summary="A whole shift-length of robbery immunity.",
        effect=Effect(shield_minutes=12 * 60),
    ),
    Item(
        id="high_roller_chip", name="🎰 High Roller Chip", tier=3, slot=3,
        summary="Casino-grade nerve for an extended run of heists.",
        effect=Effect(minutes=90, luck_bonus=20),
    ),
    # -- tier 4: Elite (10,000 / 12,500 / 15,000 / 17,500) ----------------- #
    Item(
        id="vip_pass", name="💎 VIP Pass", tier=4, slot=1,
        summary="Exclusive VIP badge, and a permanent cut of everything you earn.",
        effect=Effect(badge=True, xp_multiplier=1.05, coin_multiplier=1.10),
    ),
    Item(
        id="vault_key", name="🔑 Vault Key", tier=4, slot=2,
        summary="Double coins for a full gaming session.",
        effect=Effect(minutes=180, coin_multiplier=2.0),
    ),
    Item(
        id="oracles_lens", name="🔮 Oracle's Lens", tier=4, slot=3,
        summary="Double XP for a full gaming session.",
        effect=Effect(minutes=180, xp_multiplier=2.0),
    ),
    Item(
        id="rocket_fuel", name="🚀 Rocket Fuel", tier=4, slot=4,
        summary="2.5x XP for a full session. Best used while you are typing.",
        effect=Effect(minutes=180, xp_multiplier=2.5),
    ),
    # -- tier 5: Prestige (50,000 / 62,500 / 75,000 / 87,500) -------------- #
    Item(
        id="royal_crown", name="👑 Royal Crown", tier=5, slot=1,
        summary="The ultimate prestige luxury crown.",
        effect=Effect(badge=True, xp_multiplier=1.10, coin_multiplier=1.25),
    ),
    Item(
        id="aegis_core", name="🛡️ Aegis Core", tier=5, slot=2,
        summary="A full day of robbery immunity, for members the whole server hunts.",
        effect=Effect(shield_minutes=24 * 60),
    ),
    Item(
        id="eternal_flame", name="🔥 Eternal Flame", tier=5, slot=3,
        summary="Triple XP for four hours. Bring snacks.",
        effect=Effect(minutes=240, xp_multiplier=3.0),
    ),
    Item(
        id="dragon_hoard", name="🐉 Dragon Hoard", tier=5, slot=4,
        summary="Triple coins for four hours of noise.",
        effect=Effect(minutes=240, coin_multiplier=3.0),
    ),
)

#: Newest-first is meaningless here; the shop is ordered by price.
CATALOG: tuple[Item, ...] = tuple(sorted(_ITEMS, key=lambda item: (item.tier, item.slot)))
BY_ID: dict[str, Item] = {item.id: item for item in CATALOG}
BY_NAME: dict[str, Item] = {item.name.lower(): item for item in CATALOG}


@dataclass(frozen=True)
class Multipliers:
    """Everything a member's active effects add up to right now."""

    xp: float = 1.0
    coins: float = 1.0
    luck: int = 0
    shield: bool = False

    @property
    def any(self) -> bool:
        return self.xp != 1.0 or self.coins != 1.0 or self.luck > 0 or self.shield

    def scaled(self, coins: int, xp: int) -> tuple[int, int]:
        """Apply the multipliers to an activity payout.

        Never rounds a real payout down to nothing: a perk should not be able to
        lose you a coin you had already earned.
        """
        new_coins = int(round(coins * self.coins)) if coins else 0
        new_xp = int(round(xp * self.xp)) if xp else 0
        if coins and new_coins < 1:
            new_coins = 1
        if xp and new_xp < 1:
            new_xp = 1
        return new_coins, new_xp


# --------------------------------------------------------------------------- #
# Catalog helpers
# --------------------------------------------------------------------------- #
def all_items() -> tuple[Item, ...]:
    return CATALOG


def items_in_tier(tier: int) -> tuple[Item, ...]:
    return tuple(item for item in CATALOG if item.tier == tier)


def tiers() -> tuple[int, ...]:
    return tuple(sorted({item.tier for item in CATALOG}))


def find(query: str) -> list[Item]:
    """Items matching a `/buy` argument, best match first.

    An exact name or id wins outright; otherwise every item whose name or id
    contains the query is returned, so the caller can ask which one was meant
    instead of silently buying the first substring match.
    """
    text = (query or "").strip().lower()
    if not text:
        return []
    if text in BY_ID:
        return [BY_ID[text]]
    if text in BY_NAME:
        return [BY_NAME[text]]
    matches = [
        item for item in CATALOG
        if text in item.id.lower() or text in item.name.lower()
    ]
    return matches


def format_duration(minutes: int) -> str:
    """``90`` -> ``"1 h 30 min"``."""
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h" if not rest else f"{hours} h {rest} min"


def format_remaining(seconds: float) -> str:
    """``45`` -> ``"< 1 min"``, ``5400`` -> ``"1 h 30 min"``."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "< 1 min"
    return format_duration(int(seconds // 60))


#: How each timed effect key reads on the `/inventory` card.
EFFECT_LABELS: dict[str, str] = {
    EFFECT_XP: "XP multiplier",
    EFFECT_COINS: "coin multiplier",
    EFFECT_LUCK: "luck",
    EFFECT_SHIELD: "rob immunity",
}


def describe_active(key: str, value: float, expires_ts: float, now: float) -> str:
    """One `/inventory` line: what is running, and how long is left of it."""
    if key == EFFECT_XP:
        detail = f"×{value:g} XP"
    elif key == EFFECT_COINS:
        detail = f"×{value:g} coins"
    elif key == EFFECT_LUCK:
        detail = f"+{int(value)}% rob odds"
    else:
        detail = "robbers bounce off"
    label = EFFECT_LABELS.get(key, key)
    return f"• **{detail}** — {label} • `{format_remaining(expires_ts - now)}` left"


def describe_effect(item: Item) -> str:
    """One line saying what the perk does, in the same words the catalog uses."""
    effect = item.effect
    if effect.badge:
        return (
            f"+{round((effect.coin_multiplier - 1) * 100)}% coins and "
            f"+{round((effect.xp_multiplier - 1) * 100)}% XP, permanently"
        )
    if effect.payout is not None:
        low, high = effect.payout
        return f"instant payout of {low:,}-{high:,} 🪙"
    # Grouped by duration rather than listed flat, because an item's perks need
    # not share a clock: a flat list made a shield read "rob immunity for 4 h
    # for 4 h" (the duration stated once in the perk, once in the suffix) and
    # would attribute one perk's clock to another if they ever differed.
    parts: dict[int, list[str]] = {}
    if effect.xp_multiplier > 1.0:
        parts.setdefault(effect.minutes, []).append(f"×{effect.xp_multiplier:g} XP")
    if effect.coin_multiplier > 1.0:
        parts.setdefault(effect.minutes, []).append(f"×{effect.coin_multiplier:g} coins")
    if effect.luck_bonus:
        parts.setdefault(effect.minutes, []).append(f"+{effect.luck_bonus}% rob odds")
    if effect.shield_minutes:
        parts.setdefault(effect.shield_minutes, []).append("rob immunity")
    if not parts:
        return item.summary
    return " & ".join(
        f"{' & '.join(texts)} for {format_duration(minutes)}"
        for minutes, texts in parts.items()
    )


def shop_line(item: Item) -> str:
    """One catalog entry for the `/shop` card: the flavour, then the mechanics.

    Both halves have to be here. A player reads the summary to decide whether an
    item is interesting and the effect to decide whether it is worth the coins,
    and a catalog that showed only the effect left every ``summary`` in this
    module as unused text.
    """
    return (
        f"**{item.name}** — `{item.price:,}` 🪙\n"
        f"{item.summary} *({describe_effect(item)})*"
    )


def badge_line(item: Item) -> str:
    """One `/inventory` line for a permanent badge."""
    return f"• **{item.name}** — {describe_effect(item)}"


def roll_payout(item: Item, rng: random.Random | None = None) -> int:
    """Coins for a ``payout`` item. ``rng`` is injectable so tests are exact."""
    if item.effect.payout is None:
        return 0
    low, high = item.effect.payout
    return (rng or random).randint(low, high)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
#: ``{key: (monotonic_expiry, Multipliers)}`` — a short cache, because the
#: leveling listener would otherwise read these rows on every single message.
_cache: dict[tuple[int, int], tuple[float, Multipliers]] = {}

#: ``{key: wall_clock}`` — when each member last actually bought something.
_purchases: dict[tuple[int, int], float] = {}


def purchase_cooldown_remaining(
    user_id: int, guild_id: int, now: float | None = None
) -> float:
    """Seconds this member must wait before their next purchase (0 if free)."""
    now = time.time() if now is None else now
    last = _purchases.get((user_id, guild_id))
    if last is None:
        return 0.0
    return max(0.0, last + PURCHASE_COOLDOWN_SECONDS - now)


def note_purchase(user_id: int, guild_id: int, now: float | None = None) -> None:
    """Record a successful purchase, dropping entries that can no longer block."""
    now = time.time() if now is None else now
    # Pruned on every write so the map is bounded by the members who bought
    # something in the last few seconds, not by every member the bot has seen.
    for key, stamp in [kv for kv in _purchases.items() if kv[1] + PURCHASE_COOLDOWN_SECONDS <= now]:
        _purchases.pop(key, None)
    _purchases[(user_id, guild_id)] = now


def clear_purchase_limits() -> None:
    """Forget recent purchases. For tests, and after restoring a database."""
    _purchases.clear()


def invalidate(user_id: int | None = None, guild_id: int | None = None) -> None:
    """Drop cached multipliers after a purchase, for one member or all of them."""
    if user_id is None or guild_id is None:
        _cache.clear()
        return
    _cache.pop((user_id, guild_id), None)


# Two flavours of the same two operations, because the purchase path must run
# inside one transaction and the read path must run inside the event loop:
#
#   ``*_on(cursor, ...)``  a *synchronous* sqlite3 cursor, for db.run_atomic
#   ``grant``/``active_effects``  the async API, used by the cogs
#
# Mixing them up is exactly the bug this comment exists to prevent: an aiosqlite
# cursor's ``execute``/``fetchall`` are coroutines, not the sync objects that
# db.run_atomic hands out.

def grant_on(cursor, user_id: int, guild_id: int, item: Item, now: float) -> None:
    """Start or extend an item's timed effects on a synchronous cursor.

    Called from the purchase transaction, so the coins, the item and the perk all
    land together or not at all.
    """
    cap = MAX_EFFECT_MINUTES * 60
    for key, value, minutes in item.effect.grants():
        # Multipliers and luck points are different units: a x5 boost and +40
        # luck are both ceilings, and each has to be clamped in its own terms.
        if key == EFFECT_LUCK:
            value = min(value, float(MAX_LUCK_POINTS))
        else:
            value = min(value, MAX_BOOST_MULTIPLIER)
        cursor.execute(
            "SELECT value, expires_ts FROM item_effects WHERE user_id=? AND guild_id=? AND effect=?",
            (user_id, guild_id, key),
        )
        row = cursor.fetchone()
        if row is None or float(row[1] or 0.0) <= now:
            start, best, added = now, value, minutes * 60.0
        else:
            # Still running: keep the stronger value and add the new time on,
            # converted into that stronger perk's terms.
            #
            # Without the conversion the *cheapest* item was the best way to
            # extend the dearest one: a 625-coin Sugar Rush (x1.25 XP for 30 min)
            # bought while an Oracle's Lens (x2 XP for 3 h) was running added a
            # full 30 minutes of x2 XP — a quarter of Oracle's Lens's own price
            # for the same multiplier. Effect-minutes now keep their value, so
            # 30 minutes of x1.25 is worth 18 minutes of x2.
            start = float(row[1])
            running = max(0.0, float(row[0] or 0.0))
            best = max(running, value)
            added = minutes * 60.0 * (value / best) if best else minutes * 60.0
            # Paying for time that rounds away to nothing would be the opposite
            # of a perk, so anything bought adds at least a minute.
            added = max(60.0, added)
        expiry = min(now + cap, start + added)
        cursor.execute(
            """INSERT INTO item_effects (user_id, guild_id, effect, value, expires_ts)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, guild_id, effect) DO UPDATE SET value=?, expires_ts=?""",
            (user_id, guild_id, key, best, expiry, best, expiry),
        )


async def grant(user_id: int, guild_id: int, item: Item, now: float | None = None) -> None:
    """Start or extend an item's timed effects in its own transaction."""
    now = time.time() if now is None else now
    await asyncio.to_thread(db.run_atomic, lambda cur: grant_on(cur, user_id, guild_id, item, now))
    invalidate(user_id, guild_id)


async def active_effects(
    user_id: int, guild_id: int, now: float | None = None
) -> dict[str, tuple[float, float]]:
    """Active effects, dropping any that have just expired."""
    now = time.time() if now is None else now
    async with aiosqlite.connect(db.db_path()) as conn:
        async with conn.execute(
            "SELECT effect, value, expires_ts FROM item_effects "
            "WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        ) as cur:
            rows = await cur.fetchall()
        live = [row for row in rows if (row[2] or 0.0) > now]
        # One round trip, and a write only when something actually expired:
        # most calls find nothing expired, and a DELETE that matches nothing
        # still takes a write transaction (WAL + fsync) on every chat message.
        if len(live) != len(rows):
            await conn.execute(
                "DELETE FROM item_effects WHERE user_id=? AND guild_id=? AND expires_ts <= ?",
                (user_id, guild_id, now),
            )
            await conn.commit()
    return {row[0]: (float(row[1] or 0.0), float(row[2] or 0.0)) for row in live}


async def owned_items(user_id: int, guild_id: int) -> list[Item]:
    """Catalog entries this member owns, badges included, once each.

    Deliberately deduplicated by item id. Repeat purchases stack a boost's *time*
    in ``item_effects``, so extra inventory rows are only a purchase history —
    but a badge is folded into the member's multipliers one time per *row*, so a
    duplicate (bought before badges were made unique, or carried in by a restored
    backup) used to multiply it twice. Buying the same item twice is a legitimate
    thing to do, so the guard belongs here rather than at the call sites.
    """
    async with aiosqlite.connect(db.db_path()) as conn:
        async with conn.execute(
            "SELECT item_name FROM inventory WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        ) as cur:
            names = [row[0] for row in await cur.fetchall()]
    owned: dict[str, Item] = {}
    for name in names:
        item = BY_NAME.get((name or "").lower())
        if item is not None:
            owned.setdefault(item.id, item)
    return list(owned.values())


async def multipliers(user_id: int, guild_id: int, now: float | None = None) -> Multipliers:
    """Everything this member's timed perks and permanent badges add up to.

    Timed boosts take the *strongest* active multiplier rather than compounding;
    badges multiply together, so a VIP Pass and a Royal Crown are better than
    either alone but not the sum of them.
    """
    key = (user_id, guild_id)
    cached = _cache.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    now = time.time() if now is None else now
    effects = await active_effects(user_id, guild_id, now)
    badges = [item for item in await owned_items(user_id, guild_id) if item.effect.badge]

    xp = max(1.0, effects.get(EFFECT_XP, (1.0, 0.0))[0])
    coins = max(1.0, effects.get(EFFECT_COINS, (1.0, 0.0))[0])
    luck = min(MAX_LUCK_POINTS, int(effects.get(EFFECT_LUCK, (0.0, 0.0))[0]))
    shield = EFFECT_SHIELD in effects
    for item in badges:
        xp *= item.effect.xp_multiplier
        coins *= item.effect.coin_multiplier

    # Badges multiply on top of the strongest timed boost, so the ceiling here is
    # deliberately above the cap on a single timed multiplier — but it is a named
    # ceiling rather than an arbitrary multiple, and the test-suite asserts the
    # whole catalog stays under it.
    result = Multipliers(
        xp=min(xp, MAX_TOTAL_MULTIPLIER),
        coins=min(coins, MAX_TOTAL_MULTIPLIER),
        luck=luck,
        shield=shield,
    )
    _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, result)
    return result


async def active_summary(user_id: int, guild_id: int, now: float | None = None) -> str:
    """The member's running perks as card lines, or an empty string if none.

    Shared by `/inventory` and the purchase card, because the purchase card has
    to show what is *actually* running: buying a weaker item while a stronger one
    is up converts the time it adds (see ``grant_on``), so the item's own printed
    duration would be a promise the member did not get.
    """
    now = time.time() if now is None else now
    effects = await active_effects(user_id, guild_id, now)
    return "\n".join(
        describe_active(key, value, expires, now)
        for key, (value, expires) in sorted(effects.items())
    )


async def luck_points(user_id: int, guild_id: int) -> int:
    """Just the luck bonus, for the robbing and gambling paths."""
    return (await multipliers(user_id, guild_id)).luck


async def has_shield(user_id: int, guild_id: int) -> bool:
    """Whether a member is currently shielded from robberies."""
    return (await multipliers(user_id, guild_id)).shield

