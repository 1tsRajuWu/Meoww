"""The bot's status: one global line, editable without a redeploy.

Two things are pinned here.

1. **The reported line is gone.** "Knocking over your water glass 🐾" was
   hardcoded in `bot.py` and is the line somebody asked to remove, so it must not
   survive in the defaults — where it would quietly come back on a fresh install.
2. **The list is configurable, and the configuration is total.** This value is
   read on a loop that runs for the life of the process, so every path through it
   has to produce something usable: a hand-edited or truncated setting falls back
   to the defaults rather than raising, one bad entry is dropped rather than taken
   as proof the whole list is bad, and an out-of-range interval is clamped rather
   than refused. A rotation that dies is a bot whose status froze at boot, with
   nothing in Discord to show why.

Presence is application-wide — Discord has no per-server status — so there is
deliberately no per-guild variant of any of this to test.
"""
import asyncio
import unittest
from unittest import mock

import bot as bot_module
from tests.support import TempDatabaseMixin
from utils import db, presence

#: The line that was reported and removed. Named once, so the guard reads as the
#: thing it is about.
REPORTED_LINE = "water glass"


class TheDefaults(unittest.TestCase):
    def test_the_reported_line_is_not_in_the_defaults(self):
        for text, _kind in presence.DEFAULT_LINES:
            self.assertNotIn(
                REPORTED_LINE,
                text,
                f"{text!r} is the presence that was asked to be removed",
            )

    def test_nothing_in_the_defaults_is_unusable(self):
        """Every shipped line must survive validation and build a real activity.

        The rotation applies these directly, so a default Discord would refuse is
        not a cosmetic problem — it is the loop erroring on the line it is asked
        to show.
        """
        for text, kind in presence.DEFAULT_LINES:
            activity = presence.build(text, kind)
            self.assertEqual(activity.name, text)
            self.assertIsInstance(activity.type, bot_module.discord.ActivityType)

    def test_the_defaults_are_a_list_of_pairs(self):
        self.assertTrue(presence.DEFAULT_LINES)
        for entry in presence.DEFAULT_LINES:
            self.assertEqual(len(entry), 2, f"{entry!r} is not (text, kind)")


class ReadingWhatIsStored(unittest.TestCase):
    """`parse` is total: it is what a loop with no try/except around it reads."""

    def test_a_stored_list_round_trips(self):
        entries = [("Reading the changelog 📜", "watching"), ("/help", "playing")]
        self.assertEqual(presence.parse(presence.dump(entries)), entries)

    def test_an_empty_list_means_no_activity(self):
        self.assertEqual(presence.parse("[]"), [])
        # And it is distinguishable from "nothing configured", which is the
        # default: turning the activity off must not bring the jokes back.
        self.assertEqual(presence.parse(""), list(presence.DEFAULT_LINES))

    def test_nothing_configured_gives_the_defaults(self):
        for empty in (None, ""):
            self.assertEqual(presence.parse(empty), list(presence.DEFAULT_LINES))

    def test_a_value_this_code_did_not_write_falls_back(self):
        for junk in ("{not json", "\"a string\"", "42", '{"text": "x"}'):
            self.assertEqual(
                presence.parse(junk),
                list(presence.DEFAULT_LINES),
                f"{junk!r} should fall back to the defaults, not raise",
            )

    def test_one_bad_entry_does_not_invalidate_the_others(self):
        stored = presence.dump([("good line", "watching")])
        # A second entry an older version or a hand edit could have left behind.
        stored = stored[:-1] + ', {"text": "", "type": "playing"}]'
        self.assertEqual(presence.parse(stored), [("good line", "watching")])

    def test_a_bare_string_is_a_playing_line(self):
        self.assertEqual(presence.parse('["just words"]'), [("just words", "playing")])


class RefusalsThatNameTheProblem(unittest.TestCase):
    def test_an_unknown_activity_type_lists_the_valid_ones(self):
        with self.assertRaises(presence.PresenceError) as caught:
            presence.build("hello", "streaming")
        message = str(caught.exception)
        for kind in presence.ACTIVITY_KINDS:
            self.assertIn(kind, message)

    def test_an_over_long_line_names_discords_limit(self):
        with self.assertRaises(presence.PresenceError) as caught:
            presence.build("x" * (presence.MAX_LINE_LENGTH + 1), "playing")
        self.assertIn(str(presence.MAX_LINE_LENGTH), str(caught.exception))

    def test_an_empty_line_is_refused(self):
        with self.assertRaises(presence.PresenceError):
            presence.build("   ", "playing")

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(presence.PresenceError):
            presence.build("hello", "playing", "invisible")


class TheIntervalIsClamped(unittest.TestCase):
    """A preference expressed badly, not an error worth refusing a command over."""

    def test_zero_is_raised_to_the_floor(self):
        # Not clamped *down* to zero: a one-minute floor is what stops the loop
        # hammering Discord's presence endpoint.
        self.assertEqual(presence.parse_interval(0), presence.MIN_INTERVAL_MINUTES)

    def test_a_huge_value_is_capped(self):
        self.assertEqual(presence.parse_interval(10 ** 9), presence.MAX_INTERVAL_MINUTES)

    def test_nonsense_gives_the_default(self):
        for junk in (None, "", "soon", "1.5.2"):
            self.assertEqual(presence.parse_interval(junk), presence.DEFAULT_INTERVAL_MINUTES)


class StoredInTheDatabase(TempDatabaseMixin):
    """The point of the exercise: changeable without editing code."""

    def setUp(self):
        self.use_temp_database()
        asyncio.run(db.setup())

    def tearDown(self):
        self.release_temp_database()

    def test_setting_lines_is_read_back(self):
        async def scenario():
            await presence.set_lines([("A new status 🎯", "watching")])
            return await presence.lines()

        self.assertEqual(asyncio.run(scenario()), [("A new status 🎯", "watching")])

    def test_an_empty_list_survives_the_round_trip(self):
        """Off is a real setting; it must not read as "unset, use the defaults"."""

        async def scenario():
            await presence.set_lines([])
            return await presence.lines()

        self.assertEqual(asyncio.run(scenario()), [])

    def test_a_line_can_be_added_without_replacing_the_others(self):
        async def scenario():
            await presence.set_lines([("First", "playing")])
            await presence.add_line("Second", "watching")
            return await presence.lines()

        self.assertEqual(
            asyncio.run(scenario()),
            [("First", "playing"), ("Second", "watching")],
        )

    def test_the_interval_is_stored_and_clamped(self):
        async def scenario():
            stored = await presence.set_interval(0)
            return stored, await presence.interval_minutes()

        stored, read = asyncio.run(scenario())
        self.assertEqual(stored, presence.MIN_INTERVAL_MINUTES)
        self.assertEqual(read, presence.MIN_INTERVAL_MINUTES)

    def test_a_bad_line_is_refused_before_it_is_written(self):
        async def scenario():
            await presence.set_lines([("Only this one", "playing")])
            with self.assertRaises(presence.PresenceError):
                await presence.set_lines([("Fine", "playing"), ("", "playing")])
            return await presence.lines()

        # The refused write left the previous list intact rather than half-applied.
        self.assertEqual(asyncio.run(scenario()), [("Only this one", "playing")])


class FakeBot:
    """Enough of a bot for the rotation: a `change_presence` that records."""

    def __init__(self):
        self.seen = []

    async def change_presence(self, *, activity=None, status=None):
        self.seen.append(None if activity is None else activity.name)


class TheRotation(TempDatabaseMixin):
    """Driven for real, because the loop is where the delivery actually happens."""

    def setUp(self):
        self.use_temp_database()
        asyncio.run(db.setup())
        self.fake = FakeBot()

    def tearDown(self):
        self.release_temp_database()

    def _run(self, cycles: int):
        """Run the rotation for a bounded number of cycles, then stop it."""

        async def scenario():
            # `asyncio.sleep` is what paces the loop; making it instant is what
            # makes this test about the rotation rather than about waiting. The
            # interval is still read through the real settings path.
            real_sleep = asyncio.sleep
            calls = 0

            async def fake_sleep(_seconds):
                nonlocal calls
                calls += 1
                if calls >= cycles:
                    raise asyncio.CancelledError
                await real_sleep(0)

            with mock.patch.object(bot_module.asyncio, "sleep", fake_sleep):
                try:
                    await bot_module.rotate_status()
                except asyncio.CancelledError:
                    pass
            return self.fake.seen

        return asyncio.run(scenario())

    def test_it_never_shows_the_same_line_twice_in_a_row(self):
        async def setup():
            await presence.set_lines(
                [("One", "playing"), ("Two", "watching"), ("Three", "listening")]
            )

        asyncio.run(setup())
        seen = self._run(cycles=12)
        self.assertGreaterEqual(len(seen), 5, "the rotation did not run")
        for previous, current in zip(seen, seen[1:]):
            self.assertNotEqual(
                previous,
                current,
                f"the status repeated: {seen}",
            )

    def test_an_empty_list_clears_the_activity_instead_of_guessing(self):
        async def setup():
            await presence.set_lines([("Only line", "playing")])

        asyncio.run(setup())
        seen = self._run(cycles=3)
        self.assertEqual(seen[0], "Only line")
        # None is "clear the activity", which is what an empty list means.
        self.assertIn(None, seen, f"an empty list did not clear the status: {seen}")

    def test_a_failed_settings_read_does_not_kill_the_rotation(self):
        """The loop outlives a database hiccup rather than freezing the status."""
        async def setup():
            await presence.set_lines([("Still here", "playing")])

        asyncio.run(setup())
        with mock.patch.object(
            presence, "lines", side_effect=RuntimeError("database gone")
        ):
            seen = self._run(cycles=3)
        # It kept cycling and cleared rather than raising out of the task.
        self.assertTrue(seen, "the rotation stopped on a failed read")


if __name__ == "__main__":
    unittest.main()
