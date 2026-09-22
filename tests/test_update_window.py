"""A deploy's release notes arrive in the small hours, not when somebody pushed.

The complaint this answers is one sentence long: the bot talks about itself in a
channel it is in, on every deploy, and a deploy at 14:20 is a note at 14:20. The
answer is a clock — ``UPDATE_WINDOW_IST``, three times a few minutes apart — and
the property that makes it safe is that a held note is *owed* rather than
dropped: nothing is recorded until Discord has accepted it, so the digest that
goes out at 03:33 is everything since that server's last one.

The clock is tested by handing it moments rather than by asking it what time it
is, because a test that reads the wall clock passes or fails depending on when
the suite is run — and this is the one feature in the tree whose whole subject is
the time of day.
"""
import ast
import datetime
import os
import unittest
from pathlib import Path
from unittest import mock

import bot as bot_module
from utils import config_audit, version

ROOT = Path(__file__).resolve().parent.parent
BOT_PY = ROOT / "bot.py"
KEEP_ALIVE = ROOT / "keep_alive.py"
BLUEPRINT = ROOT / "render.yaml"

#: The three times the operator asked for, and the reason there are three: a free
#: instance can be asleep at the first.
EXPECTED_WINDOWS = ("03:33", "04:00", "04:44")


def _at(text: str) -> datetime.datetime:
    """``"2026-09-22 03:33"`` as an aware IST moment."""
    naive = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=version.IST)


def _sources():
    return BOT_PY.read_text(errors="replace"), KEEP_ALIVE.read_text(errors="replace")


def _functions(tree, name):
    return [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]


def _named_calls(node, name):
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


class _WindowEnv(unittest.TestCase):
    """A test class that can pretend the host was configured a certain way."""

    def setting(self, value):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        if value is None:
            os.environ.pop(version.WINDOW_ENV, None)
        else:
            os.environ[version.WINDOW_ENV] = value


class TestReadingTheWindow(_WindowEnv):
    """The setting, spelled every way a deployment can spell it."""

    def test_unset_is_the_three_times_it_was_asked_for(self):
        self.setting(None)
        self.assertEqual([f"{t:%H:%M}" for t in version.windows()], list(EXPECTED_WINDOWS))

    def test_a_deployment_may_choose_its_own(self):
        self.setting("22:15, 06:05")
        self.assertEqual([f"{t:%H:%M}" for t in version.windows()], ["06:05", "22:15"])

    def test_duplicates_collapse(self):
        self.setting("03:33,03:33,04:00")
        self.assertEqual(len(version.windows()), 2)

    def test_always_means_the_boot_announces_as_it_always_did(self):
        """The escape hatch: no window at all, which is the old behaviour."""
        self.setting("always")
        self.assertEqual(version.windows(), ())
        self.assertTrue(version.window_is_open(_at("2026-09-22 14:20")))

    def test_an_unreadable_value_is_the_default_and_never_always(self):
        """A typo must not turn the quiet window into "post whenever"."""
        self.setting("half past three")
        self.assertEqual([f"{t:%H:%M}" for t in version.windows()], list(EXPECTED_WINDOWS))

    def test_an_impossible_time_is_dropped(self):
        self.setting("03:33,25:00,04:99")
        self.assertEqual([f"{t:%H:%M}" for t in version.windows()], ["03:33"])

    def test_the_setting_is_declared_as_tuning_and_notable(self):
        setting = next(s for s in config_audit.SETTINGS if s.name == version.WINDOW_ENV)
        self.assertEqual(setting.kind, config_audit.TUNING)
        # Not *notable*: nothing is switched off without it, so the suite's own
        # rules do not demand it in `.env.example` — but the value it has here is
        # what the host actually runs, so the blueprint declares it.
        self.assertFalse(setting.notable)

    def test_the_host_declares_the_window_it_runs(self):
        """The deploy's own state, in the deploy's own file.

        A switch left implicit on the host is one nobody can read: this project's
        recurring defect is a setting the code reads and the manifest never
        offers, so the behaviour on the host is a default rather than a decision.
        """
        declared = dict(
            (match.group(1), match.group(2))
            for match in __import__("re").finditer(
                r"^\s*- key:\s*([A-Z][A-Z0-9_]*)\s*\n\s*value:\s*\"([^\"]*)\"",
                BLUEPRINT.read_text(errors="replace"),
                flags=__import__("re").MULTILINE,
            )
        )
        self.assertEqual(declared.get(version.WINDOW_ENV), ",".join(EXPECTED_WINDOWS))


class TestTheClock(_WindowEnv):
    """Open, closed, and where the next one is."""

    def setUp(self):
        self.setting(None)

    def test_inside_the_first_window(self):
        self.assertTrue(version.window_is_open(_at("2026-09-22 03:33")))
        self.assertTrue(version.window_is_open(_at("2026-09-22 03:50")))

    def test_the_later_windows_are_windows_too(self):
        """The whole point of three: a host asleep at 03:33 still has 04:00."""
        self.assertTrue(version.window_is_open(_at("2026-09-22 04:00")))
        self.assertTrue(version.window_is_open(_at("2026-09-22 04:44")))

    def test_outside_every_window(self):
        for moment in ("2026-09-22 03:20", "2026-09-22 05:20", "2026-09-22 12:00",
                       "2026-09-22 23:59"):
            with self.subTest(moment=moment):
                self.assertFalse(version.window_is_open(_at(moment)))

    def test_a_window_near_midnight_is_still_open_after_it(self):
        """``23:50`` opens a window that ends at 00:20, on the other date."""
        self.setting("23:50")
        self.assertTrue(version.window_is_open(_at("2026-09-22 23:55")))
        self.assertTrue(version.window_is_open(_at("2026-09-23 00:10")))
        self.assertFalse(version.window_is_open(_at("2026-09-23 00:40")))

    def test_a_window_is_wide_enough_for_a_slow_wake_up(self):
        """The monitor keeps the free host awake; the wake-up is not instant."""
        self.setting("03:33")
        self.assertTrue(version.window_is_open(_at("2026-09-22 03:59")))
        self.assertFalse(version.window_is_open(_at("2026-09-22 04:04")))

    def test_the_next_window_is_todays_when_today_has_one_left(self):
        target = version.next_window(_at("2026-09-22 02:00"))
        self.assertEqual(target, _at("2026-09-22 03:33"))

    def test_a_missed_day_waits_for_tomorrow(self):
        """All three gone is not a reason to post at noon."""
        target = version.next_window(_at("2026-09-22 08:05"))
        self.assertEqual(target, _at("2026-09-23 03:33"))

    def test_an_open_window_is_its_own_answer(self):
        self.assertEqual(version.next_window(_at("2026-09-22 03:40")), _at("2026-09-22 03:40"))

    def test_describe_next_names_the_time_the_day_and_the_distance(self):
        text = version.describe_next(_at("2026-09-22 08:05"))
        self.assertEqual(text, "03:33 IST tomorrow (in 19h 28m)")

    def test_describe_next_while_open_says_so(self):
        self.assertIn("now", version.describe_next(_at("2026-09-22 03:40")))

    def test_describe_next_without_a_window(self):
        self.setting("always")
        self.assertIn("no quiet window", version.describe_next(_at("2026-09-22 08:05")))

    def test_the_stamp_is_ist_on_the_nose(self):
        self.assertEqual(version.stamp(_at("2026-09-22 03:33").timestamp()),
                         "22 Sep 2026, 03:33 IST")
        self.assertEqual(version.IST.utcoffset(None), datetime.timedelta(hours=5, minutes=30))


class TestABootOutsideTheWindow(unittest.IsolatedAsyncioTestCase):
    """What the boot does at the wrong hour: nothing, and it owes the note."""

    def setUp(self):
        self.sent = mock.AsyncMock()
        self.fake_bot = mock.Mock(guilds=[mock.Mock(name="guild")])
        self.window = mock.patch.object(version, "window_is_open", return_value=False)
        self.window.start()
        self.addCleanup(self.window.stop)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("SILENT_UPDATES", None)

    async def _boot(self):
        with mock.patch.object(bot_module, "bot", self.fake_bot), mock.patch.object(
            bot_module, "announce_release_notes_to_guild", self.sent
        ):
            await bot_module._announce_release_notes()

    async def test_a_held_note_is_not_sent(self):
        await self._boot()
        self.sent.assert_not_awaited()

    async def test_holding_records_nothing(self):
        """The difference between a delay and a loss.

        ``announce_release_notes_to_guild`` is what writes a server's
        "already told" record, and it must not be reached at all: a note marked
        sent by a boot that never sent it is how an update goes missing forever.
        """
        with mock.patch.object(version, "set_last_hash") as stored:
            await self._boot()
        stored.assert_not_called()

    async def test_an_open_window_sends_the_note(self):
        self.window.stop()
        with mock.patch.object(version, "window_is_open", return_value=True):
            await self._boot()
        self.assertEqual(self.sent.await_count, 1)

    async def test_the_silence_switch_still_beats_an_open_window(self):
        """``SILENT_UPDATES=1`` is the other end: not "later", but "never"."""
        self.window.stop()
        os.environ["SILENT_UPDATES"] = "1"
        with mock.patch.object(version, "window_is_open", return_value=True):
            await self._boot()
        self.sent.assert_not_awaited()


class TestTheLoopThatDeliversIt(unittest.TestCase):
    """The half that does something at 03:33, since no boot occurs there."""

    def setUp(self):
        self.bot_src, self.keep_src = _sources()

    def test_the_boot_starts_it(self):
        tree = ast.parse(self.bot_src)
        on_ready = _functions(tree, "on_ready")
        self.assertEqual(len(on_ready), 1)
        self.assertTrue(
            _named_calls(on_ready[0], "start_release_window"),
            "nothing starts the quiet-window loop, so a held note is never posted",
        )

    def test_the_loop_exists_and_sleeps_on_the_clock(self):
        tree = ast.parse(self.keep_src)
        self.assertTrue(_functions(tree, "release_window_loop"))
        self.assertTrue(_functions(tree, "start_release_window"))

    def test_the_loop_asks_the_clock_the_same_way_the_boot_does(self):
        """One rule, two callers — a second copy would drift from the first."""
        self.assertIn("version.window_is_open()", self.keep_src)

    def test_the_loop_goes_through_the_client_rather_than_a_second_copy(self):
        """``bot.py`` owns the silence check and the per-server records.

        The loop reaches the same function through the client, because
        ``bot.py`` is ``__main__`` in production and importing it by name would
        build a second bot. A loop with its own copy of the announcement would be
        a second way to post a note that the first one cannot see.
        """
        self.assertIn("announce_release_notes", self.keep_src)
        self.assertNotIn("def _announce_release_notes", self.keep_src)
        self.assertIn("bot.announce_release_notes = _announce_release_notes", self.bot_src)

    def test_the_loop_does_not_hammer_a_channel_that_refused(self):
        """A window is thirty minutes wide; a retry every thirty seconds is not."""
        self.assertIn("RELEASE_WINDOW_RETRY_SECONDS", self.keep_src)
        self.assertGreater(
            int(__import__("re").search(
                r"RELEASE_WINDOW_RETRY_SECONDS = (\d+)", self.keep_src
            ).group(1)),
            int(__import__("re").search(
                r"RELEASE_WINDOW_TICK_SECONDS = (\d+)", self.keep_src
            ).group(1)),
        )


if __name__ == "__main__":
    unittest.main()
