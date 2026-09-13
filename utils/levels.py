"""Shared XP / level math for the leveling system.

This lives in `utils/` rather than in `cogs/leveling.py` so the bot and the
database integrity repair use the *exact* same formula. A mismatch between the
two is how a leaderboard ends up with rows whose stored level disagrees with
their XP.

Formula (unchanged from the original, but now exact and O(1)):

    xp_needed_for_level(L) = 15L^2 + 100L + 250      (L -> L+1)
    total_xp_for_level(L)  = 5L^3 + 42.5L^2 + 202.5L  (0 -> L)

The old implementation summed the series one level at a time in Python. At an
inflated level that meant hundreds of iterations of big-integer math on every
single message; the closed form is a single arithmetic expression.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Level ceiling
# ---------------------------------------------------------------------------
# There is no such thing as an "infinite" level: /setlevel and /givexp
# previously accepted anything, which is how this server ended up with a
# Level 1351 rank-up card and a 2.4-billion-XP leaderboard entry. Every write
# path now clamps to this ceiling, and db.setup() repairs rows written before
# the cap existed. Raise this single constant to change the ceiling.
MAX_LEVEL = 500


def xp_needed_for_level(level: int) -> int:
    """XP required to advance from `level` to `level + 1`."""
    level = max(0, int(level))
    return 15 * (level ** 2) + (100 * level) + 250


def total_xp_for_level(level: int) -> int:
    """Cumulative XP needed to go from level 0 all the way to `level`."""
    level = max(0, int(level))
    if level == 0:
        return 0
    n = level - 1
    sum_sq = n * (n + 1) * (2 * n + 1) // 6   # sum of i^2 for i = 0..n
    sum_i = n * (n + 1) // 2                  # sum of i   for i = 0..n
    return 15 * sum_sq + 100 * sum_i + 250 * level


#: Hard ceiling on stored XP, derived from the level cap.
MAX_XP = total_xp_for_level(MAX_LEVEL)


def calculate_level_from_xp(total_xp: int) -> tuple[int, int, int]:
    """Return ``(level, xp_into_level, xp_needed_for_next)`` for a total XP.

    Uses a closed-form estimate followed by an exact integer correction, so it
    is O(1) instead of looping once per level. Negative or absurd input is
    clamped rather than trusted.
    """
    total_xp = clamp_xp(total_xp)

    if total_xp <= 0:
        return 0, 0, xp_needed_for_level(0)

    # Estimate from the leading term (5 * L^3) then walk to the exact level.
    level = int((total_xp / 5.0) ** (1.0 / 3.0))
    level = max(0, min(level, MAX_LEVEL))

    while level < MAX_LEVEL and total_xp_for_level(level + 1) <= total_xp:
        level += 1
    while level > 0 and total_xp_for_level(level) > total_xp:
        level -= 1

    return level, total_xp - total_xp_for_level(level), xp_needed_for_level(level)


def clamp_xp(total_xp: int) -> int:
    """Clamp a raw XP value into the valid ``0..MAX_XP`` range."""
    try:
        value = int(total_xp)
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, MAX_XP))


def clamp_level(level: int) -> int:
    """Clamp a raw level value into the valid ``0..MAX_LEVEL`` range."""
    try:
        value = int(level)
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, MAX_LEVEL))
