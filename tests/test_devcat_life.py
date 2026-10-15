"""The pet's life cycle: what it eats, what that costs, and what happens if nobody pays.

The Dev Cat was a toy with three dials and a rate limit. This is the pass that gave
it a **stake**: four kinds of animal, food that costs the server's own coins, care
that pays a few back, a pet that asks for a bowl when the bowl runs low, and a pet
that is *gone* if nobody answers. Each of those is a way for a pet feature to stop
being a toy, so each one has its laws here rather than in the other file:

* **The tables.** A species is a rate and a price, not a costume — the cat is every
  multiplier at 1.0, which is what keeps the arithmetic ``tests/test_devtoy.py``
  already pins exactly the cat's own numbers.
* **The money.** The rules own the *prices* and the cog owns the wallet, so a bowl
  costs what the card says and a member who cannot afford one is refused **without**
  being served. That is asserted through the real cog, over a throwaway database,
  against the real economy functions.
* **The clock does not need a job.** Death is derived from the row, so a pet nobody
  has looked at in a month is dead when it is finally asked about — and the deadline
  is exactly the grace period after the instant the bowl emptied.
* **The asks.** A hungry pet gets its own allowance, separate from the server's quiet
  cards, and a dead pet never asks. The obituary goes out **once** and is remembered
  on the row.
* **Nothing may be said with a mention in it.** Every line the new cards can carry is
  walked here, for the reason the ambient ones already are: these are the cards whose
  whole job is to reach one member, and the ping is a switch rather than a promise.
"""
import importlib
import os
import random
import sys
import time
import types
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import discord

from cogs import devtoy as devtoy_cog
from cogs.devtoy import DevToy
from cogs.economy import get_eco
from tests.test_devtoy import (
    IST, NOW, CogCase, card_text, fake_guild, fake_interaction, refusal_text, sent_card,
)
from utils import bot_channels, db, devtoy, features, permissions
from utils.db import truncate_all_tables

DEV_GUILD = bot_channels.DEV_SERVER_GUILD_ID
OTHER_GUILD = 777001
MEMBER = 5


async def wallet(guild_id: int, user_id: int) -> int:
    """What a member can spend right now, read through the economy's own door."""
    return int((await get_eco(user_id, guild_id))["wallet"])


def a_pet(**fields) -> devtoy.Cat:
    """A cat-shaped pet with the given fields, for the pure rules."""
    return devtoy.Cat(**fields)


# --------------------------------------------------------------------------- #
# The tables
# --------------------------------------------------------------------------- #
class TheSpeciesAreRatesNotCostumes(unittest.TestCase):
    def test_the_cat_is_free_and_is_the_default(self):
        cat = devtoy.species_for("cat")
        self.assertEqual(cat.adopt_cost, 0)
        self.assertEqual(devtoy.species_for(""), cat)
        self.assertEqual(devtoy.species_for("who-knows"), cat)
        self.assertEqual(devtoy.DEFAULT_SPECIES, "cat")

    def test_every_species_is_described_and_identifies_itself(self):
        ids = [spec.id for spec in devtoy.SPECIES]
        self.assertEqual(len(ids), len(set(ids)), "two species sharing an id")
        for spec in devtoy.SPECIES:
            self.assertTrue(spec.label and spec.blurb and spec.emoji, spec.id)
            self.assertGreaterEqual(spec.adopt_cost, 0)
            self.assertGreater(spec.food_markup, 0)
            self.assertGreaterEqual(spec.care_coins, 0)

    def test_the_cat_is_every_multiplier_at_one(self):
        """Which is what makes ``FED_PER_HOUR`` and friends the cat's own numbers."""
        cat = devtoy.species_for("cat")
        self.assertEqual((cat.appetite, cat.mood_rate, cat.rest_rate), (1.0, 1.0, 1.0))
        self.assertEqual(cat.food_markup, 1.0)

    def test_the_species_passes_through_the_reader(self):
        """A caller holding a Species is asking about the same animal (a shop card
        for somebody who has no pet yet)."""
        dragon = devtoy.species_for("dragon")
        self.assertIs(devtoy.species_of(dragon), dragon)

    def test_an_unknown_row_species_reads_as_a_cat(self):
        self.assertEqual(devtoy.species_of(devtoy.Cat(species="")).id, "cat")
        self.assertEqual(devtoy.species_of(devtoy.Cat(species="phoenix")).id, "cat")
        self.assertEqual(devtoy.species_of(devtoy.Cat()).id, "cat")

    def test_a_hungrier_animal_really_does_get_hungrier(self):
        """The species moves the clock rather than the card: a dog eats faster than
        a cat over the same six hours, and a robot slower."""
        start = devtoy.adopt(NOW, "Biscuit")
        later = NOW + 6 * 3600
        eaten = {
            spec.id: devtoy.drift(replace(start, species=spec.id), later).fed
            for spec in devtoy.SPECIES
        }
        self.assertLess(eaten["dog"], eaten["cat"])
        self.assertLess(eaten["cat"], eaten["robot"])
        self.assertAlmostEqual(eaten["cat"], start.fed - devtoy.FED_PER_HOUR * 6, places=3)

    def test_a_dog_forgives_faster_than_a_dragon_sulks(self):
        start = devtoy.adopt(NOW, "Biscuit")
        later = NOW + 6 * 3600
        moods = {
            spec.id: devtoy.drift(replace(start, species=spec.id), later).mood
            for spec in devtoy.SPECIES
        }
        self.assertGreater(moods["dog"], moods["cat"])
        self.assertLess(moods["dragon"], moods["cat"])

    def test_adopting_records_the_species_and_refuses_a_nonsense_one(self):
        self.assertEqual(devtoy.adopt(NOW, "Rex", "dog").species, "dog")
        self.assertEqual(devtoy.adopt(NOW, "Rex", "kraken").species, "cat")

    def test_the_face_of_a_well_pet_is_the_pet(self):
        for spec in devtoy.SPECIES:
            well = devtoy.Cat(species=spec.id, fed=80, mood=80, energy=80)
            if spec.id == "cat":
                self.assertEqual(devtoy.face(well), "😻")
            else:
                self.assertEqual(devtoy.face(well), spec.emoji)
        # Starving is the same glyph for every animal: it is the one state that has
        # to read at a glance whichever pet it is.
        for spec in devtoy.SPECIES:
            self.assertEqual(devtoy.face(devtoy.Cat(species=spec.id, fed=0)), "🙀")


class FoodIsAPurchase(unittest.TestCase):
    def test_every_bowl_has_a_price_a_name_and_something_it_gives(self):
        ids = [item.id for item in devtoy.FOODS]
        self.assertEqual(len(ids), len(set(ids)))
        prices = [item.price for item in devtoy.FOODS]
        self.assertEqual(prices, sorted(prices), "the menu is not ordered by price")
        for item in devtoy.FOODS:
            self.assertTrue(item.label and item.blurb, item.id)
            self.assertGreater(item.price, 0)
            self.assertGreater(item.fed, 0)

    def test_a_bigger_animal_pays_more_for_the_same_bowl(self):
        cat = devtoy.adopt(NOW)
        dragon = replace(cat, species="dragon")
        robot = replace(cat, species="robot")
        bowl = devtoy.FOODS[1]
        self.assertEqual(devtoy.feed_price(cat, bowl), bowl.price)
        self.assertGreater(devtoy.feed_price(dragon, bowl), bowl.price)
        self.assertLess(devtoy.feed_price(robot, bowl), bowl.price)

    def test_a_bowl_is_never_free(self):
        """A markup that rounds a cheap bowl to nothing would be a free lunch."""
        cheapest = min(devtoy.FOODS, key=lambda item: item.price)
        for spec in devtoy.SPECIES:
            self.assertGreaterEqual(devtoy.feed_price(replace(devtoy.Cat(), species=spec.id), cheapest), 1)

    def test_an_unknown_bowl_gets_the_house_food(self):
        self.assertIs(devtoy.choose_food("caviar"), devtoy.HOUSE_FOOD)
        self.assertIs(devtoy.choose_food(""), devtoy.HOUSE_FOOD)
        self.assertIs(devtoy.choose_food("feast"), devtoy.FOODS[2])


# --------------------------------------------------------------------------- #
# Feeding, and what it costs
# --------------------------------------------------------------------------- #
class FeedingIsBoughtAndRefused(unittest.TestCase):
    def test_a_bowl_feeds_what_the_bowl_says(self):
        cat = devtoy.adopt(NOW, "Biscuit")
        hungry = replace(cat, fed=10.0)
        meal = devtoy.FOODS[1]
        result = devtoy.care(hungry, "feed", NOW + 1, food=meal)
        self.assertTrue(result.ok)
        # The one second between the row and the action is one second of drift.
        self.assertAlmostEqual(result.cat.fed, 10.0 + meal.fed, places=2)
        self.assertGreater(result.cat.mood, hungry.mood)

    def test_the_rule_reports_the_price_and_moves_no_money(self):
        """The prices are this module's; the wallet is the cog's, so a rule that
        moved coins would be a second place a balance could come from."""
        cat = replace(devtoy.adopt(NOW, "Biscuit"), species="dragon")
        result = devtoy.care(cat, "feed", NOW + 1, food=devtoy.FOODS[2])
        self.assertTrue(result.ok)
        self.assertEqual(result.cost, devtoy.feed_price(cat, devtoy.FOODS[2]))
        self.assertEqual(result.earn, 0, "a bowl is something you buy, not a faucet")

    def test_a_full_bowl_is_refused_rather_than_sold(self):
        cat = replace(devtoy.adopt(NOW, "Biscuit"), fed=100.0)
        result = devtoy.care(cat, "feed", NOW + 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.headline, "Not hungry")
        self.assertEqual(result.cost, 0, "a refusal must not be charged for")

    def test_care_pays_the_species_rate_and_climbs_with_the_care_level(self):
        for spec in devtoy.SPECIES:
            fresh = replace(devtoy.adopt(NOW, "Biscuit"), species=spec.id)
            petted = devtoy.care(fresh, "pet", NOW + 1)
            self.assertTrue(petted.ok)
            self.assertEqual(petted.earn, devtoy.care_coins(fresh))
            self.assertEqual(petted.cat.coins, petted.earn)
            old = replace(fresh, care_score=300)
            self.assertGreater(devtoy.care_coins(old), devtoy.care_coins(fresh))

    def test_the_pet_that_pays_nothing_still_works(self):
        """``CARE_COIN_BONUS_PER_LEVEL`` is a bonus, not the whole payback."""
        robot = replace(devtoy.adopt(NOW, "Biscuit"), species="robot")
        self.assertGreater(devtoy.care_coins(robot), 0)


# --------------------------------------------------------------------------- #
# Starving, dying, and coming back
# --------------------------------------------------------------------------- #
class APetNobodyFeedsIsGone(unittest.TestCase):
    def test_the_deadline_is_exactly_the_grace_after_the_bowl_emptied(self):
        # 21 food at 7/hour empties in exactly three hours.
        cat = a_pet(fed=devtoy.FED_PER_HOUR * 3, updated_at=NOW)
        empty_at = NOW + 3 * 3600
        self.assertAlmostEqual(
            devtoy.died_at(cat, empty_at),
            empty_at + devtoy.DEATH_GRACE,
            places=3,
        )
        self.assertFalse(devtoy.is_dead(cat, empty_at + devtoy.DEATH_GRACE - 1))
        self.assertTrue(devtoy.is_dead(cat, empty_at + devtoy.DEATH_GRACE))

    def test_a_pet_with_food_in_the_bowl_is_not_dead_yet(self):
        """A full bowl is not a promise for ever: it is *not dead yet*, and the
        countdown only starts when the food actually runs out."""
        full = devtoy.adopt(NOW, "Biscuit")
        self.assertFalse(devtoy.is_dead(full, NOW))
        # 75 food at a cat's 7/hour empties in about ten and a half hours, and the
        # grace is counted from *that* rather than from the last visit.
        empty_at = NOW + (full.fed / devtoy.FED_PER_HOUR) * 3600
        deadline = empty_at + devtoy.DEATH_GRACE
        self.assertFalse(devtoy.is_dead(full, deadline - 1))
        self.assertTrue(devtoy.is_dead(full, deadline))

    def test_the_countdown_shrinks_and_stops_at_nothing(self):
        cat = a_pet(fed=devtoy.FED_PER_HOUR, updated_at=NOW)
        self.assertAlmostEqual(devtoy.dies_in(cat, NOW + 3600), devtoy.DEATH_GRACE, places=3)
        self.assertGreater(
            devtoy.dies_in(cat, NOW + 3600), devtoy.dies_in(cat, NOW + 3600 + devtoy.DAY)
        )
        self.assertEqual(devtoy.dies_in(cat, NOW + 10 * devtoy.DAY), 0.0)

    def test_care_stops_working_on_a_pet_that_is_gone(self):
        dead = a_pet(fed=0, mood=0, energy=50, updated_at=NOW - 5 * devtoy.DAY)
        for action in ("feed", "pet", "play"):
            result = devtoy.care(dead, action, NOW)
            self.assertFalse(result.ok, action)
            self.assertEqual(result.headline, "Gone", action)
            self.assertTrue(result.dead, action)
            self.assertIn("/cat revive", result.detail)

    def test_a_revival_keeps_the_pet_and_counts_the_life(self):
        dead = a_pet(
            name="Biscuit", fed=0, mood=0, energy=10, care_score=140, care_days=9,
            adopted_at=NOW - 30 * devtoy.DAY, updated_at=NOW - 5 * devtoy.DAY,
            games=7, coins=4321, deaths=1,
        )
        back = devtoy.revive(dead, NOW)
        self.assertFalse(devtoy.is_dead(back, NOW))
        self.assertEqual(back.name, "Biscuit", "a revival is not a new pet")
        self.assertEqual((back.care_score, back.care_days, back.games), (140, 9, 7))
        self.assertEqual(back.coins, 4321)
        self.assertEqual(back.deaths, 2)
        self.assertEqual((back.fed, back.mood, back.energy), (devtoy.REVIVE_FED, devtoy.REVIVE_MOOD, devtoy.REVIVE_ENERGY))
        self.assertEqual(back.starving_since, 0.0)
        self.assertEqual((back.last_fed, back.last_pet, back.last_play), (0.0, 0.0, 0.0))

    def test_lives_lost_counts_the_life_it_is_on_right_now(self):
        dead = a_pet(fed=0, updated_at=NOW - 5 * devtoy.DAY, deaths=2)
        self.assertEqual(devtoy.lives_lost(dead, NOW), 3)
        self.assertEqual(devtoy.lives_lost(devtoy.revive(dead, NOW), NOW), 3)
        self.assertEqual(devtoy.lives_lost(devtoy.adopt(NOW), NOW), 0)

    def test_losing_a_pet_is_on_the_card(self):
        dead = a_pet(fed=0, updated_at=NOW - 5 * devtoy.DAY, deaths=1)
        self.assertTrue(any("Lost 2 lives" in badge for badge in devtoy.badges(dead, NOW)))
        self.assertTrue(any("Lost one life" in badge for badge in devtoy.badges(
            replace(dead, deaths=0, updated_at=NOW - 2 * devtoy.DAY), NOW
        )))

    def test_affection_does_not_put_anything_in_the_bowl(self):
        """The starvation clock is cleared by food. Petting a starving pet is not a
        rescue, and a clock that stopped for it would be one."""
        starving = a_pet(fed=0, mood=20, energy=80, updated_at=NOW - devtoy.HOUR,
                         starving_since=NOW - devtoy.HOUR)
        petted = devtoy.care(starving, "pet", NOW)
        self.assertTrue(petted.ok)
        self.assertEqual(petted.cat.starving_since, NOW - devtoy.HOUR)
        fed = devtoy.care(starving, "feed", NOW)
        self.assertTrue(fed.ok)
        self.assertEqual(fed.cat.starving_since, 0.0)

    def test_a_pet_nobody_has_opened_still_dies_on_time(self):
        """Derived, so the tick does not have to have run for the pet to be dead."""
        long_ago = NOW - 10 * devtoy.DAY
        cat = a_pet(fed=10.0, mood=50, energy=50, updated_at=long_ago, adopted_at=long_ago)
        self.assertEqual(cat.starving_since, 0.0, "the row itself was never touched")
        self.assertTrue(devtoy.is_dead(cat, NOW))


# --------------------------------------------------------------------------- #
# The pet asks for food
# --------------------------------------------------------------------------- #
class AHungryPetAsksItsOwnOwner(unittest.TestCase):
    def test_a_full_bowl_is_not_hungry(self):
        full = devtoy.adopt(NOW, "Biscuit")
        self.assertFalse(devtoy.hunger_due(full, NOW).ok)

    def test_a_low_bowl_is(self):
        low = replace(devtoy.adopt(NOW, "Biscuit"), fed=devtoy.HUNGRY_BELOW - 1)
        self.assertTrue(devtoy.hunger_due(low, NOW).ok)

    def test_the_switch_is_the_switch(self):
        low = replace(devtoy.adopt(NOW, "Biscuit"), fed=5.0)
        decision = devtoy.hunger_due(low, NOW, enabled=False)
        self.assertFalse(decision.ok)
        self.assertIn("switched off", decision.reason)

    def test_the_gap_holds_and_the_mark_is_what_holds_it(self):
        low = replace(devtoy.adopt(NOW, "Biscuit"), fed=5.0)
        marked = devtoy.mark_hunger(low, NOW)
        early = devtoy.hunger_due(marked, NOW + 60)
        self.assertFalse(early.ok)
        self.assertIn("ago", early.reason)
        self.assertTrue(devtoy.hunger_due(marked, NOW + devtoy.HUNGER_MIN_GAP).ok)

    def test_six_asks_a_day_and_no_more(self):
        low = replace(devtoy.adopt(NOW, "Biscuit"), fed=0.0)
        row, now = low, NOW
        for _ in range(devtoy.HUNGER_DAILY_CAP):
            self.assertTrue(devtoy.hunger_due(row, now).ok)
            row = devtoy.mark_hunger(row, now)
            now += devtoy.HUNGER_MIN_GAP
        blocked = devtoy.hunger_due(row, now)
        self.assertFalse(blocked.ok)
        self.assertIn("today", blocked.reason)

    def test_the_allowance_starts_again_tomorrow(self):
        low = replace(devtoy.adopt(NOW, "Biscuit"), fed=0.0)
        row, now = low, NOW
        for _ in range(devtoy.HUNGER_DAILY_CAP):
            row = devtoy.mark_hunger(row, now)
            now += devtoy.HUNGER_MIN_GAP
        tomorrow = now + 24 * 3600
        self.assertTrue(devtoy.hunger_due(row, tomorrow).ok)

    def test_the_day_the_cap_counts_is_the_operators_day(self):
        """Same reason the quiet cards use IST: a day that turned over at lunchtime
        would hand out six more asks in the afternoon."""
        low = replace(devtoy.adopt(NOW, "Biscuit"), fed=0.0)
        row = devtoy.mark_hunger(low, NOW)
        self.assertEqual(row.ask_day, devtoy.care_day(NOW))
        # 23:01 IST and two hours later are different days.
        first = devtoy.mark_hunger(low, 1792085460.0)
        self.assertNotEqual(first.ask_day, devtoy.care_day(1792085460.0 + 2 * 3600))

    def test_a_pet_that_is_gone_never_asks(self):
        dead = a_pet(fed=0, updated_at=NOW - 5 * devtoy.DAY)
        decision = devtoy.hunger_due(dead, NOW)
        self.assertFalse(decision.ok)
        self.assertIn("gone", decision.reason)

    def test_the_card_names_the_price_and_the_deadline(self):
        cat = replace(devtoy.adopt(NOW, "Biscuit"), species="dragon", fed=10.0)
        title, body = devtoy.hunger_text(cat, NOW)
        self.assertIn("hungry", title)
        self.assertIn(f"**{devtoy.feed_price(cat)}**", body)
        self.assertIn("/cat feed", body)
        self.assertIn(devtoy.humanize(devtoy.dies_in(cat, NOW)), body)

    def test_the_obituary_says_how_to_undo_it(self):
        dead = a_pet(name="Biscuit", fed=0, updated_at=NOW - 5 * devtoy.DAY)
        title, body = devtoy.death_text(dead, NOW)
        self.assertIn("Biscuit", title)
        self.assertIn("Biscuit is gone", body)
        self.assertIn("/cat revive", body)

    def test_every_line_the_new_cards_can_carry_is_mention_free(self):
        """These are the cards a ping is *for*, which is why the text may not carry
        one: the switch decides whether somebody is notified, and the words alone
        are what is left when it is off."""
        cat = replace(devtoy.adopt(NOW, "Biscuit"), fed=5.0)
        dead = a_pet(name="Biscuit", fed=0, updated_at=NOW - 5 * devtoy.DAY)
        rng = random.Random(11)
        texts = []
        for spec in devtoy.SPECIES:
            for _ in range(10):
                texts.append("\n".join(devtoy.hunger_text(replace(cat, species=spec.id), NOW, rng=rng)))
        for _ in range(20):
            texts.append("\n".join(devtoy.death_text(dead, NOW, rng=rng)))
            texts.append(devtoy.pick(devtoy.REVIVE_LINES, rng).format(name="Biscuit"))
            for spec in devtoy.SPECIES:
                for action in ("feed", "pet", "play"):
                    texts.append(devtoy.care_line(action, replace(cat, species=spec.id), NOW, rng=rng)[1])
        self.assertTrue(texts)
        for text in texts:
            self.assertTrue(text.strip())
            for mention in ("@everyone", "@here", "<@", "<@&", "<#"):
                self.assertNotIn(mention, text, text)


# --------------------------------------------------------------------------- #
# Through the cog, over the real economy
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def stale_path_copies() -> list[tuple[dict, str]]:
    """Every copy of the project's database path that is not this test's file.

    ``cogs/economy.py`` — and five siblings — wrote ``DB_PATH = db.DB_PATH`` the
    first time each was imported, so each keeps its own copy from then on and
    switching ``utils.db.DB_PATH`` reaches none of them. That is a trap here for
    two reasons, and both have been caught red-handed in a full run:

    * The site and preview suites put the project through *another* import, so a
      later ``from cogs.economy import update_wallet`` can land on a second copy
      whose captured path was a temp file that has since been deleted. The debit
      then raises *unable to open database file* and the coins never move.
    * A copy can also hold a path that is perfectly *alive* — the suite's own
      database is one — and then the money moves, but in the wrong file, which for
      a test is the quieter and worse failure: the assertion reads a wallet the
      debit never touched, and the cog looks broken.

    So the copies are found by walking ``sys.modules`` and, from each module, the
    functions and classes it holds: a copy that has been dropped from
    ``sys.modules`` is still reachable as the doors a cog imported out of it.
    Whatever is returned is the globals dictionaries that had to move, with what
    they held, so they can be handed back as they were found.
    """
    moved: dict[int, tuple[dict, str]] = {}

    def consider(namespace) -> None:
        if not isinstance(namespace, dict) or namespace is db.__dict__:
            return
        origin = namespace.get("__file__")
        if not isinstance(origin, str) or not os.path.abspath(origin).startswith(ROOT + os.sep):
            return  # not one of ours: nothing in it is the project's path
        held = namespace.get("DB_PATH")
        if isinstance(held, str) and held != db.DB_PATH:
            moved[id(namespace)] = (namespace, held)

    for module in list(sys.modules.values()):
        if not isinstance(module, types.ModuleType):
            continue
        consider(module.__dict__)
        for member in list(module.__dict__.values()):
            if isinstance(member, (types.FunctionType, types.MethodType)):
                consider(getattr(member, "__globals__", None))
            elif isinstance(member, type):
                for attribute in list(member.__dict__.values()):
                    consider(getattr(attribute, "__globals__", None))
    return list(moved.values())


class EconomyCase(CogCase):
    """A cog case whose coin doors point at *this* test's database.

    The rules own the prices and the cog owns the wallet, so these tests are only
    about anything at all if the wallet is really there — which means the economy
    has to be looking at **this** test's file. It keeps its own copy of the path,
    and so does every other module that wrote the path down at import, so this
    re-points them all (see :func:`stale_path_copies`) and hands them back as they
    were found on the way out.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # ``import_module`` rather than the import at the top of this file: the
        # *registered* module is the one a call-time ``from cogs.economy import
        # ...`` resolves to, and asking for it by name also puts it back into
        # ``sys.modules`` when a suite has removed it — which is exactly how a
        # second copy with a long-deleted path gets reached in the first place.
        importlib.import_module("cogs.economy")
        self._path_copies = stale_path_copies()
        for namespace, _ in self._path_copies:
            namespace["DB_PATH"] = db.DB_PATH
        self.wallet = wallet
        # A clean purse for each test: the wallet is read-then-added below, and a
        # row left behind by the previous test would be counted twice.
        await truncate_all_tables()

    async def asyncTearDown(self):
        for namespace, held in self._path_copies:
            namespace["DB_PATH"] = held
        await super().asyncTearDown()


class TheCogSpendsAndEarnsRealCoins(EconomyCase):

    async def _give(self, amount: int) -> int:
        """Top a member up and hand back what they hold now.

        Deliberately read-then-add rather than *set*: the economy's own door only
        ever adds, and the wallet table is shared with every other suite in this
        checkout — so a test that asserted an absolute balance would be asserting
        what the *rest of the session* had left behind.
        """
        from cogs.economy import update_wallet

        await update_wallet(MEMBER, DEV_GUILD, int(amount))
        return await self.wallet(DEV_GUILD, MEMBER)

    async def _adopt(self, *, species: str = "", name: str = "Biscuit", user_id: int = MEMBER):
        interaction = fake_interaction(user_id=user_id)
        await DevToy.cat_adopt.callback(self.cog, interaction, name, species)
        return interaction

    async def test_adopting_a_species_costs_its_price(self):
        before = await self._give(900)
        interaction = await self._adopt(species="dog")
        self.assertEqual(await self.wallet(DEV_GUILD, MEMBER), before - 500)
        stored = await self.cog._load(DEV_GUILD, MEMBER)
        self.assertEqual(stored.species, "dog")
        self.assertIn(str(before - 500), card_text(sent_card(interaction)), "the card says what is left")

    async def test_a_pet_that_costs_too_much_is_not_adopted(self):
        before = await self._give(10)
        interaction = await self._adopt(species="dragon")
        self.assertIsNone(await self.cog._load(DEV_GUILD, MEMBER), "nothing moved in")
        self.assertEqual(await self.wallet(DEV_GUILD, MEMBER), before, "and nothing was charged")
        self.assertIn("Not enough coins", card_text(sent_card(interaction)))

    async def test_the_free_pet_needs_no_coins_at_all(self):
        before = await self.wallet(DEV_GUILD, MEMBER)
        await self._adopt(species="")
        stored = await self.cog._load(DEV_GUILD, MEMBER)
        self.assertEqual(stored.species, "cat")
        self.assertEqual(await self.wallet(DEV_GUILD, MEMBER), before)

    async def test_feeding_charges_the_wallet_and_saves_the_pet(self):
        before = await self._give(100)
        await self.cog._save(DEV_GUILD, MEMBER, replace(devtoy.adopt(NOW), fed=10.0))
        interaction = fake_interaction(user_id=MEMBER)
        await DevToy.cat_feed.callback(self.cog, interaction, "meal")
        price = devtoy.feed_price(devtoy.adopt(NOW), devtoy.FOODS[1])
        self.assertEqual(await self.wallet(DEV_GUILD, MEMBER), before - price)
        stored = await self.cog._load(DEV_GUILD, MEMBER)
        self.assertGreater(stored.fed, 10.0)
        text = card_text(sent_card(interaction))
        self.assertIn(str(price), text)
        self.assertIn("paid", text)

    async def test_a_bowl_that_cannot_be_paid_for_is_not_served(self):
        """The whole point of a conditional debit: an unconditional one clamps at
        zero, so a member with two coins would get a seventy-coin meal.

        The member is *emptied* first rather than set to two: this door only adds,
        and the purse is shared with the rest of the session.
        """
        held = await self.wallet(DEV_GUILD, MEMBER)
        if held:
            from cogs.economy import take_stake

            await take_stake(MEMBER, DEV_GUILD, held)
        before = await self._give(2)
        hungry = replace(devtoy.adopt(NOW), fed=10.0)
        await self.cog._save(DEV_GUILD, MEMBER, hungry)
        interaction = fake_interaction(user_id=MEMBER)
        await DevToy.cat_feed.callback(self.cog, interaction, "meal")
        self.assertEqual(await self.wallet(DEV_GUILD, MEMBER), before, "no coins were taken")
        stored = await self.cog._load(DEV_GUILD, MEMBER)
        self.assertEqual(stored.fed, hungry.fed, "and no bowl was served")
        self.assertEqual(stored.last_fed, 0.0, "nor was the cooldown spent")
        text = card_text(sent_card(interaction))
        self.assertIn("Not enough coins", text)
        self.assertIn(str(devtoy.feed_price(hungry, devtoy.FOODS[1])), text, "the refusal names the price")

    async def test_the_panels_own_feed_button_buys_the_house_food(self):
        before = await self._give(50)
        await self.cog._save(DEV_GUILD, MEMBER, replace(devtoy.adopt(NOW), fed=10.0))
        interaction = fake_interaction(user_id=MEMBER)
        interaction.message = SimpleNamespace(edit=mock.AsyncMock())
        await self.cog.care_button(interaction, "feed")
        self.assertEqual(await self.wallet(DEV_GUILD, MEMBER), before - devtoy.HOUSE_FOOD.price)
        self.assertGreater((await self.cog._load(DEV_GUILD, MEMBER)).fed, 10.0)

    async def test_care_pays_the_purse_back(self):
        await self.cog._save(DEV_GUILD, MEMBER, devtoy.adopt(NOW))
        before = await self.wallet(DEV_GUILD, MEMBER)
        interaction = fake_interaction(user_id=MEMBER)
        await DevToy.cat_pet.callback(self.cog, interaction)
        self.assertEqual(
            await self.wallet(DEV_GUILD, MEMBER), before + devtoy.care_coins(devtoy.adopt(NOW))
        )
        self.assertIn("coins", card_text(sent_card(interaction)))

    async def test_a_finished_game_pays_coins_and_a_record_pays_more(self):
        before = await self.wallet(DEV_GUILD, MEMBER)
        interaction = fake_interaction(user_id=MEMBER)
        line = await self.cog.record_game(interaction, "guess", 4)
        self.assertEqual(
            await self.wallet(DEV_GUILD, MEMBER), before + devtoy.GAME_PRIZE + devtoy.RECORD_BONUS
        )
        self.assertIn("coins", line)
        # A game that beats nothing still pays the flat prize, and nothing on top.
        paid = await self.wallet(DEV_GUILD, MEMBER)
        line = await self.cog.record_game(interaction, "guess", 9)
        self.assertIn("Record stands", line)
        self.assertEqual(await self.wallet(DEV_GUILD, MEMBER), paid + devtoy.GAME_PRIZE)
        self.assertIn("coins", line)

    async def test_the_shop_prices_the_members_own_animal(self):
        dragon = devtoy.adopt(NOW, "Rex", "dragon")
        await self.cog._save(DEV_GUILD, MEMBER, dragon)
        interaction = fake_interaction(user_id=MEMBER)
        await DevToy.cat_shop.callback(self.cog, interaction)
        text = card_text(sent_card(interaction))
        meal = devtoy.FOODS[1]
        self.assertIn(str(devtoy.feed_price(dragon, meal)), text)
        self.assertIn(str(devtoy.care_coins(dragon)), text)
        self.assertIn("Dragon", text)

    async def test_a_member_with_no_pet_gets_a_cats_prices(self):
        interaction = fake_interaction(user_id=MEMBER)
        await DevToy.cat_shop.callback(self.cog, interaction)
        text = card_text(sent_card(interaction))
        self.assertIn(str(devtoy.HOUSE_FOOD.price), text)


class DyingAndComingBackThroughTheCog(EconomyCase):
    async def test_a_dead_pets_care_commands_are_refused(self):
        dead = replace(devtoy.adopt(NOW, "Biscuit"), fed=0.0, updated_at=NOW - 5 * devtoy.DAY)
        await self.cog._save(DEV_GUILD, MEMBER, dead)
        interaction = fake_interaction(user_id=MEMBER)
        await DevToy.cat_pet.callback(self.cog, interaction)
        self.assertIn("Gone", card_text(sent_card(interaction)))
        self.assertIn("/cat revive", card_text(sent_card(interaction)))

    async def test_a_dead_pet_cannot_be_replaced_by_a_new_one(self):
        dead = replace(devtoy.adopt(NOW, "Biscuit"), fed=0.0, updated_at=NOW - 5 * devtoy.DAY)
        await self.cog._save(DEV_GUILD, MEMBER, dead)
        await DevToy.cat_adopt.callback(self.cog, fake_interaction(user_id=MEMBER), "Rex", "dog")
        stored = await self.cog._load(DEV_GUILD, MEMBER)
        self.assertEqual(stored.name, "Biscuit", "a lost pet is not a chance to pick again")
        self.assertTrue(devtoy.is_dead(stored, time.time()))

    async def test_the_status_card_for_a_dead_pet_is_the_memorial(self):
        dead = replace(devtoy.adopt(NOW, "Biscuit"), fed=0.0, updated_at=NOW - 5 * devtoy.DAY)
        await self.cog._save(DEV_GUILD, MEMBER, dead)
        interaction = fake_interaction(user_id=MEMBER)
        await DevToy.cat_status.callback(self.cog, interaction, None)
        text = card_text(sent_card(interaction))
        self.assertIn("is gone", text)
        self.assertIn("/cat revive", text)

    async def test_an_administrator_brings_it_back_and_the_book_keeps_the_life(self):
        dead = replace(devtoy.adopt(NOW, "Biscuit"), fed=0.0, updated_at=NOW - 5 * devtoy.DAY)
        await self.cog._save(DEV_GUILD, MEMBER, dead)
        owner = SimpleNamespace(id=MEMBER, display_name="Dev")
        admin = fake_interaction(user_id=99)
        await DevToy.cat_revive.callback(self.cog, admin, owner)
        stored = await self.cog._load(DEV_GUILD, MEMBER)
        self.assertFalse(devtoy.is_dead(stored, time.time()))
        self.assertEqual(stored.deaths, 1)
        self.assertEqual(stored.fed, devtoy.REVIVE_FED)
        self.assertFalse(sent_card(admin).get("ephemeral"), "the member has to see this one")
        text = card_text(sent_card(admin))
        self.assertIn("Biscuit", text)
        self.assertIn("1", text)
        # And care works again, from the coin the revival left them.
        await DevToy.cat_pet.callback(self.cog, fake_interaction(user_id=MEMBER))
        self.assertGreater((await self.cog._load(DEV_GUILD, MEMBER)).care_score, 0)

    async def test_reviving_something_that_is_alive_is_not_a_no_op_silence(self):
        await self.cog._save(DEV_GUILD, MEMBER, devtoy.adopt(NOW, "Biscuit"))
        interaction = fake_interaction(user_id=MEMBER)
        await DevToy.cat_revive.callback(self.cog, interaction, None)
        text = card_text(sent_card(interaction))
        self.assertIn("Nothing to revive", text)
        self.assertEqual((await self.cog._load(DEV_GUILD, MEMBER)).deaths, 0)

    async def test_another_servers_pet_is_not_there_to_revive(self):
        owner = SimpleNamespace(id=MEMBER, display_name="Dev")
        interaction = fake_interaction(user_id=MEMBER, guild_id=OTHER_GUILD)
        await DevToy.cat_revive.callback(self.cog, interaction, owner)
        self.assertIn("own servers only", refusal_text(interaction))

    async def test_the_top_list_names_the_animal_and_the_pets_that_are_gone(self):
        await self.cog._save(DEV_GUILD, 1, devtoy.adopt(NOW, "Rex", "dog"))
        await self.cog._save(
            DEV_GUILD, 2, replace(devtoy.adopt(NOW, "Ash"), fed=0.0, updated_at=NOW - 5 * devtoy.DAY)
        )
        interaction = fake_interaction(user_id=MEMBER)
        # The names come from Discord's member cache, which a stand-in guild has
        # none of: this is the one place the card reaches outside the bot.
        with mock.patch.object(
            devtoy_cog, "display_name", mock.AsyncMock(return_value="Somebody")
        ):
            await DevToy.cat_top.callback(self.cog, interaction)
        text = card_text(sent_card(interaction))
        self.assertIn("Dog", text)
        self.assertIn("gone", text)


class TheTickAsksAndReports(EconomyCase):
    def channel(self):
        return SimpleNamespace(
            id=bot_channels.DEV_CHAT_CHANNEL_ID, mention="#cat", send=mock.AsyncMock()
        )

    def guild(self, channel):
        guild = fake_guild(DEV_GUILD, channels=[channel])
        self.cog.bot.get_guild = lambda _id: guild
        return guild

    def hungry_pet(self, *, species: str = "cat", user_id: int = MEMBER) -> devtoy.Cat:
        now = time.time()
        return replace(devtoy.adopt(now, "Biscuit", species), fed=5.0, updated_at=now)

    @staticmethod
    def asks(channel) -> list:
        """The asks among everything the cat posted, told apart by their footer.

        By footer rather than by the word *hungry*, which is on the quiet card too —
        a card about a pet whose bowl is low says so in its panel line, and that is
        the right thing for it to do. The footer is the card's own name for itself.
        """
        return [
            call for call in channel.send.await_args_list
            if "the cat, hungry" in card_text(dict(call.kwargs))
        ]

    async def test_a_hungry_pet_asks_once_and_the_gap_holds_after(self):
        channel = self.channel()
        self.guild(channel)
        await self.cog._save(DEV_GUILD, MEMBER, self.hungry_pet())
        await self.cog._ambient_once()
        asks = self.asks(channel)
        self.assertEqual(len(asks), 1)
        text = card_text(dict(asks[0].kwargs))
        self.assertIn("/cat feed", text)
        self.assertIn("coins", text)
        stored = await self.cog._load(DEV_GUILD, MEMBER)
        self.assertGreater(stored.last_ask, 0, "the ask is marked on the pet's own row")
        self.assertEqual(stored.ask_count, 1)
        # The next tick is two minutes later, and the gap is two hours.
        await self.cog._ambient_once()
        self.assertEqual(len(self.asks(channel)), 1)

    async def test_a_hungry_pet_does_not_spend_the_rooms_quiet_card(self):
        channel = self.channel()
        self.guild(channel)
        await self.cog._save(DEV_GUILD, MEMBER, self.hungry_pet())
        await self.cog._ambient_once()
        # One hungry card and one quiet card, and the *server's* allowance moved by
        # one: the two budgets are separate, which is the whole point of the split.
        self.assertEqual(len(self.asks(channel)), 1)
        self.assertEqual(channel.send.await_count, 2)
        self.assertEqual((await db.devcat_ambient(DEV_GUILD))["nudge_count"], 1)

    async def test_the_hunger_switch_silences_the_ask_and_nothing_else(self):
        await db.set_setting(DEV_GUILD, features.setting_key("devcat", "hunger"), False)
        channel = self.channel()
        self.guild(channel)
        await self.cog._save(DEV_GUILD, MEMBER, self.hungry_pet())
        await self.cog._ambient_once()
        self.assertEqual(self.asks(channel), [], "the ask is what the switch governs")
        self.assertEqual((await self.cog._load(DEV_GUILD, MEMBER)).last_ask, 0.0)
        self.assertEqual(channel.send.await_count, 1, "the quiet card is untouched")

    async def test_only_two_pets_are_asked_for_in_one_tick(self):
        channel = self.channel()
        self.guild(channel)
        for user_id in range(10, 10 + devtoy.HUNGER_ASKS_PER_TICK + 2):
            await self.cog._save(DEV_GUILD, user_id, self.hungry_pet(user_id=user_id))
        await self.cog._ambient_once()
        self.assertEqual(len(self.asks(channel)), devtoy.HUNGER_ASKS_PER_TICK)
        # And the ones not asked for this tick are the hungry ones, not the
        # switched-off ones: they come back round on the next pass.
        later = [
            row for row in await db.devcat_pets_for_tick(DEV_GUILD)
            if float(row["last_ask"]) == 0
        ]
        self.assertEqual(len(later), 2)

    async def test_the_hungriest_pet_is_asked_for_first(self):
        channel = self.channel()
        self.guild(channel)
        now = time.time()
        await self.cog._save(DEV_GUILD, 10, replace(devtoy.adopt(now, "Nearly"), fed=28.0, updated_at=now))
        await self.cog._save(DEV_GUILD, 11, replace(devtoy.adopt(now, "Starving"), fed=1.0, updated_at=now))
        await self.cog._ambient_once()
        rows = await db.devcat_pets_for_tick(DEV_GUILD)
        self.assertEqual(rows[0]["name"], "Starving", "the rows arrive sorted by the bowl")

    async def test_the_obituary_goes_out_once_and_is_remembered(self):
        channel = self.channel()
        self.guild(channel)
        dead = replace(
            devtoy.adopt(time.time() - 6 * devtoy.DAY, "Biscuit"), fed=0.0,
            updated_at=time.time() - 5 * devtoy.DAY,
        )
        await self.cog._save(DEV_GUILD, MEMBER, dead)
        await self.cog._ambient_once()
        obituaries = [
            call for call in channel.send.await_args_list
            if "is gone" in card_text(dict(call.kwargs))
        ]
        self.assertEqual(len(obituaries), 1)
        self.assertIn("/cat revive", card_text(dict(obituaries[0].kwargs)))
        self.assertNotIn("the cat, hungry", card_text(dict(obituaries[0].kwargs)))
        self.assertGreater((await self.cog._load(DEV_GUILD, MEMBER)).death_notice, 0)
        before = len(channel.send.await_args_list)
        await self.cog._ambient_once()
        self.assertEqual(len(channel.send.await_args_list), before, "the obituary repeats")

    async def test_the_obituary_arrives_whatever_the_chatter_switches_say(self):
        await db.set_setting(DEV_GUILD, features.setting_key("devcat", "nudges"), False)
        await db.set_setting(DEV_GUILD, features.setting_key("devcat", "hunger"), False)
        channel = self.channel()
        self.guild(channel)
        dead = replace(
            devtoy.adopt(time.time() - 6 * devtoy.DAY, "Biscuit"), fed=0.0,
            updated_at=time.time() - 5 * devtoy.DAY,
        )
        await self.cog._save(DEV_GUILD, MEMBER, dead)
        await self.cog._ambient_once()
        self.assertEqual(channel.send.await_count, 1)
        self.assertIn("is gone", card_text(dict(channel.send.await_args.kwargs)))

    async def test_a_pet_that_is_starving_is_not_also_asked_for(self):
        channel = self.channel()
        self.guild(channel)
        dead = replace(
            devtoy.adopt(time.time() - 6 * devtoy.DAY, "Biscuit"), fed=0.0,
            updated_at=time.time() - 5 * devtoy.DAY,
        )
        await self.cog._save(DEV_GUILD, MEMBER, dead)
        await self.cog._ambient_once()
        self.assertEqual((await self.cog._load(DEV_GUILD, MEMBER)).last_ask, 0.0)

    async def test_a_card_that_could_not_be_posted_does_not_spend_the_ask(self):
        """Same rule as the quiet card: an ask nobody received has not been made."""
        channel = SimpleNamespace(
            id=bot_channels.DEV_CHAT_CHANNEL_ID, mention="#cat",
            send=mock.AsyncMock(side_effect=discord.Forbidden(mock.MagicMock(), "no")),
        )
        self.guild(channel)
        await self.cog._save(DEV_GUILD, MEMBER, self.hungry_pet())
        await self.cog._ambient_once()
        self.assertEqual((await self.cog._load(DEV_GUILD, MEMBER)).last_ask, 0.0)


class ThePermissionsAndTheRegistryAgree(unittest.TestCase):
    def test_the_two_new_commands_have_one_audience_each(self):
        self.assertEqual(permissions.COMMAND_ACCESS["cat shop"], permissions.EVERYONE)
        self.assertEqual(permissions.COMMAND_ACCESS["cat revive"], permissions.ADMIN)

    def test_the_registry_declares_them_and_the_hunger_switch(self):
        feature = features.get("devcat")
        entries = {name for name, _ in features.feature_entries(feature)}
        self.assertIn("/cat shop", entries)
        self.assertIn("/cat revive", entries)
        self.assertIn("devcat_hunger", [features.setting_key("devcat", spec.id) for spec in feature.settings])


if __name__ == "__main__":
    unittest.main()
