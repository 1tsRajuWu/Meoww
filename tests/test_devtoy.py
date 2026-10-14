"""The Dev Cat: the operators' own servers' pet, and every way it could misbehave.

Four properties, each one a way a feature like this goes wrong rather than a way it
works:

* **The gate.** It is the bot's own two servers', by numbers written into the code —
  so the numbers have exactly one home (the same scan law the Dev role id already
  has), and the refusal happens in a stranger's server rather than in a comment.
* **The clock.** Mood, food and energy are derived from elapsed time, so the rules
  are testable without waiting four hours: a month of neglect is one subtraction,
  and the arithmetic is pinned against brute force.
* **The caps.** A pet that speaks whenever it likes is a spammer, so the ambient
  limits and the chat limits are driven directly here, including the day rollover
  and the message nobody may be pinged by.
* **The arcade.** Records only improve (the direction is per game), a scramble never
  hands back the answer, and the duel's verdicts are the real ones.
* **The outside world.** Three commands and two listeners now fetch something — a
  cat, a fact, a sentence — and the interesting half of each is what happens when
  nothing comes back: every one of them has a written answer, and a server with
  the brain switched off makes no request at all. The providers are stubbed here;
  ``tests/test_catfeed.py`` owns their shapes.

The last classes hold the two facts that keep this out of the wrong places: the cog
is never loaded by the main bot (a registry flag, read by the loader), and the
command tree declares an audience for every path it adds.
"""
import asyncio
import datetime
import os
import random
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord
from discord.ext import commands

from cogs.devtoy import MODULE, BoredHubView, CatActionsView, CatSettingsView, DevToy, cat_from_row
from tests.support import TempDatabaseMixin
from utils import bot_channels, catfeed, db, devtoy, features, permissions

ROOT = Path(__file__).resolve().parent.parent
DEV_GUILD = bot_channels.DEV_SERVER_GUILD_ID
#: The *second* own server — the home guild, which the operator sits in and which
#: this gate used to refuse. Its presence in this file is the regression the real
#: report was: `/cat` missing from the picker of the bot's own home server.
HOME_GUILD = bot_channels.HOME_GUILD_ID
OTHER_GUILD = 777001
#: 03:00 IST on a fixed date, so a test that walks twelve hours of "cards" stays
#: inside one day. A raw epoch here made the daily-cap tests pass or fail depending
#: on where the instant happened to land in the operator's day, which is not a
#: property under test.
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
NOW = datetime.datetime(2026, 10, 14, 3, 0, tzinfo=IST).timestamp()


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def fake_bot(*, vip: bool = True, module_on: bool = True, guilds=(), message_content: bool = True):
    """A bot stand-in with only what the cog reads."""
    views = []
    bot = SimpleNamespace(
        user=SimpleNamespace(id=permissions.VIP_BOT_ID if vip else 1548380269776605324),
        intents=SimpleNamespace(message_content=message_content),
        guilds=list(guilds),
        add_view=views.append,
        registered_views=views,
        is_module_enabled=lambda guild_id, module: module_on,
        get_guild=lambda guild_id: next(
            (g for g in guilds if getattr(g, "id", None) == guild_id), None
        ),
    )
    return bot


def fake_guild(guild_id: int = DEV_GUILD, channels=()):
    channel_map = {getattr(channel, "id", 0): channel for channel in channels}
    return SimpleNamespace(id=guild_id, name="Development", get_channel=channel_map.get)


def fake_interaction(guild_id: int = DEV_GUILD, user_id: int = 5, *, guild=None):
    interaction = mock.MagicMock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    interaction.guild = guild if guild is not None else fake_guild(guild_id)
    interaction.user = SimpleNamespace(id=user_id, display_name="Dev", bot=False)
    interaction.channel = SimpleNamespace(id=4242, mention="#dev", send=mock.AsyncMock())
    interaction.response = mock.MagicMock()
    interaction.response.is_done = mock.MagicMock(return_value=False)
    interaction.response.defer = mock.AsyncMock()
    interaction.response.send_message = mock.AsyncMock()
    interaction.response.edit_message = mock.AsyncMock()
    interaction.response.send_modal = mock.AsyncMock()
    interaction.followup = mock.MagicMock()
    interaction.followup.send = mock.AsyncMock()
    interaction.client = SimpleNamespace(user=SimpleNamespace(id=permissions.VIP_BOT_ID))
    return interaction


def refusal_text(interaction) -> str:
    """Whatever the cog said back, from either the response or the followup."""
    calls = list(interaction.response.send_message.await_args_list) + list(
        interaction.followup.send.await_args_list
    )
    return " ".join(str(call.args[0]) for call in calls if call.args)


def sent_card(interaction) -> dict:
    """The last card a fake interaction was sent, from either door.

    ``response`` and ``followup`` are both read because which one a card leaves by
    is the *command's* decision: a card sent before any await goes out as the
    response, and one sent after a ``defer()`` goes out as a followup.
    """
    for source in (interaction.followup.send, interaction.response.send_message):
        if source.await_args_list:
            return dict(source.await_args_list[-1].kwargs)
    return {}


def card_text(payload: dict) -> str:
    """Every word and every picture in a card, wherever the layout put it.

    ``send_card`` upgrades an embed to a Components V2 panel when the installed
    library has one (it does), so a test reading ``kwargs["embed"]`` would find
    nothing on this version and everything on an older one. Walking the tree is
    the only reading that holds on both.
    """
    seen: list[str] = []

    def walk(node) -> None:
        content = getattr(node, "content", None)
        if isinstance(content, str):
            seen.append(content)
        for item in getattr(node, "items", None) or ():
            url = getattr(getattr(item, "media", None), "url", None)
            if url:
                seen.append(str(url))
        for child in getattr(node, "children", None) or ():
            walk(child)

    view = (payload or {}).get("view")
    if view is not None:
        walk(view)
    embed = (payload or {}).get("embed")
    if embed is not None:
        seen.append(str(embed.title or ""))
        seen.append(str(embed.description or ""))
        if getattr(embed, "image", None) and embed.image.url:
            seen.append(str(embed.image.url))
        if getattr(embed, "footer", None) and embed.footer.text:
            seen.append(str(embed.footer.text))
    return "\n".join(seen)


class CogCase(unittest.IsolatedAsyncioTestCase, TempDatabaseMixin):
    """A real cog over a throwaway database."""

    async def asyncSetUp(self):
        self.use_temp_database("devtoy.db")
        await db.setup()
        self.bot = fake_bot()
        self.cog = DevToy(self.bot)
        # No test in this suite may reach the network, and the brain is a network
        # call by design. It is stubbed to "nothing came back" — which is also the
        # state a host with no key and no outbound route is in — and the tests
        # that care what the cat thought patch it for themselves.
        self.brain = mock.patch.object(catfeed, "say", mock.AsyncMock(return_value=None))
        self.brain.start()
        self.addCleanup(self.brain.stop)

    async def asyncTearDown(self):
        self.cog.cog_unload()
        self.release_temp_database()


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
class TheGateIsTheBotsOwnServers(unittest.TestCase):
    """Both of them, and neither one fewer: a gate that names one refuses in the
    other, which is the same defect ``bot_channels.is_own_server`` was written to
    fix — this is its id-only twin."""

    def test_both_own_servers_are_allowed(self):
        self.assertTrue(devtoy.in_dev_server(DEV_GUILD))
        self.assertTrue(devtoy.in_dev_server(str(DEV_GUILD)))
        self.assertTrue(devtoy.in_dev_server(HOME_GUILD))
        self.assertTrue(devtoy.in_dev_server(str(HOME_GUILD)))

    def test_everywhere_else_is_refused(self):
        self.assertFalse(devtoy.in_dev_server(OTHER_GUILD))
        self.assertFalse(devtoy.in_dev_server(None))
        self.assertFalse(devtoy.in_dev_server("not-a-snowflake"))

    def test_the_gate_agrees_with_the_own_server_rule_it_delegates_to(self):
        """One rule about which servers are the bot's own, asked of one function —
        a second opinion here would be the drift this suite keeps finding."""
        for guild_id in (DEV_GUILD, HOME_GUILD, OTHER_GUILD):
            with self.subTest(guild_id=guild_id):
                self.assertEqual(
                    devtoy.in_dev_server(guild_id),
                    bot_channels.is_own_guild_id(guild_id),
                )

    def test_the_room_the_cat_talks_in_is_one_room(self):
        self.assertTrue(devtoy.is_dev_channel(bot_channels.DEV_CHAT_CHANNEL_ID))
        self.assertFalse(devtoy.is_dev_channel(OTHER_GUILD))
        self.assertFalse(devtoy.is_dev_channel(None))

    def test_the_bot_server_default_reads_the_one_constant(self):
        """The digits keep one home, so the default *is* the constant."""
        self.assertEqual(bot_channels.DEV_SERVER_GUILD_ID, DEV_GUILD)
        saved = os.environ.pop("BOT_SERVER_GUILD_ID", None)
        try:
            self.assertEqual(bot_channels.bot_server_guild_id(), DEV_GUILD)
        finally:
            if saved is not None:
                os.environ["BOT_SERVER_GUILD_ID"] = saved

    def test_the_two_ids_have_one_home(self):
        """Every hand-written Python file except the owner's, scanned.

        The shape ``tests/test_bot_channels.py`` uses for the Dev role id: a number
        copied into a second file is a decision copied into a second file, and the
        two copies are one rename away from disagreeing about which server and which
        room this feature is.
        """
        home = (ROOT / "utils" / "bot_channels.py").resolve()
        skip = {".venv", "deleted_features", "node_modules", "__pycache__", ".git"}
        strays = []
        for path in ROOT.rglob("*.py"):
            if set(path.parts) & skip or path.name.startswith("test_"):
                continue
            if path.resolve() == home:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in (str(DEV_GUILD), str(bot_channels.DEV_CHAT_CHANNEL_ID)):
                if needle in text:
                    strays.append(f"{path.relative_to(ROOT)}:{needle}")
        self.assertEqual(strays, [], f"a dev-server id is copied into {strays}")


class TheCogRefusesEverywhereElse(CogCase):
    async def test_the_developer_server_is_allowed(self):
        interaction = fake_interaction()
        self.assertTrue(await self.cog.interaction_check(interaction))

    async def test_another_server_is_refused_with_a_sentence(self):
        interaction = fake_interaction(guild_id=OTHER_GUILD)
        self.assertFalse(await self.cog.interaction_check(interaction))
        self.assertIn("own servers only", refusal_text(interaction))

    async def test_a_dm_is_refused(self):
        interaction = fake_interaction()
        interaction.guild_id = None
        interaction.guild = None
        self.assertFalse(await self.cog.interaction_check(interaction))

    async def test_the_switch_off_here_is_the_other_refusal(self):
        cog = DevToy(fake_bot(module_on=False))
        try:
            interaction = fake_interaction()
            self.assertFalse(await cog.interaction_check(interaction))
            # The wording the module rule's own law looks for: every gated
            # command's refusal has to say the *module* is the reason.
            self.assertIn("turned off", refusal_text(interaction))
        finally:
            cog.cog_unload()

    async def test_a_button_in_another_server_is_refused_too(self):
        """Views do not run a cog's ``interaction_check``, so they check themselves."""
        interaction = fake_interaction(guild_id=OTHER_GUILD)
        await self.cog.care_button(interaction, "pet")
        self.assertIn("own servers only", refusal_text(interaction))
        self.assertIsNone(await db.devcat_pet(DEV_GUILD, 5))


# --------------------------------------------------------------------------- #
# The clock
# --------------------------------------------------------------------------- #
class TheCatIsAClockNotAJob(unittest.TestCase):
    def test_nothing_happens_before_any_time_has_passed(self):
        cat = devtoy.adopt(NOW, "Biscuit")
        self.assertEqual(devtoy.drift(cat, NOW), cat)

    def test_six_hours_away_is_one_subtraction(self):
        cat = devtoy.adopt(NOW, "Biscuit")
        later = devtoy.drift(cat, NOW + 6 * 3600)
        self.assertAlmostEqual(later.fed, cat.fed - devtoy.FED_PER_HOUR * 6, places=3)
        self.assertAlmostEqual(later.mood, cat.mood - devtoy.MOOD_PER_HOUR * 6, places=3)
        self.assertEqual(later.energy, 100.0, "a cat alone is a cat asleep")

    def test_a_full_day_drains_the_bowl_and_floors_the_mood(self):
        cat = devtoy.adopt(NOW, "Biscuit")
        later = devtoy.drift(cat, NOW + 24 * 3600)
        self.assertEqual(later.fed, 0.0, "clamped at empty, never negative")
        self.assertAlmostEqual(later.mood, cat.mood - devtoy.MOOD_PER_HOUR * 24, places=3)
        self.assertLess(later.mood, cat.mood)
        # The empty bowl is what the word says first: mood is bad *and* the cat is
        # starving, and "starving" is the sentence that gets somebody to feed it.
        self.assertEqual(devtoy.mood_word(later), "starving")

    def test_the_arithmetic_matches_a_brute_force_walk(self):
        """Six hours one minute at a time, against the closed form."""
        cat = devtoy.adopt(NOW, "Biscuit")
        walked = cat
        for minute in range(1, 6 * 60 + 1):
            walked = devtoy.drift(walked, NOW + minute * 60)
        closed = devtoy.drift(cat, NOW + 6 * 3600)
        self.assertAlmostEqual(walked.fed, closed.fed, places=6)
        self.assertAlmostEqual(walked.mood, closed.mood, places=6)
        self.assertAlmostEqual(walked.energy, closed.energy, places=6)

    def test_a_month_of_neglect_is_miserable_and_never_negative(self):
        cat = devtoy.adopt(NOW, "Biscuit")
        later = devtoy.drift(cat, NOW + 40 * 24 * 3600)
        self.assertEqual(later.fed, 0.0)
        self.assertEqual(later.mood, 0.0)
        self.assertEqual(devtoy.mood_word(later), "starving")
        self.assertEqual(devtoy.face(later), "🙀")

    def test_a_clock_that_moved_backwards_is_not_free_food(self):
        cat = devtoy.adopt(NOW, "Biscuit")
        self.assertEqual(devtoy.drift(cat, NOW - 3600), cat)

    def test_a_row_that_was_never_touched_starts_the_clock_now(self):
        fresh = devtoy.drift(devtoy.Cat(), NOW)
        self.assertEqual(fresh.updated_at, NOW)
        self.assertEqual(fresh.fed, devtoy.Cat().fed)

    def test_the_bars_are_bounded(self):
        self.assertEqual(devtoy.bar(0), "░" * 10)
        self.assertEqual(devtoy.bar(100), "█" * 10)
        self.assertEqual(devtoy.bar(1000), "█" * 10)
        self.assertEqual(devtoy.bar(-5), "░" * 10)


class CareIsRateLimited(unittest.TestCase):
    def test_petting_a_cat_makes_it_happier_and_stamps_the_clock(self):
        cat = devtoy.adopt(NOW, "Biscuit")
        result = devtoy.care(cat, "pet", NOW + 60)
        self.assertTrue(result.ok)
        self.assertGreater(result.cat.mood, cat.mood)
        self.assertEqual(result.cat.last_pet, NOW + 60)
        self.assertEqual(result.cat.care_score, 1)

    def test_the_same_action_twice_is_refused_with_the_time_left(self):
        cat = devtoy.adopt(NOW, "Biscuit")
        first = devtoy.care(cat, "pet", NOW + 60)
        second = devtoy.care(first.cat, "pet", NOW + 61)
        self.assertFalse(second.ok)
        self.assertIn("Give it", second.detail)
        self.assertGreater(second.remaining, 0)
        self.assertEqual(second.cat.care_score, 1, "a refusal must not score")

    def test_a_refusal_returns_the_drifted_cat(self):
        cat = devtoy.adopt(NOW, "Biscuit")
        first = devtoy.care(cat, "pet", NOW + 60)
        later = NOW + 60 + 600  # ten minutes: still inside the fifteen-minute clock
        second = devtoy.care(first.cat, "pet", later)
        self.assertFalse(second.ok)
        self.assertLess(second.cat.fed, first.cat.fed)
        self.assertEqual(second.cat.updated_at, later)

    def test_a_sleepy_cat_cannot_be_asked_to_play(self):
        tired = devtoy.Cat(fed=80, mood=80, energy=5, updated_at=NOW)
        result = devtoy.care(tired, "play", NOW + 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.headline, "Too sleepy")

    def test_a_hungry_cat_will_not_chase_anything(self):
        hungry = devtoy.Cat(fed=4, mood=80, energy=90, updated_at=NOW)
        result = devtoy.care(hungry, "play", NOW + 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.headline, "Too hungry")

    def test_playing_costs_energy_and_feeding_gives_food_back(self):
        cat = devtoy.adopt(NOW, "Biscuit")
        played = devtoy.care(cat, "play", NOW + 1)
        self.assertTrue(played.ok)
        self.assertLess(played.cat.energy, cat.energy)
        fed = devtoy.care(played.cat, "feed", NOW + 2)
        self.assertTrue(fed.ok)
        self.assertGreater(fed.cat.fed, played.cat.fed)

    def test_an_unknown_action_is_a_programming_error_not_a_silent_no(self):
        with self.assertRaises(ValueError):
            devtoy.care(devtoy.adopt(NOW), "juggle", NOW)
        with self.assertRaises(ValueError):
            devtoy.cooldown_left(devtoy.adopt(NOW), "juggle", NOW)


class StreaksCountDays(unittest.TestCase):
    def test_the_same_day_does_not_double_count(self):
        self.assertEqual(devtoy.streak_after("2026-10-14", 3, "2026-10-14"), (3, "2026-10-14"))

    def test_yesterday_extends_the_run(self):
        self.assertEqual(devtoy.streak_after("2026-10-13", 3, "2026-10-14"), (4, "2026-10-14"))

    def test_a_gap_starts_again(self):
        self.assertEqual(devtoy.streak_after("2026-10-01", 9, "2026-10-14"), (1, "2026-10-14"))

    def test_a_first_visit_is_a_run_of_one(self):
        self.assertEqual(devtoy.streak_after("", 0, "2026-10-14"), (1, "2026-10-14"))

    def test_an_unreadable_day_starts_again_rather_than_carrying_it_forward(self):
        self.assertEqual(devtoy.streak_after("not-a-date", 12, "2026-10-14"), (1, "2026-10-14"))

    def test_the_day_boundary_is_ist(self):
        """23:00 IST and 01:00 IST are different days; UTC would call them the same."""
        # 2026-10-14T17:31Z is 23:01 IST on the 14th; two hours later it is the 15th.
        first = devtoy.care_day(1792085460.0)
        second = devtoy.care_day(1792085460.0 + 2 * 3600)
        self.assertNotEqual(first, second)


class LevelsAndBadges(unittest.TestCase):
    def test_levels_climb_with_care_and_stop_at_the_ceiling(self):
        self.assertEqual(devtoy.care_level(devtoy.Cat(care_score=0)), 1)
        self.assertEqual(devtoy.care_level(devtoy.Cat(care_score=30)), 2)
        self.assertEqual(devtoy.care_level(devtoy.Cat(care_score=10_000)), devtoy.MAX_LEVEL)

    def test_badges_are_earned_and_absent_when_they_are_not(self):
        plain = devtoy.Cat()
        self.assertEqual(devtoy.badges(plain, NOW), [])
        earned = devtoy.Cat(care_days=30, care_score=400, games=11, adopted_at=NOW - 40 * 86400)
        badges = devtoy.badges(earned, NOW)
        self.assertTrue(any("30 days" in badge for badge in badges))
        self.assertTrue(any("Devoted" in badge for badge in badges))
        self.assertTrue(any("Arcade" in badge for badge in badges))
        self.assertTrue(any("A month" in badge for badge in badges))


# --------------------------------------------------------------------------- #
# The caps
# --------------------------------------------------------------------------- #
class TheCatBothersNobody(unittest.TestCase):
    def test_off_means_off(self):
        decision = devtoy.nudge_due(devtoy.AmbientState(), NOW, enabled=False)
        self.assertFalse(decision.ok)
        self.assertIn("switched off", decision.reason)

    def test_a_fresh_install_may_speak(self):
        self.assertTrue(devtoy.nudge_due(devtoy.AmbientState(), NOW, enabled=True).ok)

    def test_the_gap_holds(self):
        state = devtoy.mark_nudge(devtoy.AmbientState(), NOW)
        early = devtoy.nudge_due(state, NOW + 60, enabled=True)
        self.assertFalse(early.ok)
        self.assertIn("quiet for another", early.reason)
        self.assertTrue(devtoy.nudge_due(state, NOW + devtoy.NUDGE_MIN_GAP, enabled=True).ok)

    def test_three_a_day_and_no_more(self):
        state = devtoy.AmbientState()
        now = NOW
        for _ in range(devtoy.NUDGE_DAILY_CAP):
            decision = devtoy.nudge_due(state, now, enabled=True)
            self.assertTrue(decision.ok, f"card {_} should have been allowed")
            state = devtoy.mark_nudge(state, now)
            now += devtoy.NUDGE_MIN_GAP
        blocked = devtoy.nudge_due(state, now, enabled=True)
        self.assertFalse(blocked.ok)
        self.assertIn("today", blocked.reason)

    def test_a_new_day_resets_the_allowance(self):
        state = devtoy.AmbientState()
        now = NOW
        for _ in range(devtoy.NUDGE_DAILY_CAP):
            state = devtoy.mark_nudge(state, now)
            now += devtoy.NUDGE_MIN_GAP
        self.assertEqual(devtoy.nudges_today(state, now), devtoy.NUDGE_DAILY_CAP)
        tomorrow = now + 24 * 3600
        self.assertEqual(devtoy.nudges_today(state, tomorrow), 0)
        self.assertTrue(devtoy.nudge_due(state, tomorrow, enabled=True).ok)

    def test_every_line_the_cat_can_say_is_mention_free(self):
        """The default is a card that pings nobody, and this is what makes it true.

        ``NO_PINGS`` is the belt; this is the braces. A card nobody asked for that
        happens to contain ``@everyone`` would ping a server on a timer.
        """
        cat = devtoy.adopt(NOW, "Biscuit")
        rng = random.Random(7)
        for kind in devtoy.NUDGE_KINDS:
            for _ in range(20):
                title, body = devtoy.ambient_text(kind, cat, NOW, rng=rng)
                self.assertTrue(title.strip(), "a card with no title is a broken card")
                self.assertTrue(body.strip())
                combined = f"{title}\n{body}"
                for mention in ("@everyone", "@here", "<@&", "<@"):
                    self.assertNotIn(mention, combined, f"{kind} carries {mention}")

    def test_the_wording_picks_are_in_their_pools(self):
        rng = random.Random(3)
        kind = devtoy.nudge_due(devtoy.AmbientState(), NOW, enabled=True, rng=rng).kind
        self.assertIn(kind, devtoy.NUDGE_KINDS)


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
class TheCatOnlyAnswersWhenSpokenTo(unittest.TestCase):
    def test_an_empty_message_is_ignored(self):
        self.assertEqual(devtoy.chat_decision("", now=NOW), devtoy.ChatDecision())
        self.assertEqual(devtoy.chat_decision("   ", now=NOW), devtoy.ChatDecision())

    def test_a_mention_gets_a_sentence(self):
        decision = devtoy.chat_decision("hello there", mentioned=True, now=NOW)
        self.assertTrue(decision.reply)
        self.assertIsNone(decision.react, "a reply already said something")
        self.assertEqual(decision.matched, "mention")

    def test_the_cats_own_name_gets_a_sentence(self):
        decision = devtoy.chat_decision("biscuit is asleep again", cat_name="Biscuit", now=NOW)
        self.assertEqual(decision.matched, "mention")

    def test_a_name_inside_another_word_is_not_the_name(self):
        decision = devtoy.chat_decision("the biscuity smell of rain", cat_name="Biscuit", now=NOW)
        self.assertNotEqual(decision.matched, "mention")

    def test_a_keyword_answers_and_a_whole_word_is_required(self):
        self.assertEqual(devtoy.chat_decision("i am bored", now=NOW).matched, "bored")
        # ``hi`` inside ``shipped`` is the bug this rule exists for.
        self.assertNotEqual(devtoy.chat_decision("i shipped a release", now=NOW).matched, "hi")

    def test_an_ordinary_message_gets_at_most_a_reaction(self):
        decision = devtoy.chat_decision("the build went green on the first try", now=NOW)
        self.assertTrue(decision.react)
        self.assertIsNone(decision.reply)
        self.assertEqual(decision.matched, "reaction")

    def test_the_reaction_clock_holds(self):
        quiet = devtoy.chat_decision(
            "the build went green on the first try", now=NOW, last_react=NOW - 5
        )
        self.assertEqual(quiet, devtoy.ChatDecision())

    def test_the_reply_clock_holds_for_a_keyword(self):
        quiet = devtoy.chat_decision("i am bored", now=NOW, last_reply=NOW - 5, last_react=0)
        self.assertIsNone(quiet.reply)

    def test_a_tiny_message_is_never_reacted_to(self):
        self.assertEqual(devtoy.chat_decision("ok", now=NOW), devtoy.ChatDecision())

    def test_every_line_the_cat_can_say_in_chat_is_mention_free(self):
        lines = list(devtoy.MENTION_LINES)
        for _needles, pool in devtoy.KEYWORD_REPLIES:
            lines.extend(pool)
        for line in lines:
            self.assertTrue(line.strip())
            for mention in ("@everyone", "@here"):
                self.assertNotIn(mention, line)
        self.assertTrue(devtoy.REACTION_EMOJI)


class NamesAreSafe(unittest.TestCase):
    def test_an_ordinary_name_survives(self):
        self.assertEqual(devtoy.clean_name("Biscuit"), "Biscuit")
        self.assertEqual(devtoy.clean_name("  Biscuit   the  Third "), "Biscuit the Third")

    def test_markdown_is_trimmed_rather_than_refused(self):
        self.assertEqual(devtoy.clean_name("**Biscuit**"), "Biscuit")
        self.assertFalse(devtoy.name_refused("**Biscuit**"))

    def test_a_name_that_could_ping_or_link_is_refused(self):
        for raw in ("@everyone", "here @ me", "https://x.y", "<@1>", "discord.gg/x"):
            self.assertEqual(devtoy.clean_name(raw), devtoy.DEFAULT_NAME, raw)
            self.assertTrue(devtoy.name_refused(raw), raw)

    def test_an_empty_name_is_the_default_and_not_a_refusal(self):
        self.assertEqual(devtoy.clean_name(""), devtoy.DEFAULT_NAME)
        self.assertFalse(devtoy.name_refused(""))

    def test_a_name_is_bounded(self):
        self.assertEqual(len(devtoy.clean_name("x" * 200)), devtoy.NAME_LIMIT)


# --------------------------------------------------------------------------- #
# The arcade
# --------------------------------------------------------------------------- #
class TheArcadeWorks(unittest.TestCase):
    def test_the_secret_is_inside_the_range_it_promises(self):
        rng = random.Random(5)
        for _ in range(50):
            self.assertTrue(devtoy.GUESS_MIN <= devtoy.guess_secret(rng) <= devtoy.GUESS_MAX)

    def test_the_verdicts_are_the_real_ones(self):
        self.assertEqual(devtoy.guess_verdict(50, 50), "hit")
        self.assertEqual(devtoy.guess_verdict(50, 10), "higher")
        self.assertEqual(devtoy.guess_verdict(50, 90), "lower")

    def test_par_is_the_information_theoretic_answer(self):
        self.assertEqual(devtoy.GUESS_PAR, 7)
        self.assertIn("par", devtoy.guess_comment(9))

    def test_a_scramble_is_a_permutation_and_never_the_answer(self):
        rng = random.Random(9)
        for word in devtoy.SCRAMBLE_WORDS:
            for _ in range(20):
                scrambled = devtoy.scramble(word, rng)
                self.assertNotEqual(scrambled.lower(), word.lower(), word)
                self.assertEqual(sorted(scrambled), sorted(word))
        self.assertEqual(devtoy.scramble("a", rng), "a")

    def test_a_round_uses_a_real_word(self):
        answer, scrambled = devtoy.scramble_round(random.Random(2))
        self.assertIn(answer, devtoy.SCRAMBLE_WORDS)
        self.assertEqual(sorted(answer), sorted(scrambled))

    def test_the_duel_verdicts_are_the_real_ones(self):
        self.assertEqual(devtoy.rps_verdict(0, 1), "cat")   # rock vs paper
        self.assertEqual(devtoy.rps_verdict(1, 0), "you")   # paper vs rock
        self.assertEqual(devtoy.rps_verdict(2, 2), "tie")
        self.assertEqual(devtoy.rps_verdict(0, 2), "you")   # rock vs scissors
        # Indexes wrap rather than crash: 3 is 0, so this is rock against paper.
        self.assertEqual(devtoy.rps_verdict(3, 4), "cat")
        self.assertEqual(devtoy.rps_verdict(6, 0), "tie")

    def test_the_arcade_needs_three_wins(self):
        self.assertEqual(devtoy.DUEL_WINS_NEEDED, 3)
        self.assertEqual(len(devtoy.RPS_MOVES), 3)

    def test_records_only_improve_in_the_direction_the_game_says(self):
        self.assertTrue(devtoy.record_better("guess", 5, None), "no record yet is a record")
        self.assertTrue(devtoy.record_better("guess", 2, 5), "fewer tries wins")
        self.assertFalse(devtoy.record_better("guess", 9, 5))
        self.assertTrue(devtoy.record_better("reaction", 120, 300), "quicker wins")
        self.assertFalse(devtoy.record_better("reaction", 400, 300))
        self.assertTrue(devtoy.record_better("rps", 4, 3), "more wins wins")
        self.assertFalse(devtoy.record_better("rps", 1, 3))

    def test_a_stored_zero_is_not_a_record(self):
        """How a row written by a mistake (or a game that scored nothing) reads."""
        self.assertTrue(devtoy.record_better("guess", 4, 0))

    def test_an_unknown_game_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            devtoy.record_better("chess", 1, None)
        with self.assertRaises(ValueError):
            devtoy.record_text("chess", 1)

    def test_records_read_correctly(self):
        self.assertEqual(devtoy.record_text("guess", None), "—")
        self.assertEqual(devtoy.record_text("guess", 4), "4 tries")
        self.assertEqual(devtoy.record_text("reaction", 233.7), "234 ms")
        self.assertEqual(devtoy.record_text("scramble", 12.34), "12.3 s")

    def test_a_surprise_is_something_that_exists(self):
        rng = random.Random(4)
        for _ in range(30):
            self.assertIn(devtoy.roll_activity(rng), devtoy.ACTIVITY_IDS)

    def test_the_reflex_bands_cover_every_number(self):
        for ms in (0, 100, 180, 260, 400, 650, 5000):
            self.assertTrue(devtoy.reflex_comment(ms))

    def test_every_game_has_a_label_and_a_record_name(self):
        for game in devtoy.GAMES:
            self.assertTrue(game.label and game.blurb and game.record)


# --------------------------------------------------------------------------- #
# Stored rows
# --------------------------------------------------------------------------- #
class RowsRoundTrip(CogCase):
    async def test_a_cat_comes_back_exactly(self):
        cat = devtoy.care(devtoy.adopt(NOW, "Biscuit"), "feed", NOW + 60).cat
        await self.cog._save(DEV_GUILD, 5, cat)
        loaded = await self.cog._load(DEV_GUILD, 5)
        self.assertEqual(loaded, cat)

    async def test_adopting_twice_is_not_a_second_cat(self):
        await self.cog._save(DEV_GUILD, 5, devtoy.adopt(NOW, "Biscuit"))
        self.assertIsNotNone(await db.devcat_pet(DEV_GUILD, 5))
        self.assertIsNone(await db.devcat_pet(OTHER_GUILD, 5), "the row is per server")

    async def test_the_leaderboard_orders_by_care(self):
        await self.cog._save(DEV_GUILD, 1, devtoy.Cat(name="A", care_score=10))
        await self.cog._save(DEV_GUILD, 2, devtoy.Cat(name="B", care_score=90))
        rows = await db.devcat_leaderboard(DEV_GUILD)
        self.assertEqual([row["name"] for row in rows], ["B", "A"])
        self.assertEqual(cat_from_row(rows[0]).name, "B")

    async def test_a_record_keeps_its_play_count_when_a_score_does_not_beat_it(self):
        await db.save_devcat_record(DEV_GUILD, 5, "guess", 4, plays=3)
        stored = await db.devcat_record(DEV_GUILD, 5, "guess")
        self.assertEqual(stored["best"], 4)
        self.assertEqual(stored["plays"], 3)
        await db.save_devcat_record(DEV_GUILD, 5, "guess", 4, plays=4)
        self.assertEqual((await db.devcat_record(DEV_GUILD, 5, "guess"))["plays"], 4)

    async def test_a_record_written_without_a_play_count_does_not_reset_it(self):
        await db.save_devcat_record(DEV_GUILD, 5, "rps", 2, plays=7)
        await db.save_devcat_record(DEV_GUILD, 5, "rps", 3)
        stored = await db.devcat_record(DEV_GUILD, 5, "rps")
        self.assertEqual(stored["best"], 3)
        self.assertEqual(stored["plays"], 7)

    async def test_the_ambient_row_is_always_a_dict(self):
        fresh = await db.devcat_ambient(DEV_GUILD)
        self.assertEqual(fresh, {"last_nudge": 0.0, "nudge_day": "", "nudge_count": 0})
        await db.save_devcat_ambient(DEV_GUILD, last_nudge=NOW, nudge_day="2026-10-14", nudge_count=2)
        stored = await db.devcat_ambient(DEV_GUILD)
        self.assertEqual(stored["nudge_count"], 2)
        self.assertEqual(stored["nudge_day"], "2026-10-14")

    async def test_the_row_shape_survives_a_round_trip_of_the_model(self):
        cat = devtoy.Cat(
            name="Biscuit", fed=11.5, mood=22.25, energy=33.0, adopted_at=NOW,
            updated_at=NOW + 1, last_fed=NOW, last_pet=NOW, last_play=NOW,
            care_days=4, last_care_day="2026-10-14", care_score=17, games=3,
        )
        await self.cog._save(DEV_GUILD, 9, cat)
        self.assertEqual(await self.cog._load(DEV_GUILD, 9), cat)


# --------------------------------------------------------------------------- #
# The game itself, through the cog
# --------------------------------------------------------------------------- #
class TheArcadeThroughTheCog(CogCase):
    async def test_a_finished_game_stores_a_record_and_raises_the_game_count(self):
        await self.cog._save(DEV_GUILD, 5, devtoy.adopt(NOW, "Biscuit"))
        interaction = fake_interaction(user_id=5)
        line = await self.cog.record_game(interaction, "guess", 4)
        self.assertIn("First record", line)
        record = await db.devcat_record(DEV_GUILD, 5, "guess")
        self.assertEqual(record["best"], 4)
        self.assertEqual(record["plays"], 1)
        self.assertEqual((await self.cog._load(DEV_GUILD, 5)).games, 1)

    async def test_a_worse_score_says_the_record_stands(self):
        await self.cog._save(DEV_GUILD, 5, devtoy.adopt(NOW, "Biscuit"))
        interaction = fake_interaction(user_id=5)
        await self.cog.record_game(interaction, "guess", 4)
        line = await self.cog.record_game(interaction, "guess", 9)
        self.assertIn("Record stands", line)
        self.assertEqual((await db.devcat_record(DEV_GUILD, 5, "guess"))["best"], 4)

    async def test_a_better_score_names_the_old_one(self):
        await self.cog._save(DEV_GUILD, 5, devtoy.adopt(NOW, "Biscuit"))
        interaction = fake_interaction(user_id=5)
        await self.cog.record_game(interaction, "guess", 4)
        line = await self.cog.record_game(interaction, "guess", 2)
        self.assertIn("New record", line)
        self.assertIn("was 4 tries", line)

    async def test_the_settings_card_reads_the_real_switches(self):
        embed = await self.cog.settings_embed(DEV_GUILD)
        body = embed.description
        self.assertIn("Nudges", body)
        self.assertIn("0/3 cards today", body)

    async def test_a_setting_flip_is_what_the_reader_reads_back(self):
        self.assertTrue(await self.cog.setting_bool(DEV_GUILD, "nudges", default=True))
        await db.set_setting(DEV_GUILD, features.setting_key("devcat", "nudges"), False)
        self.assertFalse(await self.cog.setting_bool(DEV_GUILD, "nudges", default=True))

    async def test_an_unreadable_setting_falls_back_to_the_declared_default(self):
        await db.set_setting(DEV_GUILD, features.setting_key("devcat", "ping"), "not-a-bool")
        self.assertFalse(await self.cog.setting_bool(DEV_GUILD, "ping", default=False))

    async def test_a_care_button_refreshes_a_panel_and_answers_privately(self):
        await self.cog._save(DEV_GUILD, 5, devtoy.adopt(NOW, "Biscuit"))
        interaction = fake_interaction(user_id=5)
        interaction.message = SimpleNamespace(edit=mock.AsyncMock())
        await self.cog.care_button(interaction, "feed")
        interaction.message.edit.assert_awaited()
        self.assertTrue(interaction.response.send_message.await_args_list)
        self.assertTrue(
            interaction.response.send_message.await_args_list[0].kwargs.get("ephemeral")
        )

    async def test_the_panel_is_the_only_registered_view(self):
        """Everything else carries a timeout, and ``add_view`` refuses those."""
        self.assertEqual(self.bot.registered_views, [self.cog.panel_view])
        self.assertIsInstance(self.cog.panel_view, CatActionsView)
        self.assertIsInstance(self.cog.settings_view, CatSettingsView)
        self.assertIsInstance(self.cog.hub_view, BoredHubView)

    async def test_every_button_has_a_stable_id(self):
        ids = [
            item.custom_id
            for view in (self.cog.panel_view, self.cog.settings_view, self.cog.hub_view)
            for item in view.children
        ]
        self.assertTrue(ids)
        for custom_id in ids:
            self.assertTrue(str(custom_id).startswith("devcat:"), custom_id)
        self.assertEqual(len(ids), len(set(ids)), "two buttons sharing an id is a broken panel")


class TheCatFetchesFromOutside(CogCase):
    """``/cat photo``, ``/cat fact`` and ``/cat ask``, with the providers stubbed.

    Stubbed rather than called, because this suite runs on every push and a suite
    that fails when somebody else's free service is asleep is a suite people learn
    to skip. What is under test is the half this repository owns: that an answer
    is used, that no answer ends in an empty card, and that a question typed by a
    member cannot smuggle a ping or a link into the cat's mouth.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.cog._save(DEV_GUILD, 5, devtoy.adopt(NOW, "Biscuit"))

    async def test_a_photo_carries_the_picture_and_a_line_about_it(self):
        with mock.patch.object(
            catfeed, "photo", mock.AsyncMock(return_value="https://cdn.example/cat.jpg")
        ):
            interaction = fake_interaction(user_id=5)
            await DevToy.cat_photo.callback(self.cog, interaction)
        # Deferred first: a picture service takes seconds and Discord gives a
        # command three, so the alternative is "the application did not respond".
        interaction.response.defer.assert_awaited()
        text = card_text(sent_card(interaction))
        self.assertIn("https://cdn.example/cat.jpg", text)
        captions = {line.format(name="Biscuit") for line in devtoy.PHOTO_CAPTIONS}
        self.assertTrue(captions & set(text.splitlines()), text)

    async def test_no_picture_is_a_card_that_says_so(self):
        with mock.patch.object(catfeed, "photo", mock.AsyncMock(return_value=None)):
            interaction = fake_interaction(user_id=5)
            await DevToy.cat_photo.callback(self.cog, interaction)
        payload = sent_card(interaction)
        text = card_text(payload)
        self.assertIn("No cat answered", text)
        self.assertNotIn("http", text)
        self.assertTrue(payload.get("ephemeral"))

    async def test_a_fact_card_names_where_it_came_from(self):
        with mock.patch.object(
            catfeed, "fact", mock.AsyncMock(return_value="Cats purr at 25 to 150 Hz.")
        ):
            interaction = fake_interaction(user_id=5)
            await DevToy.cat_fact.callback(self.cog, interaction)
        payload = sent_card(interaction)
        self.assertIn("Cats purr at 25 to 150 Hz.", card_text(payload))
        self.assertIn("catfact.ninja", card_text(payload))
        self.assertTrue(payload.get("ephemeral"))

    async def test_with_every_fact_api_down_it_says_one_of_its_own_and_admits_it(self):
        with mock.patch.object(catfeed, "fact", mock.AsyncMock(return_value=None)):
            interaction = fake_interaction(user_id=5)
            await DevToy.cat_fact.callback(self.cog, interaction)
        text = card_text(sent_card(interaction))
        self.assertIn("the cat's own notes", text)
        self.assertTrue(any(fact in text for fact in devtoy.FACTS), text)

    async def test_ask_reports_the_answer_a_model_gave(self):
        with mock.patch.object(
            catfeed, "say", mock.AsyncMock(return_value="Tuna, obviously.")
        ):
            interaction = fake_interaction(user_id=5)
            await DevToy.cat_ask.callback(self.cog, interaction, "do you like tuna?")
        text = card_text(sent_card(interaction))
        self.assertIn("Tuna, obviously.", text)
        self.assertIn("do you like tuna?", text)

    async def test_ask_with_nothing_answering_is_the_cat_declining(self):
        interaction = fake_interaction(user_id=5)  # the harness stubs the brain to None
        await DevToy.cat_ask.callback(self.cog, interaction, "what is the meaning of life?")
        text = card_text(sent_card(interaction))
        self.assertIn("declines", text)
        self.assertTrue(any(line in text for line in devtoy.ASK_FALLBACKS), text)

    async def test_a_question_cannot_smuggle_a_mention_or_a_link_into_the_card(self):
        interaction = fake_interaction(user_id=5)
        await DevToy.cat_ask.callback(
            self.cog,
            interaction,
            f"<@{permissions.VIP_BOT_ID}> @everyone look at https://evil.example/x",
        )
        text = card_text(sent_card(interaction))
        self.assertNotIn("@everyone", text)
        self.assertNotIn("<@", text)
        self.assertNotIn("evil.example", text)

    async def test_the_brain_off_makes_no_request_at_all(self):
        await db.set_setting(DEV_GUILD, features.setting_key("devcat", "brain"), False)
        spy = mock.AsyncMock(return_value="never used")
        with mock.patch.object(catfeed, "say", spy):
            interaction = fake_interaction(user_id=5)
            await DevToy.cat_ask.callback(self.cog, interaction, "hello?")
        spy.assert_not_awaited()
        text = card_text(sent_card(interaction))
        self.assertTrue(any(line in text for line in devtoy.ASK_FALLBACKS), text)

    async def test_the_settings_card_says_which_brain_is_answering(self):
        body = (await self.cog.settings_embed(DEV_GUILD)).description
        self.assertIn("Brain", body)
        self.assertIn("public keyless model", body)
        with mock.patch.dict(os.environ, {"CAT_AI_KEY": "a-key"}):
            body = (await self.cog.settings_embed(DEV_GUILD)).description
        self.assertIn("this deployment's own key", body)


class TheListenerListensToAlmostNothing(CogCase):
    def message(self, *, guild_id=DEV_GUILD, content="the build went green", author_bot=False,
                webhook=False, mentions=()):
        message = mock.MagicMock()
        message.guild = fake_guild(guild_id)
        message.author = SimpleNamespace(id=5, bot=author_bot)
        message.content = content
        message.webhook_id = 1234 if webhook else None
        message.mentions = list(mentions)
        message.reply = mock.AsyncMock()
        message.add_reaction = mock.AsyncMock()
        return message

    async def test_the_intent_off_means_silence(self):
        self.cog.bot.intents.message_content = False
        message = self.message()
        await self.cog.on_message(message)
        message.reply.assert_not_awaited()
        message.add_reaction.assert_not_awaited()

    async def test_another_server_is_ignored(self):
        message = self.message(guild_id=OTHER_GUILD)
        await self.cog.on_message(message)
        message.reply.assert_not_awaited()
        message.add_reaction.assert_not_awaited()

    async def test_a_bot_author_is_ignored(self):
        message = self.message(author_bot=True)
        await self.cog.on_message(message)
        message.reply.assert_not_awaited()

    async def test_a_webhook_is_ignored(self):
        message = self.message(webhook=True)
        await self.cog.on_message(message)
        message.reply.assert_not_awaited()

    async def test_an_empty_message_is_ignored(self):
        message = self.message(content="   ")
        await self.cog.on_message(message)
        message.reply.assert_not_awaited()
        message.add_reaction.assert_not_awaited()

    async def test_the_switch_off_means_silence(self):
        self.cog.bot.is_module_enabled = lambda *_: False
        message = self.message()
        await self.cog.on_message(message)
        message.reply.assert_not_awaited()

    async def test_the_chat_switch_off_means_silence(self):
        await db.set_setting(DEV_GUILD, features.setting_key("devcat", "reactions"), False)
        message = self.message()
        await self.cog.on_message(message)
        message.reply.assert_not_awaited()
        message.add_reaction.assert_not_awaited()

    async def test_a_plain_message_earns_a_reaction(self):
        message = self.message()
        await self.cog.on_message(message)
        message.add_reaction.assert_awaited()
        message.reply.assert_not_awaited()

    async def test_an_ordinary_message_between_two_is_not_answered_twice(self):
        first = self.message()
        await self.cog.on_message(first)
        self.assertEqual(first.add_reaction.await_count, 1)
        second = self.message()
        await self.cog.on_message(second)
        second.add_reaction.assert_not_awaited()
        second.reply.assert_not_awaited()

    async def test_a_keyword_gets_a_sentence_and_no_reaction(self):
        message = self.message(content="i am so bored")
        await self.cog.on_message(message)
        message.reply.assert_awaited()
        message.add_reaction.assert_not_awaited()
        self.assertEqual(message.reply.await_args.kwargs.get("mention_author"), False)

    async def test_the_answer_uses_the_model_when_one_answers(self):
        with mock.patch.object(
            catfeed, "say", mock.AsyncMock(return_value="go and be bored elsewhere. 🐾")
        ):
            message = self.message(content="i am so bored")
            await self.cog.on_message(message)
        message.reply.assert_awaited()
        self.assertIn("go and be bored elsewhere.", message.reply.await_args.args[0])

    async def test_the_written_line_is_what_it_says_when_nothing_answers(self):
        message = self.message(content="i am so bored")  # the harness stubs the brain
        await self.cog.on_message(message)
        message.reply.assert_awaited()
        written = next(
            lines for needles, lines in devtoy.KEYWORD_REPLIES if "bored" in needles
        )
        self.assertIn(message.reply.await_args.args[0], written)

    async def test_a_slow_thought_cannot_move_the_reply_clock(self):
        """The allowance is spent *before* the model is asked, not after.

        A generation takes seconds, and a member who types twice while it runs must
        not get two answers out of one allowance — which is what happens if the
        clock is written once the sentence comes back.
        """
        seen: dict[str, float | None] = {}

        async def slow(messages, **_kwargs):
            seen["clock"] = self.cog._last_reply.get(5)
            return None

        with mock.patch.object(catfeed, "say", slow):
            message = self.message(content="i am so bored")
            await self.cog.on_message(message)
        self.assertIsNotNone(seen.get("clock"), "the clock has to move before the call")

    async def test_a_mention_of_the_bot_gets_a_sentence(self):
        self.cog.bot.user = SimpleNamespace(id=permissions.VIP_BOT_ID)
        message = self.message(content="<@1558691012975534222> hello", mentions=[
            SimpleNamespace(id=permissions.VIP_BOT_ID)
        ])
        await self.cog.on_message(message)
        message.reply.assert_awaited()

    async def test_a_broken_message_never_takes_the_listener_down(self):
        message = mock.MagicMock()
        message.guild = None
        message.author = None
        message.reply = mock.AsyncMock()
        await self.cog.on_message(message)  # must not raise
        message.reply.assert_not_awaited()


class TheOperatorCanRehearseTheChatHalf(CogCase):
    """What the cat would say, asked without anybody typing it.

    The chat half's last hop was unanswerable from a terminal: the listener ignores
    bots and webhooks by design, so a message has to come from a person, and *"is
    the cat talking in there"* therefore had no answer on the machine that had just
    been deployed. ``rehearse`` drives the same decision directly, which is worth
    something only if it is **the same** decision — so the laws here are that it
    names the gate that refused, that it says which half wrote the sentence, and
    that it moves no clock while it does it.
    """

    async def test_a_mention_in_this_server_is_one_it_would_answer(self):
        report = await self.cog.rehearse(DEV_GUILD, 5, "hello there", mentioned=True)
        self.assertIsNone(report["gate"])
        self.assertEqual(report["would"], "reply")
        self.assertEqual(report["matched"], "mention")
        self.assertTrue(report["reply"])
        self.assertIsNone(report["react"])

    async def test_the_written_line_is_what_a_host_without_a_key_gets(self):
        """The harness's brain returns nothing, which is also a host with no key."""
        report = await self.cog.rehearse(DEV_GUILD, 5, "hello", mentioned=True)
        self.assertEqual(report["source"], "written")
        self.assertIn(report["reply"], devtoy.MENTION_LINES)

    async def test_a_rehearsal_reports_the_model_when_there_is_one(self):
        with mock.patch.object(catfeed, "say", mock.AsyncMock(return_value="mrrp.")):
            report = await self.cog.rehearse(DEV_GUILD, 5, "hello", mentioned=True)
        self.assertEqual(report["reply"], "mrrp.")
        self.assertEqual(report["source"], "model")

    async def test_a_bare_message_is_the_react_it_would_use(self):
        report = await self.cog.rehearse(DEV_GUILD, 5, "the build finished at last")
        self.assertEqual(report["would"], "react")
        self.assertIn(report["react"], devtoy.REACTION_EMOJI)
        self.assertIsNone(report["reply"])

    async def test_another_server_is_refused_with_the_reason(self):
        report = await self.cog.rehearse(OTHER_GUILD, 5, "hello", mentioned=True)
        self.assertEqual(report["would"], "nothing")
        self.assertIn("own servers only", report["gate"])
        self.assertIsNone(report["reply"])

    async def test_every_gate_says_which_one_it_is(self):
        """Four gates, four sentences: a silence with no reason was the old bug."""
        off = DevToy(fake_bot(module_on=False))
        self.addCleanup(off.cog_unload)
        self.assertIn(
            "switched off",
            (await off.rehearse(DEV_GUILD, 5, "hello", mentioned=True))["gate"],
        )

        deaf = DevToy(fake_bot(message_content=False))
        self.addCleanup(deaf.cog_unload)
        self.assertIn(
            "Message Content",
            (await deaf.rehearse(DEV_GUILD, 5, "hello", mentioned=True))["gate"],
        )

        await db.set_setting(DEV_GUILD, features.setting_key(MODULE, "reactions"), False)
        quiet = await self.cog.rehearse(DEV_GUILD, 5, "hello", mentioned=True)
        self.assertIn("Answers in chat", quiet["gate"])

    async def test_a_rehearsal_costs_the_member_nothing(self):
        """No clock moves: asking what the cat would say must not spend the answer."""
        for _ in range(3):
            report = await self.cog.rehearse(DEV_GUILD, 5, "hello", mentioned=True)
            self.assertEqual(report["would"], "reply")
        self.assertEqual(self.cog._last_reply, {})
        self.assertEqual(self.cog._last_react, {})
        # And the real decision is still unspent, asked through the same call the
        # listener makes.
        decision, _cat = await self.cog.chat_decision_for(
            DEV_GUILD, 5, "hello", mentioned=True
        )
        self.assertTrue(decision.reply)


class TheAmbientLoopIsQuiet(CogCase):
    async def test_nothing_is_sent_without_a_channel_to_send_it_to(self):
        guild = fake_guild(DEV_GUILD, channels=[])
        self.cog.bot.guilds = [guild]
        self.cog.bot.get_guild = lambda _id: guild
        await self.cog._ambient_once()
        self.assertEqual((await db.devcat_ambient(DEV_GUILD))["nudge_count"], 0)

    async def test_nothing_is_sent_without_a_cat(self):
        channel = SimpleNamespace(id=999, mention="#dev", send=mock.AsyncMock())
        guild = fake_guild(DEV_GUILD, channels=[channel])
        self.cog.bot.get_guild = lambda _id: guild
        await self.cog._ambient_once()
        channel.send.assert_not_awaited()

    async def test_a_card_lands_in_the_cat_s_room_and_spends_the_allowance(self):
        channel = SimpleNamespace(id=bot_channels.DEV_CHAT_CHANNEL_ID, mention="#cat", send=mock.AsyncMock())
        guild = fake_guild(DEV_GUILD, channels=[channel])
        self.cog.bot.get_guild = lambda _id: guild
        await self.cog._save(DEV_GUILD, 5, devtoy.adopt(time.time(), "Biscuit"))
        await self.cog._ambient_once()
        channel.send.assert_awaited()
        state = await db.devcat_ambient(DEV_GUILD)
        self.assertEqual(state["nudge_count"], 1)
        self.assertGreater(state["last_nudge"], 0)

    async def test_the_cat_s_own_card_opens_with_what_it_thought_of(self):
        channel = SimpleNamespace(
            id=bot_channels.DEV_CHAT_CHANNEL_ID, mention="#cat", send=mock.AsyncMock()
        )
        guild = fake_guild(DEV_GUILD, channels=[channel])
        self.cog.bot.get_guild = lambda _id: guild
        await self.cog._save(DEV_GUILD, 5, devtoy.adopt(time.time(), "Biscuit"))
        with mock.patch.object(
            catfeed, "say", mock.AsyncMock(return_value="the chair is still cold.")
        ):
            await self.cog._ambient_once()
        channel.send.assert_awaited()
        text = card_text(dict(channel.send.await_args.kwargs))
        self.assertIn("the chair is still cold.", text)
        # The written openers are still what the card falls back to, so one of
        # them must not be in a card the model opened.
        self.assertFalse(
            any(opener in text for pool in devtoy.AMBIENT_OPENERS.values() for opener in pool)
        )

    async def test_the_gap_stops_the_second_card(self):
        channel = SimpleNamespace(id=bot_channels.DEV_CHAT_CHANNEL_ID, mention="#cat", send=mock.AsyncMock())
        guild = fake_guild(DEV_GUILD, channels=[channel])
        self.cog.bot.get_guild = lambda _id: guild
        await self.cog._save(DEV_GUILD, 5, devtoy.adopt(time.time(), "Biscuit"))
        await self.cog._ambient_once()
        await self.cog._ambient_once()
        self.assertEqual(channel.send.await_count, 1)
        self.assertEqual((await db.devcat_ambient(DEV_GUILD))["nudge_count"], 1)

    async def test_nudges_off_means_nothing_at_all(self):
        await db.set_setting(DEV_GUILD, features.setting_key("devcat", "nudges"), False)
        channel = SimpleNamespace(id=bot_channels.DEV_CHAT_CHANNEL_ID, mention="#cat", send=mock.AsyncMock())
        guild = fake_guild(DEV_GUILD, channels=[channel])
        self.cog.bot.get_guild = lambda _id: guild
        await self.cog._save(DEV_GUILD, 5, devtoy.adopt(time.time(), "Biscuit"))
        await self.cog._ambient_once()
        channel.send.assert_not_awaited()

    async def test_a_asked_for_card_does_not_spend_the_allowance(self):
        channel = SimpleNamespace(id=bot_channels.DEV_CHAT_CHANNEL_ID, mention="#cat", send=mock.AsyncMock())
        guild = fake_guild(DEV_GUILD, channels=[channel])
        self.cog.bot.get_guild = lambda _id: guild
        await self.cog._save(DEV_GUILD, 5, devtoy.adopt(time.time(), "Biscuit"))
        interaction = fake_interaction()
        interaction.guild = guild
        await self.cog.send_nudge(interaction)
        channel.send.assert_awaited()
        self.assertEqual((await db.devcat_ambient(DEV_GUILD))["nudge_count"], 0)

    async def test_the_cat_talks_in_the_configured_room_first(self):
        override = SimpleNamespace(id=555, mention="#elsewhere", send=mock.AsyncMock())
        hardcoded = SimpleNamespace(id=bot_channels.DEV_CHAT_CHANNEL_ID, mention="#cat", send=mock.AsyncMock())
        guild = fake_guild(DEV_GUILD, channels=[override, hardcoded])
        await db.set_setting(DEV_GUILD, features.setting_key("devcat", "home_channel"), "555")
        chosen = await self.cog.ambient_channel(guild)
        self.assertEqual(getattr(chosen, "id", None), 555)

    async def test_an_unreadable_home_channel_falls_through_to_the_hardcoded_room(self):
        hardcoded = SimpleNamespace(id=bot_channels.DEV_CHAT_CHANNEL_ID, mention="#cat", send=mock.AsyncMock())
        guild = fake_guild(DEV_GUILD, channels=[hardcoded])
        await db.set_setting(DEV_GUILD, features.setting_key("devcat", "home_channel"), "not-a-number")
        chosen = await self.cog.ambient_channel(guild)
        self.assertEqual(getattr(chosen, "id", None), bot_channels.DEV_CHAT_CHANNEL_ID)

    async def test_with_neither_the_cat_says_nothing_rather_than_guessing(self):
        guild = fake_guild(DEV_GUILD, channels=[])
        self.assertIsNone(await self.cog.ambient_channel(guild))


# --------------------------------------------------------------------------- #
# The two facts that keep it in the right place
# --------------------------------------------------------------------------- #
class TheCogIsTheVipBotOnly(unittest.TestCase):
    def test_the_registry_says_which_cog_is_the_vip_bots(self):
        self.assertIn("cogs.devtoy", features.vip_only_cogs())

    def test_the_loader_reads_that_rather_than_a_second_list(self):
        source = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("features.vip_only_cogs()", source)
        self.assertIn("if cog in vip_only and not is_vip:", source)

    def test_the_root_is_reserved_to_the_vip_instance(self):
        self.assertIn("cat", permissions.VIP_ONLY_COMMANDS)
        # The arcade is a subcommand (`/cat bored`) because the tree already holds
        # Discord's hundred top-level commands — so "bored" is not a name, and
        # nothing may claim it is.
        self.assertNotIn("bored", permissions.VIP_ONLY_COMMANDS)
        from utils import command_scope

        main = command_scope.for_identity({"cat", "help"}, is_vip=False)
        self.assertEqual(main, {"help"})
        vip = command_scope.for_identity({"cat", "help"}, is_vip=True)
        self.assertEqual(vip, {"cat"})

    def test_the_registry_entry_is_off_and_only_one_server_turns_it_on(self):
        feature = features.get("devcat")
        self.assertIsNotNone(feature)
        self.assertFalse(feature.enabled_by_default)
        self.assertTrue(feature.toggleable)
        self.assertTrue(feature.vip_only)
        self.assertEqual(feature.cog, "cogs.devtoy")
        self.assertEqual(set(feature.gate_commands), {"cat"})

    def test_an_off_by_default_system_is_not_published_until_it_is_on(self):
        from utils import command_scope

        self.assertIn("devcat", features.DEFAULT_DISABLED_MODULES)
        states = {}
        self.assertIn("cat", command_scope.hidden_names(["cat", "help"], states))

    def test_every_command_it_declares_is_one_the_bot_has(self):
        feature = features.get("devcat")
        declared = {name.lstrip("/").split(" ")[0] for name, _ in features.feature_entries(feature)}
        self.assertEqual(declared, {"cat"})
        from utils import permissions as perms

        for path in (
            "cat adopt", "cat status", "cat feed", "cat pet", "cat play", "cat rename",
            "cat top", "cat photo", "cat fact", "cat ask", "cat bored", "cat panel",
            "cat settings",
        ):
            self.assertIn(path, perms.COMMAND_ACCESS)
        self.assertEqual(perms.COMMAND_ACCESS["cat settings"], perms.ADMIN)
        self.assertEqual(perms.COMMAND_ACCESS["cat panel"], perms.ADMIN)
        self.assertEqual(perms.COMMAND_ACCESS["cat bored"], perms.EVERYONE)
        # The three that fetch are everyone's: a picture of a cat is not a
        # moderation act, and the one that costs a network call is bounded by the
        # server's own brain switch rather than by a permission bit.
        for path in ("cat photo", "cat fact", "cat ask"):
            self.assertEqual(perms.COMMAND_ACCESS[path], perms.EVERYONE)

    def test_the_settings_it_declares_have_one_home_each(self):
        keys = [features.setting_key("devcat", spec.id) for spec in features.settings_for("devcat")]
        self.assertEqual(
            keys,
            [
                "devcat_nudges", "devcat_reactions", "devcat_ping", "devcat_brain",
                "devcat_home_channel",
            ],
        )
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_help_panel_has_a_page_for_it(self):
        from cogs.help import HELP_MODULES

        self.assertIn("devcat", HELP_MODULES)
        self.assertTrue(HELP_MODULES["devcat"]["desc"])


class TheCogLoadsWithEveryOtherCog(unittest.IsolatedAsyncioTestCase):
    """The registry's own cross-check, asked about this cog specifically."""

    async def test_it_loads_and_registers_the_commands_it_claims(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        try:
            await bot.load_extension("cogs.devtoy")
            names = {path for path, _command in permissions.walk(bot.tree)}
            self.assertIn("cat adopt", names)
            self.assertIn("cat settings", names)
            self.assertIn("cat bored", names)
            self.assertEqual(features.MODULE_COMMAND_MAP.get("cat"), ("devcat",))
            for path in names:
                if path.startswith("cat"):
                    self.assertIn(path, permissions.COMMAND_ACCESS)
        finally:
            cog = bot.get_cog("DevToy")
            if cog is not None:
                cog.cog_unload()
            await bot.close()

    async def test_the_cog_starts_no_background_noise_outside_a_running_loop(self):
        """``ambient.start()`` outside a loop is a crash, so it does not happen."""
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        try:
            cog = DevToy(bot)
            self.assertTrue(cog.ambient.is_running(), "a running loop should start the tick")
            cog.cog_unload()
            # ``cancel`` only *asks*; the tick reads as running until the loop has
            # had a turn to process the cancellation.
            await asyncio.sleep(0)
            self.assertFalse(cog.ambient.is_running())
        finally:
            await bot.close()


class TheStoredTablesAreCreatedBySetup(CogCase):
    async def test_every_table_exists_after_setup(self):
        connection = db.connect()
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        finally:
            connection.close()
        names = {row[0] for row in rows}
        for table in ("devcat_pets", "devcat_records", "devcat_ambient"):
            self.assertIn(table, names)

    async def test_a_cat_row_survives_a_written_and_read_cycle_of_every_column(self):
        row = {
            "name": "Biscuit", "fed": 1.5, "mood": 2.5, "energy": 3.5,
            "adopted_at": 4.5, "updated_at": 5.5, "last_fed": 6.5, "last_pet": 7.5,
            "last_play": 8.5, "care_days": 9, "last_care_day": "2026-10-14",
            "care_score": 10, "games": 11,
        }
        await db.save_devcat_pet(DEV_GUILD, 5, row)
        stored = await db.devcat_pet(DEV_GUILD, 5)
        for key, value in row.items():
            self.assertEqual(stored[key], value, key)


if __name__ == "__main__":
    unittest.main()
