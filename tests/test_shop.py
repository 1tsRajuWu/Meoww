"""The community shop: the price ladder, the perks, and the commands that hand
them out.

The shop used to be five strings and a price. `/buy` inserted the item into
``inventory`` and did nothing else, so the Rob Shield shielded nothing and the
XP Booster boosted nothing — the descriptions were the whole feature. These tests
pin the two properties that replaced it:

* prices follow the documented ladder (tier anchor x slot multiplier), so a new
  item's price is a decision about its tier and slot rather than a guess; and
* every perk is enforced by the command that is supposed to care — activity
  multipliers in ``cogs/leveling``, rob shields and odds in ``cogs/economy``.
"""
import random
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import economy as economy_module
from tests.support import TempDatabaseMixin
from utils import db, shop

GUILD = 4242
BUYER = 77
VICTIM = 88


def _wallet(user_id: int = BUYER, guild_id: int = GUILD) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (user_id, guild_id)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _inventory(user_id: int = BUYER, guild_id: int = GUILD) -> list[str]:
    conn = db.connect()
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT item_name FROM inventory WHERE user_id=? AND guild_id=?",
                (user_id, guild_id),
            )
        ]
    finally:
        conn.close()


class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    def is_done(self) -> bool:
        return bool(self.messages)


class _FakeUser:
    def __init__(self, uid: int, name: str = "Buyer"):
        self.id = uid
        self.name = name
        self.display_name = name
        self.mention = f"<@{uid}>"
        self.bot = False
        self.display_avatar = SimpleNamespace(url="https://example.invalid/a.png")


class _FakeInteraction:
    def __init__(self, uid: int = BUYER, gid: int = GUILD):
        self.user = _FakeUser(uid)
        self.guild = SimpleNamespace(id=gid)
        self.guild_id = gid
        self.response = _FakeResponse()
        self.client = None
        self.channel = None


class TestCatalog(unittest.TestCase):
    def test_ids_and_names_are_unique(self):
        ids = [item.id for item in shop.all_items()]
        names = [item.name for item in shop.all_items()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(names), len(set(names)))

    def test_prices_follow_the_ladder(self):
        for item in shop.all_items():
            self.assertEqual(
                item.price,
                int(round(shop.TIER_BASE[item.tier] * shop.SLOT_MULTIPLIER[item.slot])),
                f"{item.name} does not match its tier/slot price",
            )

    def test_prices_ascend_within_each_tier(self):
        for tier in shop.tiers():
            prices = [item.price for item in shop.items_in_tier(tier)]
            self.assertEqual(prices, sorted(prices), f"tier {tier} is out of order")
        # The ends of the ladder: tier 1's anchor is the cheapest thing for sale
        # and tier 5's top slot the dearest.
        self.assertEqual(shop.all_items()[0].price, shop.TIER_BASE[1])
        self.assertEqual(
            shop.all_items()[-1].price,
            int(round(shop.TIER_BASE[5] * shop.SLOT_MULTIPLIER[4])),
        )

    def test_no_listing_is_dominated_by_a_cheaper_one(self):
        """The whole point of a price ladder: paying more is never the worse deal.

        A deeper ladder used to put a tier-2 item and a strictly better tier-3
        item at the *same* price (3x of 1,500 is the Pro anchor of 3,000), so
        buying the cheaper tier was a trap. Two things are asserted here: no two
        listings share a price, and within an effect kind the strength on offer
        (effect size x its duration) rises strictly with price.
        """
        prices = [item.price for item in shop.all_items()]
        self.assertEqual(len(prices), len(set(prices)), "two listings share a price")

        kinds: dict[str, list[tuple[int, float]]] = {}
        for item in shop.all_items():
            effect = item.effect
            if effect.badge:
                continue
            if effect.payout is not None:
                continue
            if effect.xp_multiplier > 1.0:
                kinds.setdefault("xp", []).append((item.price, effect.xp_multiplier * effect.minutes))
            if effect.coin_multiplier > 1.0:
                kinds.setdefault("coins", []).append(
                    (item.price, effect.coin_multiplier * effect.minutes)
                )
            if effect.luck_bonus:
                kinds.setdefault("luck", []).append((item.price, effect.luck_bonus * effect.minutes))
            if effect.shield_minutes:
                kinds.setdefault("shield", []).append((item.price, float(effect.shield_minutes)))

        self.assertEqual(set(kinds), {"xp", "coins", "luck", "shield"})
        for kind, entries in kinds.items():
            entries.sort()
            strengths = [strength for _, strength in entries]
            self.assertEqual(
                strengths, sorted(strengths), f"a cheaper {kind} item is the better one"
            )
            self.assertEqual(len(strengths), len(set(strengths)), f"two {kind} items tie")

    def test_a_bigger_anchor_never_sells_a_cheaper_item(self):
        """Every tier's dearest listing is still below the next tier's anchor.

        This is the structural reason the ladder can't collide again: slot 4 is
        1.75x, comfortably under the tightest anchor ratio (1,500 -> 3,000).
        """
        for lower, upper in zip(shop.tiers(), shop.tiers()[1:]):
            dearest = max(item.price for item in shop.items_in_tier(lower))
            self.assertLess(
                dearest,
                shop.TIER_BASE[upper],
                f"tier {lower} reaches {dearest:,}, past tier {upper}'s anchor",
            )

    def test_a_permanent_badge_is_better_the_more_it_costs(self):
        badges = sorted(
            (item.price, item) for item in shop.all_items() if item.effect.badge
        )
        self.assertGreaterEqual(len(badges), 2)
        for (_, cheaper), (_, dearer) in zip(badges, badges[1:]):
            self.assertGreater(dearer.effect.xp_multiplier, cheaper.effect.xp_multiplier)
            self.assertGreater(dearer.effect.coin_multiplier, cheaper.effect.coin_multiplier)

    def test_no_catalog_combination_can_reach_the_total_ceiling(self):
        """The combined ceiling is a real number, and the catalog stays under it.

        It exists so an unbounded product of badges cannot quietly become the one
        limit nobody checks as items are added.
        """
        badges = [item for item in shop.all_items() if item.effect.badge]
        timed = [item for item in shop.all_items() if not item.effect.badge]
        badge_xp = badge_coins = 1.0
        for item in badges:
            badge_xp *= item.effect.xp_multiplier
            badge_coins *= item.effect.coin_multiplier
        best_xp = max(item.effect.xp_multiplier for item in timed)
        best_coins = max(item.effect.coin_multiplier for item in timed)
        self.assertLessEqual(best_xp * badge_xp, shop.MAX_TOTAL_MULTIPLIER)
        self.assertLessEqual(best_coins * badge_coins, shop.MAX_TOTAL_MULTIPLIER)
        # ...and it must not be so loose that one listing alone reaches it.
        self.assertGreater(shop.MAX_TOTAL_MULTIPLIER, shop.MAX_BOOST_MULTIPLIER)
        for item in shop.all_items():
            self.assertLessEqual(item.effect.xp_multiplier, shop.MAX_BOOST_MULTIPLIER, item.name)
            self.assertLessEqual(item.effect.coin_multiplier, shop.MAX_BOOST_MULTIPLIER, item.name)

    def test_purchase_cooldown_maths(self):
        shop.clear_purchase_limits()
        try:
            self.assertEqual(shop.purchase_cooldown_remaining(1, 2, 100.0), 0.0)
            shop.note_purchase(1, 2, 100.0)
            self.assertAlmostEqual(
                shop.purchase_cooldown_remaining(1, 2, 100.5),
                shop.PURCHASE_COOLDOWN_SECONDS - 0.5,
            )
            self.assertEqual(
                shop.purchase_cooldown_remaining(1, 2, 100.0 + shop.PURCHASE_COOLDOWN_SECONDS), 0.0
            )
            # Another member, or the same member in another guild, is unaffected.
            self.assertEqual(shop.purchase_cooldown_remaining(1, 3, 100.5), 0.0)
            self.assertEqual(shop.purchase_cooldown_remaining(2, 2, 100.5), 0.0)
            # A later purchase prunes entries that can no longer block anyone.
            later = 100.0 + shop.PURCHASE_COOLDOWN_SECONDS + 1
            shop.note_purchase(9, 9, later)
            self.assertEqual(shop.purchase_cooldown_remaining(1, 2, later), 0.0)
            self.assertEqual(len(shop._purchases), 1)
        finally:
            shop.clear_purchase_limits()

    def test_an_instant_payout_never_averages_more_than_it_costs(self):
        """A ticket whose expected value beat its price would print coins forever."""
        for item in shop.all_items():
            if item.effect.payout is None:
                continue
            low, high = item.effect.payout
            # The top prize may exceed the price (that is the fun of a ticket);
            # what it must never do is pay *on average* more than it charges,
            # which is the difference between a gamble and a coin printer.
            self.assertLess((low + high) / 2, item.price, item.name)

    def test_every_tier_has_four_slots(self):
        self.assertEqual(shop.tiers(), (1, 2, 3, 4, 5))
        for tier in shop.tiers():
            self.assertEqual(len(shop.items_in_tier(tier)), 4, f"tier {tier}")
            self.assertEqual(
                sorted(item.slot for item in shop.items_in_tier(tier)), [1, 2, 3, 4]
            )

    def test_the_original_items_kept_their_prices(self):
        """Players already know what these cost; a re-price would look like a scam."""
        expected = {
            "🍀 Lucky Clover": 500,
            "🛡️ Rob Shield": 1_500,
            "⚡ XP Booster": 3_000,
            "💎 VIP Pass": 10_000,
            "👑 Royal Crown": 50_000,
        }
        for name, price in expected.items():
            self.assertEqual(shop.find(name)[0].price, price, name)

    def test_every_item_does_something(self):
        for item in shop.all_items():
            effect = item.effect
            has_perk = (
                effect.badge
                or effect.payout is not None
                or effect.xp_multiplier > 1.0
                or effect.coin_multiplier > 1.0
                or effect.luck_bonus > 0
                or effect.shield_minutes > 0
            )
            self.assertTrue(has_perk, f"{item.name} is a decorative price tag")
            self.assertTrue(shop.describe_effect(item), f"{item.name} has no description")

    def test_timed_items_have_a_duration_and_badges_do_not(self):
        for item in shop.all_items():
            if item.effect.badge:
                self.assertFalse(item.effect.timed)
                self.assertTrue(item.unique, f"{item.name} is a badge but not unique")
                self.assertEqual(item.effect.grants(), [])
            elif item.effect.payout is not None:
                self.assertFalse(item.effect.timed)
                self.assertEqual(item.effect.grants(), [])
            else:
                self.assertTrue(item.effect.timed, f"{item.name} has no duration")
                self.assertGreater(
                    item.effect.minutes or item.effect.shield_minutes, 0, item.name
                )
                self.assertTrue(item.effect.grants(), f"{item.name} grants nothing")

    def test_every_grant_is_within_its_own_cap(self):
        """Multipliers and luck points are different units with different caps."""
        for item in shop.all_items():
            if item.effect.payout is not None or item.effect.badge:
                # A ticket pays out once; a badge is read from inventory forever.
                # Neither starts a clock, so neither grants anything here.
                continue
            grants = item.effect.grants()
            self.assertTrue(grants, f"{item.name} grants nothing")
            for key, value, minutes in grants:
                self.assertGreater(minutes, 0)
                if key == shop.EFFECT_LUCK:
                    self.assertLessEqual(value, shop.MAX_LUCK_POINTS)
                else:
                    self.assertLessEqual(value, shop.MAX_BOOST_MULTIPLIER)

    def test_find_matches_exactly_then_by_substring(self):
        self.assertEqual(shop.find("royal_crown")[0].id, "royal_crown")
        self.assertEqual(shop.find("👑 Royal Crown")[0].id, "royal_crown")
        self.assertEqual(shop.find("clover")[0].id, "lucky_clover")
        self.assertEqual(shop.find("CLOVER")[0].id, "lucky_clover")
        self.assertEqual(shop.find("nonsense"), [])

    def test_find_reports_ambiguity_instead_of_picking_one(self):
        matches = shop.find("coin")
        self.assertGreater(len(matches), 1, "precondition: two items contain 'coin'")
        self.assertIn("coin_charm", {item.id for item in matches})
        self.assertIn("coin_doubler", {item.id for item in matches})

    def test_describe_effect_reads_like_the_catalog(self):
        self.assertIn("×1.5 XP", shop.describe_effect(shop.find("xp_booster")[0]))
        self.assertIn("rob immunity", shop.describe_effect(shop.find("rob_shield")[0]))
        self.assertIn("rob odds", shop.describe_effect(shop.find("lucky_clover")[0]))
        self.assertIn("instant payout", shop.describe_effect(shop.find("scratch_card")[0]))
        self.assertIn("permanently", shop.describe_effect(shop.find("vip_pass")[0]))
        # A shield states its duration exactly once.
        self.assertEqual(shop.describe_effect(shop.find("rob_shield")[0]), "rob immunity for 4 h")
        for item in shop.all_items():
            text = shop.describe_effect(item)
            self.assertNotIn("h for", text, text)
            self.assertNotIn("min for", text, text)

    def test_describe_effect_gives_each_perk_its_own_clock(self):
        """No listing mixes durations today, so the grouping is pinned directly."""
        mixed = shop.Item(
            id="test_mixed", name="Test Mixed", tier=1, slot=1, summary="",
            effect=shop.Effect(minutes=60, xp_multiplier=1.5, shield_minutes=240),
        )
        text = shop.describe_effect(mixed)
        self.assertEqual(text, "×1.5 XP for 1 h & rob immunity for 4 h")

    def test_duration_formatting(self):
        self.assertEqual(shop.format_duration(30), "30 min")
        self.assertEqual(shop.format_duration(120), "2 h")
        self.assertEqual(shop.format_duration(90), "1 h 30 min")
        self.assertEqual(shop.format_remaining(30), "< 1 min")
        self.assertEqual(shop.format_remaining(3600), "1 h")

    def test_payout_stays_inside_its_range(self):
        item = shop.find("scratch_card")[0]
        low, high = item.effect.payout
        for _ in range(200):
            self.assertGreaterEqual(shop.roll_payout(item, random.Random(7)), low)
            self.assertLessEqual(shop.roll_payout(item, random.Random(7)), high)

    def test_scaling_never_rounds_a_real_payout_away(self):
        for multiplier in (1.0, 1.25, 0.0001):
            coins, xp = shop.Multipliers(coins=multiplier, xp=multiplier).scaled(1, 1)
            self.assertGreaterEqual(coins, 1)
            self.assertGreaterEqual(xp, 1)
        # A payout of zero stays zero rather than being invented.
        self.assertEqual(shop.Multipliers(coins=2.0, xp=2.0).scaled(0, 0), (0, 0))


class TestPerkStorage(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("shop.db")
        economy_module.DB_PATH = path
        await db.setup()
        shop.invalidate()

    async def asyncTearDown(self):
        shop.invalidate()
        self.release_temp_database()

    async def _grant(self, item_id: str, now: float = 1_000_000.0):
        await shop.grant(BUYER, GUILD, shop.find(item_id)[0], now)

    async def test_a_boost_is_visible_immediately(self):
        await self._grant("xp_booster")
        totals = await shop.multipliers(BUYER, GUILD, 1_000_000.0 + 60)
        self.assertEqual(totals.xp, 1.5)
        self.assertEqual(totals.coins, 1.0)

    async def test_a_cheap_boost_cannot_extend_a_dear_one_at_a_discount(self):
        """Effect-minutes keep their value when a weaker item tops up a stronger one.

        Buying a 625-coin Sugar Rush to extend a running Oracle's Lens used to
        add a full 30 minutes of *x2* XP — a quarter of Oracle's Lens's own price
        per hour for the same multiplier, which made the cheap item the best way
        to extend the expensive one.
        """
        t0 = 1_000_000.0
        await self._grant("oracles_lens", t0)  # x2 XP for 3 h
        await self._grant("sugar_rush", t0)    # x1.25 XP for 30 min
        value, expires = (await shop.active_effects(BUYER, GUILD, t0))[shop.EFFECT_XP]
        self.assertEqual(value, 2.0)
        self.assertAlmostEqual(expires, t0 + 3 * 3600 + 30 * 60 * (1.25 / 2.0), places=3)
        self.assertLess(expires, t0 + 3 * 3600 + 30 * 60)

    async def test_an_extension_always_buys_some_time(self):
        """A far weaker item is worth little, but never nothing — it was paid for."""
        t0 = 1_000_000.0
        strong = shop.Item(
            id="t_strong", name="T Strong", tier=1, slot=1, summary="",
            effect=shop.Effect(minutes=60, xp_multiplier=5.0),
        )
        # 1 minute of x1.05 converts to 12.6 seconds of a x5 boost, which is below
        # the floor rather than a purchase that granted nothing at all.
        weak = shop.Item(
            id="t_weak", name="T Weak", tier=1, slot=1, summary="",
            effect=shop.Effect(minutes=1, xp_multiplier=1.05),
        )
        await shop.grant(BUYER, GUILD, strong, t0)
        await shop.grant(BUYER, GUILD, weak, t0)
        value, expires = (await shop.active_effects(BUYER, GUILD, t0))[shop.EFFECT_XP]
        self.assertEqual(value, 5.0)
        self.assertAlmostEqual(expires, t0 + 3600 + 60, places=3)

    async def test_a_shield_extension_stacks_by_time(self):
        t0 = 1_000_000.0
        await self._grant("ward_charm", t0)   # 12 h
        await self._grant("rob_shield", t0)   # 4 h
        value, expires = (await shop.active_effects(BUYER, GUILD, t0))[shop.EFFECT_SHIELD]
        self.assertEqual(value, 1.0)
        self.assertAlmostEqual(expires, t0 + 16 * 3600, places=3)

    async def test_a_duplicate_badge_row_counts_once(self):
        """A badge is folded in per inventory *row*, so a legacy duplicate doubled it."""
        badge = shop.find("vip_pass")[0]
        conn = db.connect()
        try:
            for _ in range(2):
                conn.execute(
                    "INSERT INTO inventory (user_id, guild_id, item_name) VALUES (?, ?, ?)",
                    (BUYER, GUILD, badge.name),
                )
            conn.commit()
        finally:
            conn.close()
        shop.invalidate(BUYER, GUILD)

        totals = await shop.multipliers(BUYER, GUILD, 1_000_000.0)
        self.assertEqual(totals.xp, badge.effect.xp_multiplier)
        self.assertEqual(totals.coins, badge.effect.coin_multiplier)
        self.assertEqual(len(await shop.owned_items(BUYER, GUILD)), 1)
        # The purchase history is untouched: `/inventory`'s quantity list still
        # shows two rows, only the perk counting is deduplicated.
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM inventory WHERE user_id=? AND guild_id=?", (BUYER, GUILD)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rows, 2)

    async def test_buying_the_same_boost_again_extends_it(self):
        await self._grant("xp_booster", 1_000_000.0)
        await self._grant("xp_booster", 1_000_000.0 + 600)  # 10 minutes later
        effects = await shop.active_effects(BUYER, GUILD, 1_000_000.0 + 600)
        _value, expires = effects[shop.EFFECT_XP]
        # The second purchase adds its two hours on to what was left, not on to now.
        self.assertAlmostEqual(expires, 1_000_000.0 + 2 * 120 * 60, places=3)

    async def test_a_weaker_boost_cannot_downgrade_a_stronger_one(self):
        await self._grant("oracles_lens")   # x2 for 3 h
        await self._grant("sugar_rush")     # x1.25 for 30 min
        totals = await shop.multipliers(BUYER, GUILD, 1_000_000.0 + 60)
        self.assertEqual(totals.xp, 2.0)

    async def test_stacking_is_capped_at_a_day(self):
        item = shop.find("eternal_flame")[0]  # 4 h of x3
        for _ in range(20):
            await shop.grant(BUYER, GUILD, item, 1_000_000.0)
        effects = await shop.active_effects(BUYER, GUILD, 1_000_000.0)
        _value, expires = effects[shop.EFFECT_XP]
        self.assertAlmostEqual(expires, 1_000_000.0 + shop.MAX_EFFECT_MINUTES * 60, places=3)

    async def test_expired_effects_stop_counting_and_are_dropped(self):
        await self._grant("xp_booster", 1_000_000.0)
        later = 1_000_000.0 + 3 * 3600
        totals = await shop.multipliers(BUYER, GUILD, later)
        self.assertEqual(totals.xp, 1.0)
        self.assertEqual(await shop.active_effects(BUYER, GUILD, later), {})

    async def test_a_shield_is_reported_as_a_shield(self):
        await self._grant("rob_shield", now=time.time())
        self.assertTrue(await shop.has_shield(BUYER, GUILD))

    async def test_luck_does_not_stack_but_is_still_capped(self):
        for _ in range(10):
            await self._grant("high_roller_chip", now=time.time())
        totals = await shop.multipliers(BUYER, GUILD)
        self.assertEqual(totals.luck, 20, "one effect key, so the strongest value wins")

        # Even a hand-written row cannot push a robbery past the cap.
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE item_effects SET value=999 WHERE user_id=? AND guild_id=? AND effect=?",
                (BUYER, GUILD, shop.EFFECT_LUCK),
            )
            conn.commit()
        finally:
            conn.close()
        shop.invalidate(BUYER, GUILD)
        self.assertEqual((await shop.multipliers(BUYER, GUILD)).luck, shop.MAX_LUCK_POINTS)

    async def test_badges_multiply_and_survive_expiry(self):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO inventory (user_id, guild_id, item_name) VALUES (?, ?, ?)",
                (BUYER, GUILD, shop.find("vip_pass")[0].name),
            )
            conn.execute(
                "INSERT INTO inventory (user_id, guild_id, item_name) VALUES (?, ?, ?)",
                (BUYER, GUILD, shop.find("royal_crown")[0].name),
            )
            conn.commit()
        finally:
            conn.close()

        shop.invalidate(BUYER, GUILD)
        totals = await shop.multipliers(BUYER, GUILD, 1_000_000.0)
        self.assertAlmostEqual(totals.coins, 1.10 * 1.25, places=5)
        self.assertAlmostEqual(totals.xp, 1.05 * 1.10, places=5)

    async def test_invalidate_makes_a_purchase_visible_at_once(self):
        """The leveling listener caches for a few seconds; a fresh perk must not wait."""
        await shop.multipliers(BUYER, GUILD, 1_000_000.0)   # populates the cache
        await self._grant("xp_booster")
        self.assertEqual((await shop.multipliers(BUYER, GUILD, 1_000_000.0 + 30)).xp, 1.5)


class _ShopCommands(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("shop_cmd.db")
        economy_module.DB_PATH = path
        await db.setup()
        shop.invalidate()
        self.cog = economy_module.Economy(bot=None)
        self.cards: list = []

    async def asyncTearDown(self):
        shop.invalidate()
        self.release_temp_database()

    async def _fake_send_card(self, _target, *args, **kwargs):
        self.cards.append(kwargs.get("embed"))
        return SimpleNamespace(id=1)

    def _seed_wallet(self, wallet: int, user_id: int = BUYER):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?, ?, ?, 0)",
                (user_id, GUILD, wallet),
            )
            conn.commit()
        finally:
            conn.close()

    def _answer(self, interaction):
        """Whatever this command told the user: a card, or an ephemeral refusal.

        The refusal paths (bad name, no funds) answer through ``response``, so a
        helper that only looked at captured cards would silently re-read the
        *previous* success card.
        """
        if self.cards:
            card = self.cards[-1]
            return SimpleNamespace(
                title=getattr(card, "title", "") or "",
                description=getattr(card, "description", "") or "",
                fields=getattr(card, "fields", []),
                footer=getattr(card, "footer", None),
            )
        content = interaction.response.messages[-1]["content"] if interaction.response.messages else ""
        return SimpleNamespace(title="", description=content or "", fields=[], footer=None)

    async def _buy(self, interaction, name: str, *, release_limit: bool = True):
        """Drive the real `/buy` callback.

        ``release_limit`` forgets the purchase rate limit first, because a test
        that buys two items in a row is not testing the rate limit: real members
        are seconds apart and these run in microseconds. The rate-limit tests
        pass ``release_limit=False`` to see the limit as a member would.
        """
        if release_limit:
            shop.clear_purchase_limits()
        self.cards.clear()
        with mock.patch.object(economy_module, "send_card", new=self._fake_send_card):
            await economy_module.Economy.buy.callback(self.cog, interaction, name)
        return self._answer(interaction)

    async def _inventory(self, interaction):
        self.cards.clear()
        with mock.patch.object(economy_module, "send_card", new=self._fake_send_card):
            await economy_module.Economy.inventory.callback(self.cog, interaction, None)
        return self._answer(interaction)

    async def _shop(self, interaction):
        self.cards.clear()
        with mock.patch.object(economy_module, "send_card", new=self._fake_send_card):
            await economy_module.Economy.shop.callback(self.cog, interaction)
        return self._answer(interaction)


class TestBuyCommand(_ShopCommands):
    async def test_buying_charges_records_and_starts_the_perk(self):
        self._seed_wallet(5_000)
        card = await self._buy(_FakeInteraction(), "xp booster")

        self.assertIn("Purchase Successful", card.title)
        self.assertEqual(_wallet(), 5_000 - 3_000)
        self.assertIn(shop.find("xp_booster")[0].name, _inventory())
        self.assertEqual((await shop.multipliers(BUYER, GUILD)).xp, 1.5)

    async def test_a_permanent_badge_cannot_be_bought_twice(self):
        # Enough for two crowns, so the refusal has to come from the ownership
        # check rather than from the wallet.
        self._seed_wallet(120_000)
        first = await self._buy(_FakeInteraction(), "royal crown")
        self.assertIn("permanently", first.description)
        self.assertEqual(_wallet(), 70_000)

        second = await self._buy(_FakeInteraction(), "royal crown")
        self.assertIn("already own", second.description)
        self.assertEqual(_wallet(), 70_000, "the second badge was charged for")
        self.assertEqual(_inventory().count(shop.find("royal_crown")[0].name), 1)

    async def test_a_boost_can_be_bought_again_on_purpose(self):
        self._seed_wallet(9_000)
        await self._buy(_FakeInteraction(), "xp booster")
        await self._buy(_FakeInteraction(), "xp booster")
        self.assertEqual(_wallet(), 3_000)
        self.assertEqual(_inventory().count(shop.find("xp_booster")[0].name), 2)

    async def test_you_cannot_buy_what_you_cannot_afford(self):
        self._seed_wallet(100)
        card = await self._buy(_FakeInteraction(), "vip pass")
        self.assertIn("You need", card.description)
        self.assertEqual(_wallet(), 100)

    async def test_an_ambiguous_name_is_refused_rather_than_guessed(self):
        self._seed_wallet(5_000)
        card = await self._buy(_FakeInteraction(), "coin")
        self.assertIn("matches more than one item", card.description)
        self.assertEqual(_wallet(), 5_000, "an ambiguous name spent coins")

    async def test_a_purchase_starts_a_rate_limit(self):
        self._seed_wallet(50_000)
        bought = await self._buy(_FakeInteraction(), "sugar rush")
        self.assertIn("Purchase Successful", bought.title)

        blocked = await self._buy(_FakeInteraction(), "coin charm", release_limit=False)
        self.assertIn("One purchase at a time", blocked.description)
        # Only the first purchase was charged for.
        self.assertEqual(_wallet(), 50_000 - shop.find("sugar_rush")[0].price)
        self.assertEqual(_inventory().count(shop.find("coin_charm")[0].name), 0)

    async def test_a_refused_purchase_does_not_start_the_rate_limit(self):
        """A typo or an empty wallet must not lock a member out of the shop."""
        self._seed_wallet(100)
        refused = await self._buy(_FakeInteraction(), "royal crown")
        self.assertIn("You need", refused.description)

        self._seed_wallet(50_000)
        bought = await self._buy(_FakeInteraction(), "sugar rush", release_limit=False)
        self.assertIn("Purchase Successful", bought.title)

    async def test_the_purchase_card_shows_what_is_actually_running(self):
        """A weaker item topping up a stronger one must not over-state itself."""
        self._seed_wallet(50_000)
        await self._buy(_FakeInteraction(), "oracle's lens")
        card = await self._buy(_FakeInteraction(), "sugar rush")

        self.assertIn("Active now", card.description)
        # The running perk is the x2 one, and its clock is the converted total.
        self.assertIn("×2 XP", card.description)
        self.assertNotIn("×1.25 XP", card.description)

    async def test_an_unknown_name_says_so(self):
        self._seed_wallet(5_000)
        card = await self._buy(_FakeInteraction(), "definitely not an item")
        self.assertIn("Nothing in the shop", card.description)

    async def test_a_wallet_that_moved_mid_purchase_is_refused(self):
        """The charge is conditional, so a stale read cannot buy for free."""
        self._seed_wallet(0)

        async def _stale_read(_user_id, _guild_id, is_owner=None):
            return {"wallet": 90_000, "bank": 0, "last_daily": None, "last_weekly": None, "last_work": None}

        with mock.patch.object(economy_module, "get_eco", new=_stale_read):
            card = await self._buy(_FakeInteraction(), "rob shield")

        self.assertIn("wallet changed", card.description)
        self.assertEqual(_inventory(), [], "an unpaid purchase granted the item")

    async def test_a_scratch_card_pays_out_and_is_recorded(self):
        ticket = shop.find("scratch_card")[0]
        self._seed_wallet(1_500)
        with mock.patch.object(shop.random, "randint", return_value=1_000):
            card = await self._buy(_FakeInteraction(), "scratch card")
        self.assertIn("1,000", card.description)
        # The ticket's price spent, the top payout paid back.
        self.assertEqual(_wallet(), 1_500 - ticket.price + 1_000)
        self.assertIn(ticket.name, _inventory())

    async def test_the_shop_lists_every_item_and_price(self):
        card = await self._shop(_FakeInteraction())
        rendered = "\n".join(
            f"{field.name}\n{field.value}" for field in card.fields
        )
        for item in shop.all_items():
            self.assertIn(item.name, rendered, f"{item.name} is missing from /shop")
            self.assertIn(f"{item.price:,}", rendered, f"{item.price:,} is missing")
            # The flavour text is written for this card; a catalog field that
            # never reaches a player is a field that silently rots.
            self.assertIn(item.summary, rendered, f"{item.name}'s summary is unshown")

    async def test_the_shop_card_stays_inside_discord_limits(self):
        """Adding items is exactly what breaks this, and Discord rejects the whole
        message when it does — the shop renders as nothing at all."""
        card = await self._shop(_FakeInteraction())
        total = len(card.title) + len(card.description)
        for field in card.fields:
            self.assertLessEqual(len(field.name), 256, field.name)
            self.assertLessEqual(len(field.value), 1024, field.name)
            total += len(field.name) + len(field.value)
        # 25 fields per embed and 6,000 characters across the whole thing. Every
        # tier is one field, so the catalog would have to grow past 100 items to
        # reach the field cap.
        self.assertLessEqual(len(card.fields), 25)
        self.assertLessEqual(total, 6000)


class TestInventoryCard(_ShopCommands):
    async def test_active_perks_and_badges_are_shown(self):
        self._seed_wallet(20_000)
        await self._buy(_FakeInteraction(), "vip pass")
        await self._buy(_FakeInteraction(), "xp booster")

        card = await self._inventory(_FakeInteraction())
        section = "\n".join(f"{field.name}\n{field.value}" for field in card.fields)
        self.assertIn("Active Perks", section)
        self.assertIn("×1.5 XP", section)
        self.assertIn("left", section)
        self.assertIn("Permanent Badges", section)
        self.assertIn("VIP Pass", section)


class TestRobPerks(_ShopCommands):
    def _rob_interaction(self):
        interaction = _FakeInteraction()
        interaction.guild.owner_id = 1
        return interaction

    async def _rob(self, interaction, victim_id: int = VICTIM):
        victim = _FakeUser(victim_id, name="Victim")
        self.cards.clear()
        with mock.patch.object(economy_module, "send_card", new=self._fake_send_card):
            await economy_module.Economy.rob.callback(self.cog, interaction, victim)
        return self._answer(interaction)

    async def test_a_shield_blocks_the_robbery(self):
        self._seed_wallet(10_000, BUYER)
        self._seed_wallet(10_000, VICTIM)
        await shop.grant(VICTIM, GUILD, shop.find("rob shield")[0], now=None)

        card = await self._rob(self._rob_interaction())

        self.assertIn("Shielded", card.title)
        self.assertEqual(_wallet(VICTIM), 10_000, "a shielded member was robbed")
        self.assertEqual(_wallet(BUYER), 10_000)

    async def test_luck_actually_swings_the_roll(self):
        """The same dice roll fails at base odds and succeeds with the perk."""
        self._seed_wallet(10_000, BUYER)
        self._seed_wallet(10_000, VICTIM)
        # A roll between the base 45% chance and the boosted one.
        roll = 0.50
        fake_random = SimpleNamespace(random=lambda: roll, randint=random.randint)

        with mock.patch.object(economy_module, "random", fake_random):
            caught = await self._rob(self._rob_interaction())
        self.assertIn("Caught", caught.title)

        self._seed_wallet(10_000, BUYER)
        self._seed_wallet(10_000, VICTIM)
        await shop.grant(BUYER, GUILD, shop.find("high_roller_chip")[0], now=None)

        with mock.patch.object(economy_module, "random", fake_random):
            success = await self._rob(self._rob_interaction())
        self.assertIn("Successful Robbery", success.title)
        self.assertIn("+20% from perks", success.footer.text)


if __name__ == "__main__":
    unittest.main()
