"""Level math was the source of the "Level 1351 / 2.4B XP" bug: /setlevel and
/givexp wrote unbounded XP and the old lookup looped once per level. These
tests pin the closed-form formula and its inverse."""
import unittest

from utils import levels


class TestLevelMath(unittest.TestCase):
    def _brute_total(self, level: int) -> int:
        # total_xp_for_level(L) is the sum of the per-level costs for 0..L-1.
        return sum(levels.xp_needed_for_level(n) for n in range(0, level))

    def test_total_matches_brute_force_every_level(self):
        for level in range(0, levels.MAX_LEVEL + 1):
            self.assertEqual(
                levels.total_xp_for_level(level),
                self._brute_total(level),
                f"cumulative XP mismatch at level {level}",
            )

    def test_round_trip_is_exact_at_every_boundary(self):
        for level in range(0, levels.MAX_LEVEL + 1):
            total = levels.total_xp_for_level(level)
            got_level, _, _ = levels.calculate_level_from_xp(total)
            self.assertEqual(got_level, level, f"round trip failed at level {level}")

    def test_one_xp_short_stays_on_previous_level(self):
        for level in range(1, levels.MAX_LEVEL + 1):
            total = levels.total_xp_for_level(level) - 1
            got_level, _, _ = levels.calculate_level_from_xp(total)
            self.assertEqual(got_level, level - 1, f"boundary off-by-one at level {level}")

    def test_progress_values_are_in_range(self):
        for level in (0, 1, 7, 99, 400, levels.MAX_LEVEL):
            _, into, needed = levels.calculate_level_from_xp(levels.total_xp_for_level(level))
            self.assertEqual(into, 0)
            self.assertGreaterEqual(needed, 0)

    def test_cap_clamps_absurd_values(self):
        self.assertEqual(levels.clamp_xp(2_407_979_200), levels.MAX_XP)
        self.assertEqual(levels.clamp_xp(-5), 0)
        level, _, _ = levels.calculate_level_from_xp(2_407_979_200)
        self.assertEqual(level, levels.MAX_LEVEL)

    def test_clamp_level(self):
        self.assertEqual(levels.clamp_level(1351), levels.MAX_LEVEL)
        self.assertEqual(levels.clamp_level(-1), 0)
        self.assertEqual(levels.clamp_level(12), 12)

    def test_max_xp_matches_formula(self):
        self.assertEqual(levels.MAX_XP, levels.total_xp_for_level(levels.MAX_LEVEL))

    def test_levels_are_monotonic(self):
        previous = -1
        for level in range(0, 200):
            total = levels.total_xp_for_level(level)
            self.assertGreater(total, previous)
            previous = total


if __name__ == "__main__":
    unittest.main()
