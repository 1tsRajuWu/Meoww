"""The Dev Cat: the rules behind a pet that lives in the developer's own server.

One system, three surfaces, and this module is the whole of the first two:
the cat that drifts and needs feeding, the solo arcade it plays with you, and the
small decisions about when it is allowed to bother you on its own. The Discord
half — the commands, the buttons, the gateway — lives in ``cogs/devtoy.py``, and
the split is the one ``utils/company.py`` and ``utils/alonetime.py`` already use:
every rule worth arguing about is testable without a gateway, and the half that
needs Discord can only be tested by using it.

**It is one server's, and that is a decision rather than a default.** The gate is
:func:`in_dev_server`, backed by :data:`utils.bot_channels.DEV_SERVER_GUILD_ID` —
the single place a server id is allowed to be written into this tree, guarded by a
scan in ``tests/test_devtoy.py`` that fails if the digits appear anywhere else.
Every other server gets a refusal, and no module switch can widen it. The registry
still ships the system **off** (see ``Feature.enabled_by_default``), because a
per-server switch is the only lever Discord offers for hiding a slash command in
one server — so the toy is invisible until it is switched on *in that server*, and
refused even if it is switched on somewhere else.

Three rules the numbers encode, each one a way a pet feature becomes a nuisance:

* **A cat is a function of the clock, not a loop.** Mood, food and energy are
  derived from ``now - updated_at`` on every read, so nothing ticks, nothing
  schedules a write, and a month of neglect is one subtraction. Every dial is
  clamped, so a cat nobody visited for a year is *miserable*, never negative.
* **Care is rate-limited, and the limits are the point** — petting is cheap and
  frequent, feeding is the one with a real clock on it, playing costs energy the
  cat has to sleep back.
* **The cat bothers nobody it was not invited to bother.** The ambient rules cap
  the unprompted cards (one per :data:`NUDGE_MIN_GAP`, :data:`NUDGE_DAILY_CAP` a
  day), the chat rules cap the replies separately from the reactions, and every
  line this module can say out loud is written without a mention in it — a card
  that pings is a card that wakes somebody up, which is exactly what a toy must
  not do.
"""
from __future__ import annotations

import datetime
import random
import re
from dataclasses import dataclass, replace
from typing import Iterable

from utils import bot_channels

# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
MINUTE = 60.0
HOUR = 60.0 * MINUTE
DAY = 24.0 * HOUR

#: The clock the *day* boundary is read on. IST because that is the operator's, and
#: the same reason ``UPDATE_WINDOW_IST`` exists: "today" has to mean the day the
#: person reading the card is having, or a streak breaks at lunchtime.
IST_OFFSET_SECONDS = 5.5 * HOUR


def care_day(at: float) -> str:
    """The ``YYYY-MM-DD`` day a timestamp falls on, in IST."""
    moment = datetime.datetime.fromtimestamp(
        float(at or 0.0), tz=datetime.timezone.utc
    ) + datetime.timedelta(seconds=IST_OFFSET_SECONDS)
    return moment.date().isoformat()


def _previous_day(day: str) -> str:
    """The day before ``day``, or ``""`` when it is not readable."""
    try:
        return (datetime.date.fromisoformat(day) - datetime.timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        return ""


def humanize(seconds: float) -> str:
    """``5400`` → ``"1h 30m"``. Seconds only when there are no minutes left."""
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def in_dev_server(guild_id) -> bool:
    """Whether a guild id is the developer's own server — the only one this runs in."""
    try:
        return int(guild_id) == int(bot_channels.DEV_SERVER_GUILD_ID)
    except (TypeError, ValueError):
        return False


def is_dev_channel(channel_id) -> bool:
    """Whether a channel id is the room the cat's own cards were pointed at."""
    try:
        return int(channel_id) == int(bot_channels.DEV_CHAT_CHANNEL_ID)
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# The cat
# --------------------------------------------------------------------------- #
DIAL_MIN = 0.0
DIAL_MAX = 100.0

DEFAULT_NAME = "Meow"
NAME_LIMIT = 24

#: How fast each dial moves while nobody is looking, per hour: food runs out
#: first (a cat that eats once a day stays full), mood next, and energy comes
#: *back* — a cat alone is a cat asleep.
FED_PER_HOUR = 7.0
MOOD_PER_HOUR = 3.0
REST_PER_HOUR = 5.0

#: A cat that tired cannot be asked to play, and neither can a starving one.
PLAY_MIN_ENERGY = 20.0
PLAY_MIN_FED = 15.0

#: Care score → level. Deliberately shallow: this is a pet, not a progression
#: system, and a number that climbs forever stops meaning anything.
CARE_PER_LEVEL = 30
MAX_LEVEL = 20


@dataclass(frozen=True)
class Cat:
    """One member's cat, exactly as it is stored."""

    name: str = DEFAULT_NAME
    fed: float = 60.0
    mood: float = 70.0
    energy: float = 80.0
    adopted_at: float = 0.0
    updated_at: float = 0.0
    last_fed: float = 0.0
    last_pet: float = 0.0
    last_play: float = 0.0
    care_days: int = 0
    last_care_day: str = ""
    care_score: int = 0
    games: int = 0


@dataclass(frozen=True)
class Action:
    """One thing a person can do to a cat, with what it costs and what it gives."""

    id: str
    label: str
    done_word: str
    cooldown: float
    fed: float = 0.0
    mood: float = 0.0
    energy: float = 0.0
    score: int = 1


ACTIONS: tuple[Action, ...] = (
    Action("feed", "🍖 Feed", "fed", cooldown=2 * HOUR, fed=55.0, mood=6.0, score=2),
    Action("pet", "🐾 Pet", "petted", cooldown=15 * MINUTE, mood=22.0, score=1),
    Action(
        "play", "🎾 Play", "played with", cooldown=30 * MINUTE,
        mood=28.0, energy=-25.0, fed=-6.0, score=3,
    ),
)
ACTIONS_BY_ID: dict[str, Action] = {action.id: action for action in ACTIONS}
#: Stored column names live here rather than being built with an f-string at two
#: call sites: a stored column is a schema decision, and the reader below has to
#: agree with the writer about its name.
STAMP_FIELD: dict[str, str] = {
    "feed": "last_fed", "pet": "last_pet", "play": "last_play",
}


def _clamp(value: float) -> float:
    return max(DIAL_MIN, min(DIAL_MAX, float(value)))


def drift(cat: Cat, now: float) -> Cat:
    """The cat as it is *now*, after the hours since anybody last looked.

    Pure and total: any clock, any row, no exception. A clock that moved backwards
    (an NTP step, a restored backup) is treated as no time passing rather than as
    negative decay, which would otherwise hand out food for free.
    """
    if float(cat.updated_at) <= 0:
        # A row that has never been touched starts the clock now rather than at
        # the epoch, which would otherwise read as fifty years of neglect.
        return replace(cat, updated_at=float(now))
    elapsed = (float(now) - float(cat.updated_at)) / HOUR
    if elapsed <= 0:
        return cat
    return replace(
        cat,
        fed=_clamp(cat.fed - FED_PER_HOUR * elapsed),
        mood=_clamp(cat.mood - MOOD_PER_HOUR * elapsed),
        energy=_clamp(cat.energy + REST_PER_HOUR * elapsed),
        updated_at=float(now),
    )


def cooldown_left(cat: Cat, action_id: str, now: float) -> float:
    """Seconds before ``action_id`` is allowed again (``0`` when it may be now)."""
    action = ACTIONS_BY_ID.get(action_id)
    if action is None:
        raise ValueError(f"unknown action: {action_id!r}")
    last = float(getattr(cat, STAMP_FIELD[action.id], 0.0) or 0.0)
    return max(0.0, last + action.cooldown - float(now))


@dataclass(frozen=True)
class CareResult:
    """What one care action did, or the sentence saying why it did nothing."""

    ok: bool
    cat: Cat
    headline: str
    detail: str
    remaining: float = 0.0


def care(cat: Cat, action_id: str, now: float, *, rng: random.Random | None = None) -> CareResult:
    """Do one action to ``cat``, or refuse it — with the cat either way.

    Refusals return the **drifted** cat, so a call that only reads the reason does
    not also have to do the arithmetic: what comes back is what the row should say.
    """
    action = ACTIONS_BY_ID.get(action_id)
    if action is None:
        raise ValueError(f"unknown action: {action_id!r}")
    stale = drift(cat, now)

    left = cooldown_left(stale, action.id, now)
    if left > 0:
        return CareResult(
            False, stale, "Not yet",
            f"{_name(stale)} was just {action.done_word}. Give it {humanize(left)}.",
            remaining=left,
        )

    if action.id == "play":
        if stale.energy < PLAY_MIN_ENERGY:
            return CareResult(
                False, stale, "Too sleepy",
                f"{_name(stale)} is napping. Let it rest for "
                f"{humanize((PLAY_MIN_ENERGY - stale.energy) / REST_PER_HOUR * HOUR)}.",
            )
        if stale.fed < PLAY_MIN_FED:
            return CareResult(
                False, stale, "Too hungry",
                f"{_name(stale)} will not chase anything on an empty stomach.",
            )

    day = care_day(now)
    days, last_day = streak_after(stale.last_care_day, stale.care_days, day)
    changes: dict[str, object] = {
        "fed": _clamp(stale.fed + action.fed),
        "mood": _clamp(stale.mood + action.mood),
        "energy": _clamp(stale.energy + action.energy),
        "updated_at": float(now),
        STAMP_FIELD[action.id]: float(now),
        "care_days": days,
        "last_care_day": last_day,
        "care_score": max(0, int(stale.care_score) + action.score),
        "adopted_at": stale.adopted_at or float(now),
    }
    fresh = replace(stale, **changes)
    head, body = care_line(action.id, fresh, now, rng=rng)
    return CareResult(True, fresh, head, body)


def _name(cat: Cat) -> str:
    return cat.name or DEFAULT_NAME


def adopt(now: float, name: str = "") -> Cat:
    """A brand-new cat, already happy and already fed.

    Adopting is the first act of care, so the streak starts at one rather than at
    nothing: a card that says ``0 day(s)`` to somebody who just took a cat home is
    the whole feature arguing with itself.
    """
    return Cat(
        name=clean_name(name),
        fed=75.0,
        mood=85.0,
        energy=95.0,
        adopted_at=float(now),
        updated_at=float(now),
        care_days=1,
        last_care_day=care_day(now),
    )


def streak_after(last_day: str, days: int, today: str) -> tuple[int, str]:
    """The streak a care action on ``today`` leaves behind.

    Same day: unchanged (a streak counts *days*, not actions). Yesterday: one more.
    Anything else — a gap, a first visit, an unreadable row: today starts a run of
    one, which is the honest answer rather than carrying a broken streak forward.
    """
    if not today:
        return max(1, int(days or 0)), last_day
    if last_day == today:
        return max(1, int(days or 0)), today
    if last_day and last_day == _previous_day(today):
        return max(1, int(days or 0)) + 1, today
    return 1, today


def care_level(cat: Cat) -> int:
    """1–20, from the care score."""
    return min(MAX_LEVEL, 1 + max(0, int(cat.care_score)) // CARE_PER_LEVEL)


def mood_word(cat: Cat) -> str:
    if cat.fed <= 5:
        return "starving"
    if cat.mood >= 80:
        return "purring"
    if cat.mood >= 60:
        return "content"
    if cat.mood >= 40:
        return "restless"
    if cat.mood >= 20:
        return "sulking"
    return "miserable"


def face(cat: Cat) -> str:
    """The one glyph that says how the cat is, for a title or a button."""
    if cat.fed <= 5:
        return "🙀"
    if cat.mood >= 80 and cat.energy >= 40:
        return "😻"
    if cat.mood >= 55:
        return "😺"
    if cat.mood >= 30:
        return "🐈"
    if cat.mood >= 15:
        return "😿"
    return "😾"


def bar(value: float, *, width: int = 10) -> str:
    """A plain ``████░░`` bar. Local rather than ``utils.style``'s, because that
    one imports Discord and this module is deliberately free of it."""
    filled = int(round(max(0.0, min(1.0, float(value) / DIAL_MAX)) * width))
    return "█" * filled + "░" * (width - filled)


def badges(cat: Cat, now: float) -> list[str]:
    """The small honours a cat earns, in the order the card shows them."""
    earned: list[str] = []
    if cat.care_days >= 30:
        earned.append("🏆 30 days running")
    elif cat.care_days >= 7:
        earned.append("🔥 A week without missing a day")
    elif cat.care_days >= 3:
        earned.append("🧡 Three days in a row")
    if cat.care_score >= 300:
        earned.append("👑 Devoted")
    if cat.games >= 10:
        earned.append(f"🎮 Arcade regular ({cat.games} games)")
    if cat.adopted_at and (float(now) - cat.adopted_at) >= 30 * DAY:
        earned.append("📅 A month together")
    if cat.fed >= 95 and cat.mood >= 95:
        earned.append("✨ Perfectly spoiled")
    return earned


def status_fields(cat: Cat, now: float) -> list[tuple[str, str]]:
    """The card's rows: name, level and the three dials."""
    fresh = drift(cat, now)
    rows = [
        ("Cat", f"{face(fresh)} **{_name(fresh)}** · {mood_word(fresh)}"),
        ("Level", f"**{care_level(fresh)}** · {fresh.care_score} care"),
        ("🍖 Food", f"{bar(fresh.fed)} {int(fresh.fed)}%"),
        ("💛 Mood", f"{bar(fresh.mood)} {int(fresh.mood)}%"),
        ("⚡ Energy", f"{bar(fresh.energy)} {int(fresh.energy)}%"),
        ("🔥 Streak", f"{fresh.care_days} day(s)"),
    ]
    badges_text = badges(fresh, now)
    if badges_text:
        rows.append(("Badges", " · ".join(badges_text)))
    return rows


def panel_body(cat: Cat, now: float) -> str:
    """The one-line face of the persistent panel."""
    fresh = drift(cat, now)
    return (
        f"{face(fresh)} **{_name(fresh)}** — {mood_word(fresh)}\n"
        f"{bar(fresh.fed)} 🍖 {int(fresh.fed)}%   "
        f"{bar(fresh.mood)} 💛 {int(fresh.mood)}%   "
        f"{bar(fresh.energy)} ⚡ {int(fresh.energy)}%"
    )


# --------------------------------------------------------------------------- #
# What the cat says
# --------------------------------------------------------------------------- #
FEED_LINES: tuple[str, ...] = (
    "{name} inhales the whole bowl before you finish putting it down.",
    "{name} sniffs it, decides it will do, and eats like a tiny royal.",
    "{name} headbutts your hand out of the way and goes straight for the bowl.",
    "{name} eats, then looks up as if to ask what else there is.",
)
PET_LINES: tuple[str, ...] = (
    "{name} leans the whole weight of their head into your hand.",
    "{name} purrs so hard the desk hums.",
    "{name} closes both eyes and lets you have the one second of trust.",
    "You get three pets in before {name} decides that is enough and bites you gently.",
)
PLAY_LINES: tuple[str, ...] = (
    "{name} stalks the laser like it is the first cat to ever notice one.",
    "{name} catches it, looks betrayed, and demands you do it again.",
    "{name} sprints a full circle and slides into the wall. Worth it.",
    "{name} wins the duel and sits on the toy as a trophy.",
)
LINES_BY_ACTION: dict[str, tuple[str, ...]] = {
    "feed": FEED_LINES, "pet": PET_LINES, "play": PLAY_LINES,
}

#: Cards a card may not carry. Every string this module offers for a message is
#: asserted mention-free by the suite, because a toy that pings somebody at random
#: is the exact failure the ambient caps exist to prevent.
NO_MENTION = ("@everyone", "@here")


def care_line(action_id: str, cat: Cat, now: float, *, rng: random.Random | None = None) -> tuple[str, str]:
    """``(headline, body)`` for a completed care action."""
    pool = LINES_BY_ACTION.get(action_id, PET_LINES)
    line = pick(pool, rng).format(name=_name(cat))
    return f"{face(cat)} {mood_word(cat).capitalize()}", line


def pick(pool: Iterable[str], rng: random.Random | None = None) -> str:
    items = tuple(pool)
    if not items:
        return ""
    return (rng or random).choice(items)


# --------------------------------------------------------------------------- #
# The solo arcade
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Game:
    """One way to be bored, and how its record reads."""

    id: str
    label: str
    blurb: str
    record: str
    lower_is_better: bool
    unit: str = ""


GAMES: tuple[Game, ...] = (
    Game("guess", "🎯 Guess my number", "I pick 1–100. Fewest tries wins.", "fewest tries", True, "tries"),
    Game("reaction", "⚡ Paw reflex", "A paw appears — tap it fast.", "quickest paw", True, "ms"),
    Game("scramble", "🔤 Word scramble", "Unscramble my word against the clock.", "fastest solve", True, "s"),
    Game("rps", "✊ Cat duel", "Best of five against me. I cheat a little.", "longest run", False, "wins"),
)
GAMES_BY_ID: dict[str, Game] = {game.id: game for game in GAMES}

#: The hub's non-game buttons: the three things that need no scoring.
EXTRA_ACTIVITIES: tuple[str, ...] = ("challenge", "riddle", "joke")
ACTIVITY_IDS: tuple[str, ...] = tuple(game.id for game in GAMES) + EXTRA_ACTIVITIES


def roll_activity(rng: random.Random | None = None) -> str:
    """One random thing to do, for the 🤷 button."""
    return pick(ACTIVITY_IDS, rng)


# -- guess my number -------------------------------------------------------- #
GUESS_MIN, GUESS_MAX = 1, 100
#: Seven is the information-theoretic answer for 1–100 (⌈log₂101⌉); it is the par
#: the cat mentions rather than a rule.
GUESS_PAR = 7


def guess_secret(rng: random.Random | None = None) -> int:
    return (rng or random).randint(GUESS_MIN, GUESS_MAX)


def guess_verdict(secret: int, guess: int) -> str:
    """``"higher"``, ``"lower"`` or ``"hit"`` — the cat's only honest answer."""
    if int(guess) == int(secret):
        return "hit"
    return "higher" if int(secret) > int(guess) else "lower"


def guess_comment(tries: int) -> str:
    if tries <= GUESS_PAR:
        return f"Found it in **{tries}** — at or under par ({GUESS_PAR})."
    return f"Found it in **{tries}** (par is {GUESS_PAR})."


# -- word scramble ---------------------------------------------------------- #
SCRAMBLE_WORDS: tuple[str, ...] = (
    "whiskers", "kitten", "nap", "laser", "purr", "treat", "paw", "cushion",
    "meow", "catnip", "fluffy", "tail", "belly", "snooze", "pounce", "milk",
)


def scramble(word: str, rng: random.Random | None = None) -> str:
    """``word`` with its letters shuffled — and **never** the same as the word.

    A scramble that hands the answer back is the one outcome that makes the game
    unwinnable in the wrong direction, so it is re-rolled rather than returned.
    """
    chooser = rng or random
    text = str(word or "")
    if len(text) < 2:
        return text
    shuffled = text
    for _ in range(12):
        letters = list(text)
        chooser.shuffle(letters)
        shuffled = "".join(letters)
        if shuffled.lower() != text.lower():
            break
    return shuffled


def scramble_round(rng: random.Random | None = None) -> tuple[str, str]:
    """``(answer, scrambled)`` for one round."""
    word = pick(SCRAMBLE_WORDS, rng)
    return word, scramble(word, rng)


# -- paw reflex ------------------------------------------------------------- #
REFLEX_BANDS: tuple[tuple[float, str], ...] = (
    (180.0, "You have the reflexes of a cat that has never once missed a meal."),
    (260.0, "Quick. Suspiciously quick, for somebody who was just bored."),
    (400.0, "Respectable. The cat is not impressed, but the cat is rarely impressed."),
    (650.0, "Were you reading something else? Be honest."),
)


def reflex_comment(ms: float) -> str:
    for ceiling, line in REFLEX_BANDS:
        if float(ms) <= ceiling:
            return line
    return "The paw is still up. Take your time."


# -- cat duel --------------------------------------------------------------- #
RPS_MOVES: tuple[str, str, str] = ("✊", "✋", "✌️")
DUEL_WINS_NEEDED = 3


def rps_verdict(player: int, cat: int) -> str:
    """``"you"``, ``"cat"`` or ``"tie"`` for two move indexes."""
    player, cat = int(player) % 3, int(cat) % 3
    if player == cat:
        return "tie"
    return "you" if (player - cat) % 3 == 1 else "cat"


DUEL_LINES: dict[str, tuple[str, ...]] = {
    "you": ("Ow. Fine, that one is yours.", "Lucky. Do it again.", "I was thinking of a different paw."),
    "cat": ("Told you.", "The cat does not lose, the cat merely allows.", "Skill. It is a cat thing."),
    "tie": ("A tie. Rude of you.", "Again.", "Same again, then."),
}


def duel_line(verdict: str, rng: random.Random | None = None) -> str:
    return pick(DUEL_LINES.get(verdict, DUEL_LINES["tie"]), rng)


# -- records ---------------------------------------------------------------- #
def record_better(game_id: str, value: float, best: float | None) -> bool:
    """Whether ``value`` beats a stored ``best`` (``None`` = no record yet).

    One function because the direction is per game — fewest tries and quickest paw
    are *lower*, longest run is higher — and a cog that had that backwards would
    congratulate somebody for their worst game.
    """
    game = GAMES_BY_ID.get(game_id)
    if game is None:
        raise ValueError(f"unknown game: {game_id!r}")
    if best is None or float(best) <= 0:
        return True
    return float(value) < float(best) if game.lower_is_better else float(value) > float(best)


def record_text(game_id: str, value: float | None) -> str:
    """How one number reads on a card (``—`` when there is no record yet)."""
    game = GAMES_BY_ID.get(game_id)
    if game is None:
        raise ValueError(f"unknown game: {game_id!r}")
    if value is None or float(value) <= 0:
        return "—"
    if game.id == "reaction":
        return f"{int(round(float(value)))} ms"
    if game.id == "scramble":
        return f"{float(value):.1f} s"
    return f"{int(round(float(value)))} {game.unit}".strip()


# --------------------------------------------------------------------------- #
# The cat's own cards
# --------------------------------------------------------------------------- #
#: One unprompted card every four hours at most, three a day. Both numbers are
#: here rather than in the cog because they are the difference between a pet and
#: a spammer, and a test drives them directly.
NUDGE_MIN_GAP = 4 * HOUR
NUDGE_DAILY_CAP = 3
NUDGE_KINDS: tuple[str, ...] = ("checkin", "mischief", "riddle", "challenge")


@dataclass(frozen=True)
class AmbientState:
    """The bookkeeping row that keeps the cat quiet."""

    last_nudge: float = 0.0
    nudge_day: str = ""
    nudge_count: int = 0


@dataclass(frozen=True)
class NudgeDecision:
    """Whether the cat may speak on its own, and which kind it would be."""

    ok: bool
    kind: str = ""
    reason: str = ""


def nudge_due(
    state: AmbientState,
    now: float,
    *,
    enabled: bool,
    rng: random.Random | None = None,
) -> NudgeDecision:
    """Whether an unprompted card is allowed *right now*, and why not when it is not."""
    if not enabled:
        return NudgeDecision(False, reason="nudges are switched off")
    day = care_day(now)
    count = int(state.nudge_count) if state.nudge_day == day else 0
    since = float(now) - float(state.last_nudge or 0.0)
    if state.last_nudge and since < NUDGE_MIN_GAP:
        return NudgeDecision(False, reason=f"quiet for another {humanize(NUDGE_MIN_GAP - since)}")
    if count >= NUDGE_DAILY_CAP:
        return NudgeDecision(False, reason=f"already {NUDGE_DAILY_CAP} today")
    kind = pick(NUDGE_KINDS, rng) if rng else NUDGE_KINDS[int(now // NUDGE_MIN_GAP) % len(NUDGE_KINDS)]
    return NudgeDecision(True, kind=kind)


def mark_nudge(state: AmbientState, now: float) -> AmbientState:
    """The state after one card went out, with the day rollover handled."""
    day = care_day(now)
    count = int(state.nudge_count) if state.nudge_day == day else 0
    return AmbientState(last_nudge=float(now), nudge_day=day, nudge_count=count + 1)


def nudges_today(state: AmbientState, now: float) -> int:
    return int(state.nudge_count) if state.nudge_day == care_day(now) else 0


#: The cat's own voice, for the four kinds of card it sends without being asked.
AMBIENT_OPENERS: dict[str, tuple[str, ...]] = {
    "checkin": (
        "Your chair is empty and the cat has noticed.",
        "Nobody has typed anything here in a while.",
        "The cat has been sitting in the doorway for a bit.",
    ),
    "mischief": (
        "Something fell over. It was probably already like that.",
        "The cat has knocked the pen off the desk again.",
        "There is a cat on the keyboard now. Nothing was lost.",
    ),
    "riddle": (
        "A riddle, since it is quiet.",
        "The cat thought of something.",
    ),
    "challenge": (
        "One small challenge, while nothing is happening.",
        "The cat has made up a rule.",
    ),
}


def ambient_text(kind: str, cat: Cat, now: float, *, rng: random.Random | None = None) -> tuple[str, str]:
    """``(title, body)`` for one unprompted card. Never contains a mention."""
    fresh = drift(cat, now)
    opener = pick(AMBIENT_OPENERS.get(kind, AMBIENT_OPENERS["checkin"]), rng)
    name = _name(fresh)
    if kind == "riddle":
        question, answer = pick(RIDDLES, rng)
        body = f"{opener}\n\n> {question}\n\n-# Answer: ||{answer}||"
    elif kind == "challenge":
        body = f"{opener}\n\n> {pick(CHALLENGES, rng)}"
    elif kind == "mischief":
        body = f"{opener}\n\n{panel_body(fresh, now)}"
    else:
        body = f"{opener}\n\n{face(fresh)} {name} is {mood_word(fresh)}. {panel_body(fresh, now).splitlines()[-1]}"
    return f"{face(fresh)} {name}", body


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
#: How often the cat may react to a message at all, and how often it may *say*
#: something. Separate numbers on purpose: a reaction is a tiny thing and a reply
#: is a sentence, so they are not the same intrusion.
REACTION_MIN_GAP = 60.0
REPLY_MIN_GAP = 5 * MINUTE

REACTION_EMOJI: tuple[str, ...] = ("🐾", "💛", "😺", "✨", "🐈", "🌙")

#: What a message has to contain for the cat to answer it, and what it answers
#: with. Every line is deliberately generic: the toy does not read the room, so it
#: must not pretend to — a keyword is a nudge, not a conversation.
KEYWORD_REPLIES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("meow", "mrrp", "nya", "purr"),
        ("mrrp. 🐾", "meow meow — yes, i heard you.", "purrr. that is all i had to say."),
    ),
    (
        ("bored", "boring", "nothing to do"),
        ("then come lose at something. `/bored` is one command away. 🎾",),
    ),
    (
        ("hungry", "food", "snack", "eat"),
        ("feed me and i will stop looking at you like that. 🍖",),
    ),
    (
        ("tired", "sleepy", "gn", "good night", "goodnight"),
        ("go to bed. i will guard the empty room. 🌙",),
    ),
    (
        ("good morning", "gm", "morning"),
        ("morning. the sun came up again. typical. ☀️",),
    ),
    (
        ("sad", "lonely", "alone"),
        ("i am here. i am not much company, but i am here. 🐾",),
    ),
    (
        ("hi", "hello", "hey", "yo"),
        ("hello from the floor. 🐈", "hi. busy doing nothing."),
    ),
)

#: What the cat says when you *mention* it, or when you say its name.
MENTION_LINES: tuple[str, ...] = (
    "you called? 🐾",
    "mrrp. i was asleep. i am now awake, mostly.",
    "yes? no? both?",
    "present. on the floor, as always.",
    "i heard my name. what did i do this time.",
)


@dataclass(frozen=True)
class ChatDecision:
    """What the cat does with one message: nothing, a reaction, or a reply."""

    react: str | None = None
    reply: str | None = None
    matched: str = ""


def chat_decision(
    text: str,
    *,
    mentioned: bool = False,
    cat_name: str = "",
    now: float = 0.0,
    last_react: float = 0.0,
    last_reply: float = 0.0,
    rng: random.Random | None = None,
) -> ChatDecision:
    """The rule for one message, with both clocks passed in.

    Order matters: a mention or a name beats a keyword, a keyword beats a bare
    reaction, and a bare reaction only happens when its own minute has passed. A
    reply and a reaction are never both: the reply already said something.
    """
    body = clean_text(text)
    if not body:
        return ChatDecision()

    name = (cat_name or "").strip().lower()
    called = bool(mentioned) or (len(name) >= 3 and keyword_hit(body, name))
    reply_ready = (float(now) - float(last_reply or 0.0)) >= REPLY_MIN_GAP

    if called:
        if reply_ready:
            return ChatDecision(reply=pick(MENTION_LINES, rng), matched="mention")
        return ChatDecision()

    for needles, lines in KEYWORD_REPLIES:
        if any(keyword_hit(body, needle) for needle in needles):
            if reply_ready:
                return ChatDecision(reply=pick(lines, rng), matched=needles[0])
            return ChatDecision()

    if len(body) >= 4 and (float(now) - float(last_react or 0.0)) >= REACTION_MIN_GAP:
        return ChatDecision(react=pick(REACTION_EMOJI, rng), matched="reaction")
    return ChatDecision()


#: One compiled word-boundary pattern per phrase. Substring matching is what put
#: ``hi`` inside ``shipped`` and answered a message about a release with a greeting.
#: The boundaries are ``(?<!\w)`` / ``(?!\w)`` rather than ``\b`` because a needle
#: may hold a space or punctuation, where ``\b`` is not the edge anybody means.
_WORD_PATTERNS: dict[str, re.Pattern[str]] = {}


def keyword_hit(text: str, needle: str) -> bool:
    """Whether ``needle`` appears in ``text`` as a whole phrase, case-insensitively."""
    phrase = str(needle or "").strip().lower()
    if not phrase:
        return False
    pattern = _WORD_PATTERNS.get(phrase)
    if pattern is None:
        pattern = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)")
        _WORD_PATTERNS[phrase] = pattern
    return pattern.search(str(text or "").lower()) is not None


def clean_text(text: str, *, limit: int = 400) -> str:
    """One line of what somebody typed, collapsed and bounded."""
    body = " ".join(str(text or "").split())
    return body[:limit]


#: Words that make a name unusable: a cat called ``@everyone`` is a ping, and a
#: cat called ``discord.gg/x`` is a link somebody else gets to post.
BANNED_IN_NAME = ("@", "http", "://", "```", "discord", "everyone", "here", "<", ">")


def clean_name(raw: str, *, fallback: str = DEFAULT_NAME) -> str:
    """A name that is safe in a card, or the fallback.

    Deliberately strict: this string is rendered on every panel in the server and
    can be edited forty times a day, so anything that could ping, link or break a
    markdown fence is refused rather than escaped.
    """
    text = " ".join(str(raw or "").split())
    for banned in BANNED_IN_NAME:
        if banned in text.lower():
            return fallback
    text = text.strip("*_`~|#").strip()
    if not text:
        return fallback
    return text[:NAME_LIMIT]


def name_refused(raw: str) -> bool:
    """Whether :func:`clean_name` would **refuse** a name rather than trim it.

    Trimmed is a rename; refused is the fallback, and the card has to say which
    happened or the person reads "Now answering to Meow" and concludes the command
    ignored them. It mirrors ``clean_name``'s two refusals exactly rather than
    comparing the output, because ``**Meow**`` cleans to the fallback *and* was
    perfectly acceptable.
    """
    text = " ".join(str(raw or "").split())
    if not text:
        return False
    if any(banned in text.lower() for banned in BANNED_IN_NAME):
        return True
    return not text.strip("*_`~|#").strip()


# --------------------------------------------------------------------------- #
# Corpora
# --------------------------------------------------------------------------- #
CHALLENGES: tuple[str, ...] = (
    "Stand up and stretch. Right now. The cat is watching.",
    "Drink some water before you read another line of code.",
    "Close the tab you have not looked at in an hour.",
    "Say something nice about a thing you built this week, out loud.",
    "Fix the one comment that is still wrong. You know which one.",
    "Rename that variable to something a stranger would understand.",
    "Take the break. The bug will still be there, and it will be smaller.",
    "Write down the one thing you learned today before you forget it.",
    "Reply to the message you have been leaving on read.",
    "Look out of an actual window for thirty seconds.",
)

RIDDLES: tuple[tuple[str, str], ...] = (
    ("I have whiskers but no chin, and I nap on your keyboard. What am I?", "a cat"),
    ("The more of me you take, the more you leave behind. What am I?", "footsteps"),
    ("I am always coming but never arrive. What am I?", "tomorrow"),
    ("I am full of holes but still hold water. What am I?", "a sponge"),
    ("What has keys but opens no locks?", "a keyboard"),
    ("What gets wetter the more it dries?", "a towel"),
    ("What has a head and a tail but no body?", "a coin"),
    ("I speak without a mouth and hear without ears. What am I?", "an echo"),
    ("What can you catch but never throw?", "a cold"),
    ("What has one eye but cannot see?", "a needle"),
)

JOKES: tuple[str, ...] = (
    "Why did the cat sit on the computer? To keep an eye on the mouse.",
    "I told the cat a joke about UDP. I do not know if it got it.",
    "A cat's favourite language is purr-thon.",
    "Why do cats never ship on Fridays? They push to main and nap.",
    "The cat reviewed my code. It sat on it, which is the review it deserved.",
    "Why was the cat so good at debugging? It always follows the smelly trail.",
    "My cat does not have bugs, it has features it refuses to document.",
)
