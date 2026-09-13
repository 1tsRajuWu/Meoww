"""The XP/coin maths behind the text and voice rewards, and the anti-AFK and
anti-spam gates that feed it.

Two layers, because either alone would pass while the system misbehaved:

* ``TestRewardMath`` drives ``utils/rewards`` directly. Every formula there is
  pure, so the awkward cases — a solo channel, a deafened member, reposted text,
  a flood — are pinned without a Discord connection.
* ``TestLevelingCallbacks`` drives the real ``cogs/leveling.py`` listener and the
  real voice loop against a throwaway database, because formulas that are right
  but unwired change nothing.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import economy as economy_module
from cogs import leveling as leveling_module
from tests.support import TempDatabaseMixin
from utils import db, rewards, shop

GUILD = 777
SPAMMER = 11
CHATTER = 22
VC_ID = 900
BASE_TIME = 1_700_000_000.0


class TestRewardMath(unittest.TestCase):
    # -- text: effort ------------------------------------------------------ #
    def test_effort_quality_is_monotonic_and_saturates_below_one(self):
        scores = [
            rewards.effort_quality(rewards.effort_points(chars))
            for chars in (0, 5, 30, 120, 600, 5_000)
        ]
        self.assertEqual(scores[0], 0.0)
        for lower, higher in zip(scores, scores[1:]):
            self.assertLess(lower, higher)
        self.assertLessEqual(scores[-1], 1.0)
        self.assertGreater(scores[4], 0.99, "a long message should be near the top")
        # Diminishing returns, not linear: ten times the text is far less than
        # ten times the score — otherwise a wall of text is the best farm.
        self.assertLess(scores[4], 10 * scores[2])

    def test_attachments_count_as_effort_but_are_capped(self):
        one = rewards.effort_points(0, 1)
        self.assertEqual(one, rewards.ATTACHMENT_POINTS)
        many = rewards.effort_points(0, 99)
        self.assertLessEqual(many, rewards.MAX_ATTACHMENT_POINTS)
        self.assertGreater(many, one)

    def test_effort_outranks_shouting(self):
        """More genuine effort must always beat less of it."""
        quiet = rewards.text_reward(
            content_length=200, text_xp=15, text_min_coins=4, text_max_coins=10
        )
        shout = rewards.text_reward(
            content_length=4, text_xp=15, text_min_coins=4, text_max_coins=10
        )
        self.assertGreater(quiet.coins, shout.coins)
        self.assertGreater(quiet.xp, shout.xp)

    # -- text: anti-spam --------------------------------------------------- #
    def test_repeated_text_is_discounted_hard(self):
        clean = rewards.text_reward(
            content_length=200, text_xp=15, text_min_coins=4, text_max_coins=10
        )
        repeat = rewards.text_reward(
            content_length=200,
            text_xp=15,
            text_min_coins=4,
            text_max_coins=10,
            duplicate_ratio=1.0,
        )
        self.assertLess(repeat.coins, clean.coins / 2)
        self.assertLess(repeat.xp, clean.xp / 2)

    def test_flooding_and_emoji_only_messages_are_discounted(self):
        normal = rewards.text_reward(
            content_length=60, text_xp=15, text_min_coins=4, text_max_coins=10
        )
        flood = rewards.text_reward(
            content_length=60,
            text_xp=15,
            text_min_coins=4,
            text_max_coins=10,
            tempo_ratio=3.0,
        )
        emoji = rewards.text_reward(
            content_length=60,
            text_xp=15,
            text_min_coins=4,
            text_max_coins=10,
            low_signal=True,
        )
        self.assertLess(flood.xp, normal.xp)
        self.assertLess(emoji.xp, normal.xp)
        self.assertLess(flood.coins, normal.coins)

    def test_penalties_stack_but_never_reach_zero(self):
        payout = rewards.text_reward(
            content_length=200,
            text_xp=15,
            text_min_coins=4,
            text_max_coins=10,
            duplicate_ratio=1.0,
            tempo_ratio=99.0,
            low_signal=True,
        )
        self.assertGreaterEqual(payout.coins, 1)
        self.assertGreaterEqual(payout.xp, 1)

    def test_integrity_is_clamped_to_its_floor(self):
        self.assertEqual(rewards.integrity(5.0, 5.0, True), rewards.MIN_INTEGRITY)
        self.assertEqual(rewards.integrity(0.0, 0.0), 1.0)
        # A negative or nonsense ratio is not a bonus.
        self.assertEqual(rewards.integrity(-3.0, -3.0), 1.0)

    # -- text: bands and bounds -------------------------------------------- #
    def test_payout_stays_inside_the_configured_band_times_the_multiplier(self):
        for chars in (3, 20, 100, 400, 4_000):
            payout = rewards.text_reward(
                content_length=chars, text_xp=15, text_min_coins=4, text_max_coins=10
            )
            self.assertGreaterEqual(payout.coins, 1)
            self.assertLessEqual(payout.coins, round(10 * rewards.TOP_MULTIPLIER) + 1)
            self.assertGreaterEqual(payout.xp, 1)
            self.assertLessEqual(payout.xp, round(15 * rewards.TOP_MULTIPLIER) + 1)

    def test_unreadable_text_still_earns_but_still_notices_a_flood(self):
        """Message Content intent off must not silently disable text rewards."""
        quiet = rewards.text_reward(
            content_length=0, readable=False, text_xp=15, text_min_coins=4, text_max_coins=10
        )
        flooded = rewards.text_reward(
            content_length=0,
            readable=False,
            text_xp=15,
            text_min_coins=4,
            text_max_coins=10,
            tempo_ratio=4.0,
        )
        self.assertGreater(quiet.xp, 1)
        self.assertLess(flooded.xp, quiet.xp)

    def test_a_swapped_band_is_corrected(self):
        payout = rewards.text_reward(
            content_length=200, text_xp=15, text_min_coins=10, text_max_coins=4
        )
        self.assertGreater(payout.coins, 0)

    # -- voice: the anti-AFK gates ----------------------------------------- #
    def test_solo_members_earn_nothing(self):
        self.assertEqual(rewards.voice_company_factor(0), 0.0)
        factor = rewards.voice_factor(
            presence=1.0, company=rewards.voice_company_factor(0), freshness=1.0
        )
        self.assertEqual(factor, 0.0)
        self.assertEqual(rewards.voice_reward(factor, vc_xp=3, vc_coins=2).xp, 0)

    def test_company_grows_logarithmically_and_caps_at_one(self):
        factors = [rewards.voice_company_factor(n) for n in (1, 2, 3, 4, 40)]
        for lower, higher in zip(factors, factors[1:]):
            self.assertLessEqual(lower, higher)
        self.assertLess(factors[0], 1.0)
        self.assertEqual(rewards.voice_company_factor(4), 1.0)
        self.assertEqual(rewards.voice_company_factor(40), 1.0)

    def test_deafened_or_suppressed_members_earn_nothing(self):
        for flags in ({"self_deaf": True}, {"deaf": True}, {"mute": True}, {"suppress": True}):
            self.assertEqual(rewards.voice_presence(**flags), 0.0, flags)

    def test_self_mute_halves_and_camera_or_stream_lifts(self):
        listening = rewards.voice_presence(self_mute=True)
        talking = rewards.voice_presence()
        self.assertLess(listening, talking)
        self.assertGreater(rewards.voice_presence(self_video=True), talking)
        self.assertGreater(rewards.voice_presence(self_stream=True), talking)
        self.assertLessEqual(
            rewards.voice_presence(self_mute=True, self_stream=True), rewards.VC_MAX_FACTOR
        )

    def test_freshness_decays_then_stops_entirely(self):
        self.assertEqual(rewards.voice_freshness(0), 1.0)
        self.assertEqual(rewards.voice_freshness(rewards.VC_FULL_SECONDS), 1.0)
        middle = rewards.voice_freshness((rewards.VC_FULL_SECONDS + rewards.VC_DEAD_SECONDS) / 2)
        self.assertLess(middle, 1.0)
        self.assertGreater(middle, rewards.VC_STALE_FLOOR)
        self.assertEqual(rewards.voice_freshness(rewards.VC_DEAD_SECONDS), 0.0)
        self.assertEqual(rewards.voice_freshness(rewards.VC_DEAD_SECONDS * 10), 0.0)

    def test_a_quiet_channel_pays_less_than_a_live_one(self):
        live = rewards.voice_factor(presence=1.0, company=1.0, freshness=1.0)
        stale = rewards.voice_factor(
            presence=1.0,
            company=1.0,
            freshness=rewards.voice_freshness(rewards.VC_DEAD_SECONDS * 2),
        )
        self.assertGreater(live, stale)
        self.assertEqual(stale, 0.0)

    def test_voice_reward_uses_the_configured_rate_as_a_ceiling(self):
        top = rewards.voice_reward(rewards.VC_MAX_FACTOR, vc_xp=3, vc_coins=2)
        self.assertLessEqual(top.xp, round(3 * rewards.VC_MAX_FACTOR))
        self.assertLessEqual(top.coins, round(2 * rewards.VC_MAX_FACTOR))
        # Any paid minute is worth at least something.
        token = rewards.voice_reward(0.01, vc_xp=3, vc_coins=2)
        self.assertGreaterEqual(token.coins, 1)
        self.assertGreaterEqual(token.xp, 1)

    def test_a_rate_of_zero_pays_zero_rather_than_one(self):
        self.assertEqual(rewards.voice_reward(1.0, vc_xp=0, vc_coins=0).xp, 0)
        self.assertEqual(rewards.voice_reward(1.0, vc_xp=0, vc_coins=0).coins, 0)

    # -- trackers ---------------------------------------------------------- #
    def test_first_message_is_never_a_duplicate_of_itself(self):
        screen = rewards.SpamScreen()
        stats = screen.record(GUILD, SPAMMER, "good morning", BASE_TIME)
        self.assertEqual(stats.duplicate_ratio, 0.0)
        self.assertFalse(stats.low_signal)

    def test_reposting_the_same_text_is_flagged(self):
        screen = rewards.SpamScreen()
        screen.record(GUILD, SPAMMER, "good morning", BASE_TIME)
        stats = screen.record(GUILD, SPAMMER, "  Good   MORNING!  ", BASE_TIME + 20)
        self.assertEqual(stats.duplicate_ratio, 1.0)

    def test_history_expires_so_old_messages_stop_counting(self):
        screen = rewards.SpamScreen()
        screen.record(GUILD, SPAMMER, "good morning", BASE_TIME)
        stats = screen.record(
            GUILD, SPAMMER, "good morning", BASE_TIME + rewards.HISTORY_SECONDS + 1
        )
        self.assertEqual(stats.duplicate_ratio, 0.0)
        self.assertLess(stats.tempo_ratio, 1.0)

    def test_tempo_counts_messages_towards_the_allowance(self):
        screen = rewards.SpamScreen()
        stats = None
        for index in range(rewards.TEMPO_ALLOWANCE + 5):
            stats = screen.record(GUILD, SPAMMER, f"line {index}", BASE_TIME + index)
        self.assertGreater(stats.tempo_ratio, 1.0)

    def test_screen_and_clock_stay_bounded(self):
        screen = rewards.SpamScreen(kept=4, max_users=8)
        for user in range(200):
            screen.record(GUILD, user, f"hello number {user}", BASE_TIME + user)
        self.assertLessEqual(len(screen._history), 8)

        clock = rewards.PulseClock(limit=4)
        for channel in range(100):
            clock.touch(channel, BASE_TIME + channel)
        self.assertLessEqual(len(clock), 4)

    def test_pulse_clock_reports_unknown_channels_as_fresh(self):
        clock = rewards.PulseClock()
        self.assertEqual(clock.age(VC_ID, BASE_TIME), 0.0)
        # ``seen`` starts the clock for a channel the bot has not observed yet, so
        # a restart does not read every occupied channel as dead.
        self.assertEqual(clock.seen(VC_ID, BASE_TIME), 0.0)
        self.assertEqual(clock.seen(VC_ID, BASE_TIME + 60), 60.0)
        clock.forget(VC_ID)
        self.assertEqual(clock.age(VC_ID, BASE_TIME), 0.0)


# --------------------------------------------------------------------------- #
# The real listeners, against a throwaway database
# --------------------------------------------------------------------------- #
class _Clock:
    """A clock the tests can move, so cooldowns and decays are deterministic."""

    def __init__(self, now: float = BASE_TIME):
        self.now = float(now)

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class _TimeProxy:
    """Stands in for ``cogs.leveling``'s ``time``, with a movable ``time.time``."""

    def __init__(self, clock: _Clock):
        self._clock = clock

    def time(self) -> float:
        return self._clock.now

    def __getattr__(self, name):
        import time as real_time

        return getattr(real_time, name)


class _FakeVoiceState:
    def __init__(self, channel=None, **flags):
        self.channel = channel
        self.self_deaf = flags.get("self_deaf", False)
        self.deaf = flags.get("deaf", False)
        self.mute = flags.get("mute", False)
        self.self_mute = flags.get("self_mute", False)
        self.suppress = flags.get("suppress", False)
        self.self_video = flags.get("self_video", False)
        self.self_stream = flags.get("self_stream", False)


class _FakeMember:
    def __init__(self, uid: int, bot: bool = False):
        self.id = uid
        self.bot = bot
        self.display_name = f"User{uid}"
        self.mention = f"<@{uid}>"
        self.guild = None
        self.voice = None


class _FakeVC:
    def __init__(self, channel_id: int = VC_ID, members=()):
        self.id = channel_id
        self.name = f"voice-{channel_id}"
        self.members = list(members)


class _FakeGuild:
    def __init__(self, guild_id: int = GUILD):
        self.id = guild_id
        self.name = "Test Guild"
        self.voice_channels = []
        self.afk_channel = None


class _FakeMessage:
    def __init__(self, author, guild, content: str = "", attachments: int = 0):
        self.author = author
        self.guild = guild
        self.content = content
        self.attachments = [SimpleNamespace(filename=f"f{i}.png") for i in range(attachments)]
        self.channel = None


def _wallet(user_id: int, guild_id: int = GUILD) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (user_id, guild_id)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _row(user_id: int, guild_id: int = GUILD):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT xp, level, total_messages, voice_minutes, total_earned "
            "FROM user_levels WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
    finally:
        conn.close()


class _LevelingCase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("rewards.db")
        # The cogs captured the database path at import time.
        leveling_module.DB_PATH = path
        economy_module.DB_PATH = path
        await db.setup()
        # utils.shop caches multipliers per (member, guild) for a few seconds — the
        # same key every test here uses, so it has to be cleared between them.
        shop.invalidate()

        self.clock = _Clock()
        time_patch = mock.patch.object(leveling_module, "time", _TimeProxy(self.clock))
        time_patch.start()
        self.addCleanup(time_patch.stop)

        self.level_ups = []
        self.bot = SimpleNamespace(is_module_enabled=lambda *args, **kwargs: True, guilds=[])
        self.cog = leveling_module.Leveling(self.bot)

        async def record_level_up(guild, member, old, new, channel=None):
            self.level_ups.append((old, new))

        self.cog.handle_level_up = record_level_up

    async def asyncTearDown(self):
        shop.invalidate()
        self.release_temp_database()

    async def _send(self, content: str, author=None, attachments: int = 0):
        guild = _FakeGuild()
        author = author or _FakeMember(CHATTER)
        await self.cog.on_message(_FakeMessage(author, guild, content, attachments))
        return author

    def _scene(self, *specs):
        """Build a guild with one voice channel holding the given members.

        ``specs`` are ``(user_id, voice_flags)`` pairs. Returns ``(guild, vc,
        members)`` with every member wired to the guild, the channel and its own
        voice state, and the guild registered with the bot so the loop sees it.
        """
        guild = _FakeGuild()
        members = []
        vc = _FakeVC()
        for uid, flags in specs:
            member = _FakeMember(uid)
            member.guild = guild
            member.voice = _FakeVoiceState(channel=vc, **flags)
            members.append(member)
        vc.members = list(members)
        guild.voice_channels = [vc]
        self.bot.guilds = [guild]
        return guild, vc, members

    async def _join(self, member):
        """Fire the join that puts a member's channel on the activity clock."""
        before = SimpleNamespace(channel=None)
        return await self.cog.on_voice_state_update(member, before, member.voice)

    async def _tick(self):
        """Run the voice reward loop once, as the 60-second task would."""
        return await self.cog.vc_reward_loop.coro(self.cog)


class TestLevelingCallbacks(_LevelingCase):
    async def test_effort_beats_repeated_spam(self):
        """The whole point: identical messages stop paying, real ones keep paying."""
        same = "gm everyone hope you are all having a good day today!"
        for _ in range(6):
            await self._send(same, author=_FakeMember(SPAMMER))
            self.clock.advance(rewards.REWARD_COOLDOWN_SECONDS + 1)

        varied = [
            "gm everyone hope you are all having a good day today!",
            "anyone up for a quick game of something this evening?",
            "i finally finished that project i was stuck on for a week",
            "the weather here has been absolutely miserable all week honestly",
            "what is everyone listening to at the moment, i need new music",
            "shoutout to whoever fixed the bot commands earlier, nice work",
        ]
        for content in varied:
            await self._send(content)
            self.clock.advance(rewards.REWARD_COOLDOWN_SECONDS + 1)

        spammer_wallet = _wallet(SPAMMER)
        chatter_wallet = _wallet(CHATTER)
        self.assertGreater(spammer_wallet, 0, "precondition: spamming paid nothing at all")
        self.assertGreater(chatter_wallet, spammer_wallet * 2, "spam still out-earned effort")
        # Both are counted as six messages; only the payout differs.
        self.assertEqual(_row(SPAMMER)[2], 6)
        self.assertEqual(_row(CHATTER)[2], 6)

    async def test_cooldown_messages_still_count_but_pay_nothing(self):
        await self._send("a properly long first message about something")
        first = _wallet(CHATTER)
        self.clock.advance(2)  # inside the reward cooldown
        await self._send("and another properly long message right away")
        self.assertEqual(_wallet(CHATTER), first, "the cooldown did not hold")
        self.assertEqual(_row(CHATTER)[2], 2, "the message was not counted")

    async def test_tiny_messages_are_counted_but_not_paid(self):
        await self._send("k")
        self.assertEqual(_wallet(CHATTER), 0)
        self.assertEqual(_row(CHATTER)[2], 1)

    async def test_attachments_earn_where_short_text_would_not(self):
        """A meme with no caption is real effort; the same with no attachment is not."""
        await self._send("", attachments=2)
        self.assertGreater(_wallet(CHATTER), 0, "an attachment-only post earned nothing")
        self.assertGreater(_row(CHATTER)[0], 0, "an attachment-only post earned no XP")

    async def test_joining_voice_grants_no_text_activity(self):
        """Regression: a join used to write a free hour into last_message_ts."""
        _, vc, (member,) = self._scene((CHATTER, {}))
        await self._join(member)

        self.assertIsNone(_row(CHATTER), "a voice join created a levels row")
        self.assertEqual(self.cog._pulse.age(vc.id, self.clock.now), 0.0)

    async def test_solo_member_in_voice_earns_nothing(self):
        _, _, _ = self._scene((CHATTER, {}))

        await self._tick()

        self.assertEqual(_wallet(CHATTER), 0, "a solo member was paid")
        self.assertIsNone(_row(CHATTER), "a solo member accrued voice minutes")

    async def test_a_pair_in_voice_is_paid_and_the_minute_is_counted(self):
        _, _, (chatter, spammer) = self._scene((CHATTER, {}), (SPAMMER, {}))
        await self._join(chatter)

        await self._tick()

        self.assertGreater(_wallet(CHATTER), 0)
        self.assertGreater(_wallet(SPAMMER), 0)
        self.assertEqual(_row(CHATTER)[3], 1, "the paid minute was not counted")
        self.assertEqual(_row(CHATTER)[4], _wallet(CHATTER))
        self.assertGreater(_row(CHATTER)[0], 0, "the paid minute earned no XP")

    async def test_deafened_and_server_muted_members_are_paid_nothing(self):
        _, _, (deafened, muted, talker) = self._scene(
            (CHATTER, {"self_deaf": True}), (SPAMMER, {"mute": True}), (33, {})
        )
        await self._join(talker)

        await self._tick()

        self.assertEqual(_wallet(CHATTER), 0, "a deafened member was paid")
        self.assertEqual(_wallet(SPAMMER), 0, "a server-muted member was paid")
        self.assertGreater(_wallet(33), 0)

    async def test_self_muted_members_earn_less_than_unmuted_ones(self):
        _, _, (quiet, loud) = self._scene((CHATTER, {"self_mute": True}), (SPAMMER, {}))
        await self._join(loud)

        await self._tick()

        self.assertGreater(_wallet(SPAMMER), _wallet(CHATTER))

    async def test_a_channel_that_goes_silent_stops_paying(self):
        _, _, (chatter, _spammer) = self._scene((CHATTER, {}), (SPAMMER, {}))
        await self._join(chatter)
        await self._tick()
        paid = _wallet(CHATTER)
        self.assertGreater(paid, 0, "precondition: the active pair was paid")

        # Nobody joins, leaves, moves, mutes, deafens, goes live or types for six
        # hours: whatever is happening, it is not visible activity.
        self.clock.advance(rewards.VC_DEAD_SECONDS + 60)
        await self._tick()
        self.assertEqual(_wallet(CHATTER), paid, "a dead channel kept paying")

    async def test_typing_in_text_keeps_a_voice_channel_alive(self):
        _, _, (chatter, spammer) = self._scene((CHATTER, {}), (SPAMMER, {}))
        await self._join(chatter)
        self.clock.advance(rewards.VC_FULL_SECONDS + 300)

        # A message from someone sitting in the channel is evidence of life.
        await self._send("anyone still in here want to play something?", author=chatter)
        await self._tick()

        self.assertGreater(_wallet(SPAMMER), 0)

    async def test_a_message_that_earns_nothing_still_keeps_voice_alive(self):
        _, _, (chatter, spammer) = self._scene((CHATTER, {}), (SPAMMER, {}))
        # A normal message puts the channel on the clock...
        await self._send("first message with plenty of words in it", author=chatter)
        # ...then the channel goes completely silent for longer than the death window.
        self.clock.advance(rewards.VC_DEAD_SECONDS + 60)
        # A one-character message pays nothing, but it is still a human typing.
        await self._send("k", author=chatter)

        await self._tick()

        self.assertGreater(_wallet(spammer.id), 0, "the channel was treated as dead")

    async def test_a_shop_boost_multiplies_what_activity_earns(self):
        """The XP Booster has to reach the leveling listener, not just the shop."""
        await self._send("a message with plenty of words in it before any boost")
        self.clock.advance(rewards.REWARD_COOLDOWN_SECONDS + 1)
        baseline_wallet = _wallet(CHATTER)
        baseline_xp = _row(CHATTER)[0]

        await shop.grant(CHATTER, GUILD, shop.find("xp_booster")[0], now=self.clock.now)
        self.clock.advance(rewards.REWARD_COOLDOWN_SECONDS + 1)
        await self._send("another message with plenty of words in it after the boost")

        boosted_xp = _row(CHATTER)[0] - baseline_xp
        self.assertGreater(boosted_xp, baseline_xp, "the XP booster did nothing")
        self.assertGreater(_wallet(CHATTER), baseline_wallet)

    async def test_a_permanent_badge_reaches_activity_earnings(self):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO inventory (user_id, guild_id, item_name) VALUES (?, ?, ?)",
                (CHATTER, GUILD, shop.find("vip_pass")[0].name),
            )
            conn.commit()
        finally:
            conn.close()
        shop.invalidate(CHATTER, GUILD)
        await self._send("a message with plenty of words in it, badge active")
        totals = await shop.multipliers(CHATTER, GUILD, self.clock.now)
        self.assertGreater(totals.coins, 1.0)
        self.assertGreater(totals.xp, 1.0)
        self.assertGreater(_wallet(CHATTER), 0)

    async def test_the_per_minute_rate_is_a_ceiling_not_a_flat_salary(self):
        _, _, (chatter, _b, _c, _d) = self._scene((CHATTER, {}), (SPAMMER, {}), (33, {}), (44, {}))
        await self._join(chatter)

        await self._tick()

        settings = await self.cog.get_guild_settings(GUILD)
        self.assertLessEqual(_wallet(CHATTER), round(settings["vc_coins_per_min"] * 1.25))

    async def test_a_crowded_channel_pays_at_least_a_pair_does(self):
        _, _, (chatter, _b, _c, _d) = self._scene((CHATTER, {}), (SPAMMER, {}), (33, {}), (44, {}))
        await self._join(chatter)
        await self._tick()
        crowded = _wallet(CHATTER)

        _, _, (single, _other) = self._scene((CHATTER, {}), (SPAMMER, {}))
        await self._join(single)
        await self._tick()
        pair_gain = _wallet(CHATTER) - crowded

        self.assertGreaterEqual(crowded, pair_gain)


if __name__ == "__main__":
    unittest.main()
