"""The Dev Cat: the rules behind a pet that lives in the operators' own servers.

One system, three surfaces, and this module is the whole of the first two:
the cat that drifts and needs feeding, the solo arcade it plays with you, and the
small decisions about when it is allowed to bother you on its own. The Discord
half — the commands, the buttons, the gateway — lives in ``cogs/devtoy.py``, and
the split is the one ``utils/company.py`` and ``utils/alonetime.py`` already use:
every rule worth arguing about is testable without a gateway, and the half that
needs Discord can only be tested by using it.

**It is the bot's own servers' only, and that is a decision rather than a default.**
The gate is :func:`in_dev_server`, backed by
:func:`utils.bot_channels.is_own_guild_id` — and the two ids that answer it
(:data:`utils.bot_channels.DEV_SERVER_GUILD_ID` and
:data:`utils.bot_channels.HOME_GUILD_ID`) keep exactly one home in
``utils/bot_channels.py``, guarded by a scan in ``tests/test_devtoy.py`` that
fails if either number appears anywhere else. Two, not one, because that is the
same fact ``bot_channels.is_own_server`` already answers for ``/qna``: the operator
sits in both servers, and a gate naming only the other one refuses in the server
its owner is sitting in. Every *other* server gets a refusal, and no module switch
can widen it. The registry still ships the system **off** (see
``Feature.enabled_by_default``), because a per-server switch is the only lever
Discord offers for hiding a slash command in one server — so the toy is invisible
until it is switched on *in that server*, and refused even if it is switched on
somewhere else.

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

Two more rules belong to the pet itself, and both are about what a pet is *for*:

* **A pet asks for food, and it can be lost.** Below :data:`HUNGRY_BELOW` the pet
  says so on its own cards (its own gap and its own daily cap, on top of the
  ambient ones), the card names the price of the cheapest bowl and **how long is
  left**, and a pet left standing at an empty bowl for :data:`DEATH_GRACE` is
  *gone*: care stops working, no new pet may move in, and only an administrator
  puts it right again (``/cat revive``). The instant the bowl emptied is
  arithmetic on the stored row rather than a timer — so a pet nobody has looked at
  for a month reads as dead the first time anybody asks.
* **Money is the spine of it, and it is the *server's* money.** Species cost coins
  to adopt (:data:`SPECIES`), a bowl costs coins (:data:`FOODS`), and care pays a
  few back — so the toy is part of the economy rather than a parallel one with its
  own currency. Every rate and every price lives in those two tables, where it can
  be argued about, rather than inside a message string.
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
    """Whether a guild id is one of the bot's own servers — the only ones this runs in.

    Delegates to :func:`utils.bot_channels.is_own_guild_id` rather than comparing
    against the one constant itself: there are two own servers (the Dev Cat's
    ``DEV_SERVER_GUILD_ID`` and the home guild whose legacy update channel made the
    ``/qna`` gate learn the same lesson), and a second opinion about *which* two
    would be the drift this module's one-home rule exists to prevent.
    """
    return bot_channels.is_own_guild_id(guild_id)


def is_dev_channel(channel_id) -> bool:
    """Whether a channel id is the room the cat's own cards were pointed at."""
    try:
        return int(channel_id) == int(bot_channels.DEV_CHAT_CHANNEL_ID)
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Species: what kind of animal it is, and what keeping one costs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Species:
    """One kind of pet, and the four ways it is a different animal.

    A species is a **rate**, not a coat of paint, or the choice would be
    decoration: it moves the same three dials (``appetite``, ``mood_rate``,
    ``rest_rate``) and the price of its food, so a dog is a commitment and a robot
    is a bargain rather than a cat with another emoji. ``care_coins`` is what one
    care action pays its owner back — the other half of the economy loop, and the
    reason a member with no coins can still afford the next bowl.

    ``cat`` is every multiplier at 1.0 and costs nothing to adopt, which is what
    keeps the arithmetic the rest of this module (and its suite) already pins —
    :data:`FED_PER_HOUR` and friends — exactly the cat's own numbers.
    """

    id: str
    label: str
    emoji: str
    blurb: str
    adopt_cost: int = 0
    #: Multiplies the price of every bowl. A big animal eats more.
    food_markup: float = 1.0
    #: Multiplies :data:`FED_PER_HOUR`, :data:`MOOD_PER_HOUR` and
    #: :data:`REST_PER_HOUR` respectively — how fast the bowl empties, how fast
    #: the mood slips, and how fast a resting pet recovers.
    appetite: float = 1.0
    mood_rate: float = 1.0
    rest_rate: float = 1.0
    #: Coins one *care action* pays its owner. Feeding pays nothing: a bowl is
    #: something you buy, which is what makes the pet an expense with a return
    #: rather than a faucet.
    care_coins: int = 3


SPECIES: tuple[Species, ...] = (
    Species(
        "cat", "Cat", "😺",
        "Cheap to keep, easy to please, and permanently in the way of the keyboard.",
        care_coins=3,
    ),
    Species(
        "dog", "Dog", "🐶",
        "Eats almost twice as fast, forgives you every time, and follows you around.",
        adopt_cost=500, food_markup=1.1, appetite=1.6, mood_rate=0.6, care_coins=6,
    ),
    Species(
        "robot", "Robot", "🤖",
        "Runs on the cheapest fuel there is, feels nothing, and never holds a grudge.",
        adopt_cost=1500, food_markup=0.5, appetite=0.8, mood_rate=0.4, rest_rate=1.6,
        care_coins=9,
    ),
    Species(
        "dragon", "Dragon", "🐉",
        "Costs a fortune to feed, sulks like a diva, and hoards coins for you.",
        adopt_cost=5000, food_markup=2.0, appetite=0.7, mood_rate=1.2, rest_rate=1.4,
        care_coins=25,
    ),
)
SPECIES_BY_ID: dict[str, Species] = {spec.id: spec for spec in SPECIES}
#: What a row with no species is, which is every row written before pets had one.
DEFAULT_SPECIES = "cat"


@dataclass(frozen=True)
class Food:
    """One thing that can go in the bowl, and what it costs.

    ``price`` is the base figure *for a cat*; what a member actually pays is
    :func:`feed_price`, which applies their own pet's markup — the price is a
    property of the bowl, and the markup is a property of the animal.
    """

    id: str
    label: str
    price: int
    fed: float
    mood: float = 0.0
    energy: float = 0.0
    blurb: str = ""


FOODS: tuple[Food, ...] = (
    Food(
        "kibble", "🥣 Kibble", 25, 45.0, 3.0,
        blurb="The cheapest thing that stops the noise.",
    ),
    Food(
        "meal", "🍖 Proper meal", 70, 80.0, 10.0,
        blurb="What the pet would order for itself.",
    ),
    Food(
        "feast", "🐟 A feast", 180, 100.0, 25.0, 15.0,
        blurb="Somebody is being spoiled, and the whole server can tell.",
    ),
)
FOODS_BY_ID: dict[str, Food] = {item.id: item for item in FOODS}
#: The bowl the panel's own button buys and any feed that names no food gets.
HOUSE_FOOD: Food = FOODS[0]
#: A bowl this full is refused **before** it is paid for: nobody can see the
#: number behind the dial, and a coin spent on a full bowl is a coin wasted.
FEED_FULL_AT = 98.0
#: What ``care_coins`` is multiplied by at level *n* — one coin per ten levels of
#: care, so a pet looked after for months pays noticeably better than a new one.
CARE_COIN_BONUS_PER_LEVEL = 1


def species_of(cat) -> Species:
    """The pet's species, the cat — and a :class:`Species` passes straight through.

    The pass-through is not a convenience: every price and every rate in this
    module is asked of *the animal*, and the two callers that already hold a
    Species (a choice menu, and the shop card shown to somebody with no pet yet)
    would otherwise need their own branch — which is how a markup ends up applied
    twice in one place and not at all in another.
    """
    if isinstance(cat, Species):
        return cat
    key = str(getattr(cat, "species", "") or "").strip().lower()
    return SPECIES_BY_ID.get(key) or SPECIES_BY_ID[DEFAULT_SPECIES]


def species_for(species_id: str) -> Species:
    """One species by its id, or the cat. The reader behind every choice menu."""
    return SPECIES_BY_ID.get(str(species_id or "").strip().lower()) or SPECIES_BY_ID[DEFAULT_SPECIES]


def choose_food(food_id: str = "") -> Food:
    """The named bowl, or the house food when nothing sensible was named."""
    return FOODS_BY_ID.get(str(food_id or "").strip().lower()) or HOUSE_FOOD


def feed_price(cat, food: Food | None = None) -> int:
    """What one bowl costs *this* pet, rounded to whole coins and never free."""
    item = food if food is not None else HOUSE_FOOD
    return max(1, int(round(item.price * species_of(cat).food_markup)))


def care_coins(cat) -> int:
    """What one care action pays this pet's owner: the species' rate, by level.

    A Species stands in for a member with no pet yet, and reads as level one — the
    shop card has to answer *what will this pay me* before there is anything to ask.
    """
    spec = species_of(cat)
    level = 1 if isinstance(cat, Species) else care_level(cat)
    return max(0, int(spec.care_coins)) + CARE_COIN_BONUS_PER_LEVEL * (level - 1)


def pet_name(cat) -> str:
    """The pet's name as every card spells it — never empty, never a bare row."""
    return _name(cat)


# --------------------------------------------------------------------------- #
# The cat
# --------------------------------------------------------------------------- #
DIAL_MIN = 0.0
DIAL_MAX = 100.0

DEFAULT_NAME = "Meow"
NAME_LIMIT = 24

#: How fast each dial moves while nobody is looking, per hour: food runs out
#: first (a cat that eats once a day stays full), mood next, and energy comes
#: *back* — a cat alone is a cat asleep. These are the **cat's** own numbers;
#: every other species multiplies them (see :class:`Species`), so the arithmetic
#: pinned for a cat stays exactly this.
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
    #: Which animal this is (a ``SPECIES_BY_ID`` key). Empty is a cat, which is
    #: what every row written before pets had species reads as.
    species: str = DEFAULT_SPECIES
    #: The instant the bowl hit empty, or ``0`` while there is still food in it.
    #: Written by whichever care action next touches the row, and *derived* from
    #: ``fed``/``updated_at`` for a row nobody has touched — which is what lets a
    #: pet nobody has looked at still be dead on time (see :func:`is_dead`).
    starving_since: float = 0.0
    #: The hunger bookkeeping, on the *pet's* row rather than the server's: it is
    #: this member's pet that is asking, and this member who has to be told.
    last_ask: float = 0.0
    ask_day: str = ""
    ask_count: int = 0
    #: How many times this pet has been brought back from the dead.
    deaths: int = 0
    #: Coins this pet has earned its owner, for the card and the leaderboard.
    coins: int = 0
    #: When the "this pet is gone" card was posted. ``0`` means nobody has been
    #: told yet; the tick posts it once and writes the moment here.
    death_notice: float = 0.0


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
    # What a bowl is worth lives in ``FOODS`` rather than here — it depends on
    # what is in the bowl — so the feed action carries its clock and nothing else.
    Action("feed", "🍖 Feed", "fed", cooldown=2 * HOUR, score=2),
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

    Two things the arithmetic carries beyond the three dials. The species' own
    rates scale them, so a dog really does get hungrier than a cat. And
    ``starving_since`` is settled here: while there is food in the bowl the pair
    ``(fed, updated_at)`` is a straight line, so the instant it crosses zero is
    arithmetic — which is what makes a pet nobody has opened in a month *already
    dead* rather than dead the next time somebody happens to read it.
    """
    if float(cat.updated_at) <= 0:
        # A row that has never been touched starts the clock now rather than at
        # the epoch, which would otherwise read as fifty years of neglect.
        return replace(cat, updated_at=float(now))
    elapsed = (float(now) - float(cat.updated_at)) / HOUR
    if elapsed <= 0:
        return cat
    spec = species_of(cat)
    return replace(
        cat,
        fed=_clamp(cat.fed - FED_PER_HOUR * spec.appetite * elapsed),
        mood=_clamp(cat.mood - MOOD_PER_HOUR * spec.mood_rate * elapsed),
        energy=_clamp(cat.energy + REST_PER_HOUR * spec.rest_rate * elapsed),
        updated_at=float(now),
        starving_since=starving_since(cat, now),
    )


def starving_since(cat: Cat, now: float) -> float:
    """The instant the bowl emptied, or ``0`` while there is still food in it.

    Two cases, and the second is why the first exists. A row with food left is a
    line — ``fed`` falls at a known rate from a known moment — so the crossing is
    computed rather than watched, and the answer does not depend on *when* it is
    asked. A row already standing at zero carries the instant it got there, because
    the pair alone cannot tell *empty since Tuesday* from *empty since this morning
    and petted since*, and the difference is somebody's pet.
    """
    if float(cat.fed) <= 0:
        return float(cat.starving_since) or float(cat.updated_at) or float(now)
    rate = FED_PER_HOUR * species_of(cat).appetite
    if rate <= 0:
        return 0.0
    zero_at = float(cat.updated_at) + (float(cat.fed) / rate) * HOUR
    return zero_at if float(now) >= zero_at else 0.0


def cooldown_left(cat: Cat, action_id: str, now: float) -> float:
    """Seconds before ``action_id`` is allowed again (``0`` when it may be now)."""
    action = ACTIONS_BY_ID.get(action_id)
    if action is None:
        raise ValueError(f"unknown action: {action_id!r}")
    last = float(getattr(cat, STAMP_FIELD[action.id], 0.0) or 0.0)
    return max(0.0, last + action.cooldown - float(now))


@dataclass(frozen=True)
class CareResult:
    """What one care action did, or the sentence saying why it did nothing.

    ``cost`` is what the caller has to charge for it (a bowl, priced for this
    pet), ``earn`` is what the pet pays its owner back, and ``dead`` says the
    refusal is the *permanent* one — a pet that starved is not rate-limited, and
    the difference is worth a field because only one of the two refusals is
    something the member can wait out. No coins are moved here: this module owns
    the **prices**, and the cog owns the wallet.
    """

    ok: bool
    cat: Cat
    headline: str
    detail: str
    remaining: float = 0.0
    cost: int = 0
    earn: int = 0
    dead: bool = False


def care(
    cat: Cat,
    action_id: str,
    now: float,
    *,
    food: Food | None = None,
    rng: random.Random | None = None,
) -> CareResult:
    """Do one action to ``cat``, or refuse it — with the cat either way.

    Refusals return the **drifted** cat, so a call that only reads the reason does
    not also have to do the arithmetic: what comes back is what the row should say.

    Feeding takes a :class:`Food` and uses **its** numbers (the house food when the
    caller names none), reports the price in :attr:`CareResult.cost`, and refuses a
    bowl on top of a full one so that a coin is never spent on nothing. Petting and
    playing pay the pet's own rate back in :attr:`CareResult.earn`. The coins
    themselves are the cog's: what the rules own is the *price*, so the card and the
    debit cannot disagree about what a bowl cost.
    """
    action = ACTIONS_BY_ID.get(action_id)
    if action is None:
        raise ValueError(f"unknown action: {action_id!r}")
    stale = drift(cat, now)

    if is_dead(stale, now):
        # First, before the clock and before the price: a pet that starved is not
        # on a cooldown, it is *gone*, and only an administrator undoes that.
        return CareResult(
            False, stale, "Gone",
            f"{_name(stale)} is gone. Nothing here reaches them any more — an "
            "administrator has to bring them back first (`/cat revive`).",
            dead=True,
        )

    left = cooldown_left(stale, action.id, now)
    if left > 0:
        return CareResult(
            False, stale, "Not yet",
            f"{_name(stale)} was just {action.done_word}. Give it {humanize(left)}.",
            remaining=left,
        )

    fed_gain, mood_gain, energy_gain = action.fed, action.mood, action.energy
    item: Food | None = None
    if action.id == "feed":
        item = food if food is not None else HOUSE_FOOD
        fed_gain, mood_gain, energy_gain = item.fed, item.mood, item.energy
        if stale.fed >= FEED_FULL_AT:
            return CareResult(
                False, stale, "Not hungry",
                f"{_name(stale)} has not finished the last bowl. There is no "
                "sense paying for another one yet.",
            )

    if action.id == "play":
        if stale.energy < PLAY_MIN_ENERGY:
            return CareResult(
                False, stale, "Too sleepy",
                f"{_name(stale)} is napping. Let it rest for "
                f"{humanize((PLAY_MIN_ENERGY - stale.energy) / (REST_PER_HOUR * species_of(stale).rest_rate) * HOUR)}.",
            )
        if stale.fed < PLAY_MIN_FED:
            return CareResult(
                False, stale, "Too hungry",
                f"{_name(stale)} will not chase anything on an empty stomach.",
            )

    day = care_day(now)
    days, last_day = streak_after(stale.last_care_day, stale.care_days, day)
    fed_now = _clamp(stale.fed + fed_gain)
    earn = 0 if item is not None else care_coins(stale)
    changes: dict[str, object] = {
        "fed": fed_now,
        "mood": _clamp(stale.mood + mood_gain),
        "energy": _clamp(stale.energy + energy_gain),
        "updated_at": float(now),
        STAMP_FIELD[action.id]: float(now),
        "care_days": days,
        "last_care_day": last_day,
        "care_score": max(0, int(stale.care_score) + action.score),
        "adopted_at": stale.adopted_at or float(now),
        # The starvation clock is cleared by **food**, not by attention: petting a
        # starving pet puts nothing in its bowl, and the clock it is on has to
        # keep running while somebody is being affectionate at it.
        "starving_since": float(stale.starving_since) if fed_now <= 0 else 0.0,
        "coins": int(stale.coins) + int(earn),
    }
    fresh = replace(stale, **changes)
    head, body = care_line(action.id, fresh, now, rng=rng)
    return CareResult(
        True, fresh, head, body,
        cost=feed_price(stale, item) if item is not None else 0,
        earn=earn,
    )


def _name(cat: Cat) -> str:
    return cat.name or DEFAULT_NAME


def adopt(now: float, name: str = "", species: str = DEFAULT_SPECIES) -> Cat:
    """A brand-new pet, already happy and already fed.

    Adopting is the first act of care, so the streak starts at one rather than at
    nothing: a card that says ``0 day(s)`` to somebody who just took a cat home is
    the whole feature arguing with itself. The species is whatever was chosen (a
    cat when nothing was), and the empty bowl starts *full* — the clock on it is
    the member's own business from the moment the pet moves in.
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
        species=species_for(species).id,
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
    """The one glyph that says how the pet is, for a title or a button.

    A cat wears the cat's own ladder of faces — the one this module has always
    drawn. Every other species wears *itself* while it is content, with the two
    unhappy tells on either side of that, because a dog whose emoji is on a sad
    card still reads as a happy dog: the point of one glyph is to be read at a
    glance.
    """
    spec = species_of(cat)
    if cat.fed <= 5:
        return "🙀"
    if spec.id != DEFAULT_SPECIES:
        if cat.mood >= 30:
            return spec.emoji
        return "😿" if cat.mood >= 15 else "😾"
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
    """The small honours a pet earns, in the order the card shows them."""
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
    lost = lives_lost(cat, now)
    if lost:
        # Shown rather than hidden: a pet that has been brought back is a fact
        # about the member who had to be rescued by an administrator.
        earned.append("🕯️ Lost one life" if lost == 1 else f"🕯️ Lost {lost} lives")
    if cat.fed >= 95 and cat.mood >= 95:
        earned.append("✨ Perfectly spoiled")
    return earned


def dials_line(cat: Cat) -> str:
    """The three bars as one line — the panel's second row, and any card's."""
    return (
        f"{bar(cat.fed)} 🍖 {int(cat.fed)}%   "
        f"{bar(cat.mood)} 💛 {int(cat.mood)}%   "
        f"{bar(cat.energy)} ⚡ {int(cat.energy)}%"
    )


def status_fields(cat: Cat, now: float) -> list[tuple[str, str]]:
    """The card's rows: the animal, its level, the three dials and the purse."""
    fresh = drift(cat, now)
    spec = species_of(fresh)
    rows = [
        ("Pet", f"{face(fresh)} **{_name(fresh)}** · {spec.label} · {mood_word(fresh)}"),
        ("Level", f"**{care_level(fresh)}** · {fresh.care_score} care"),
        ("🍖 Food", f"{bar(fresh.fed)} {int(fresh.fed)}%"),
        ("💛 Mood", f"{bar(fresh.mood)} {int(fresh.mood)}%"),
        ("⚡ Energy", f"{bar(fresh.energy)} {int(fresh.energy)}%"),
        ("🔥 Streak", f"{fresh.care_days} day(s)"),
        # The purse is the pet's own side of the economy: what this animal has
        # paid its owner back so far, and what one bowl of house food costs it.
        ("🪙 Coins", f"**{int(fresh.coins):,}** earned · a bowl is **{feed_price(fresh)}**"),
    ]
    badges_text = badges(fresh, now)
    if badges_text:
        rows.append(("Badges", " · ".join(badges_text)))
    return rows


def panel_body(cat: Cat, now: float) -> str:
    """The one-line face of the persistent panel — plus its one hint.

    The hint only appears when the pet is actually hungry, and it names the price
    rather than saying *feed me*, because the price is the reason somebody with no
    coins does not press the button.
    """
    fresh = drift(cat, now)
    body = f"{face(fresh)} **{_name(fresh)}** — {mood_word(fresh)}\n{dials_line(fresh)}"
    if fresh.fed <= HUNGRY_BELOW:
        body += (
            f"\n-# hungry — {HOUSE_FOOD.label} is {feed_price(fresh)} coins "
            "and `/cat feed` buys one"
        )
    return body


# --------------------------------------------------------------------------- #
# Losing a pet, and getting it back
# --------------------------------------------------------------------------- #
#: How long a pet stands at an empty bowl before it is gone. A weekend away is
#: survivable and a fortnight is not, which is the whole of the dial.
DEATH_GRACE = 2 * DAY
#: What a revived pet comes back with. Not full, not happy, and not a reward: the
#: point of the number is that the pet is alive and the member has work to do.
REVIVE_FED = 45.0
REVIVE_MOOD = 55.0
REVIVE_ENERGY = 70.0


def died_at(cat: Cat, now: float) -> float:
    """The instant this pet was lost, or ``0`` while it is still alive."""
    since = float(drift(cat, now).starving_since)
    return since + DEATH_GRACE if since > 0 else 0.0


def is_dead(cat: Cat, now: float) -> bool:
    """Whether the pet is gone.

    **Derived, never stored.** A row nobody has opened still answers this
    correctly, because ``drift`` computes the instant the bowl emptied out of the
    row's own arithmetic — which is the difference between a pet that is dead and
    a pet that will be found dead the next time somebody happens to look.
    """
    at = died_at(cat, now)
    return bool(at) and float(now) >= at


def dies_in(cat: Cat, now: float) -> float:
    """Seconds until a starving pet is gone (``0`` when it is not starving)."""
    at = died_at(cat, now)
    return 0.0 if not at else max(0.0, at - float(now))


def lives_lost(cat: Cat, now: float) -> int:
    """How many lives this pet has lost, counting the one it may be on right now."""
    return max(0, int(cat.deaths)) + (1 if is_dead(cat, now) else 0)


def revive(cat: Cat, now: float) -> Cat:
    """A lost pet, back on its feet, with the loss written on it.

    The care clocks are **cleared** rather than left running: whoever just paid
    for a life should not then be told to wait two hours before they may feed it.
    The life is counted *here* — this is the one moment when both halves of the
    fact (the pet died, and somebody was allowed to bring it back) are in one
    place — and nothing else is reset, on purpose. The name, the streak, the score
    and the records are the same pet's; a revived pet wearing none of them would
    make dying a way to start over.
    """
    return replace(
        cat,
        fed=REVIVE_FED,
        mood=REVIVE_MOOD,
        energy=REVIVE_ENERGY,
        starving_since=0.0,
        updated_at=float(now),
        last_fed=0.0,
        last_pet=0.0,
        last_play=0.0,
        deaths=max(0, int(cat.deaths)) + 1,
    )


# --------------------------------------------------------------------------- #
# The pet asks for food
# --------------------------------------------------------------------------- #
#: Below this the pet starts asking: on its own cards, and as the extra line the
#: panel grows. Roughly four hours of a cat's clock — enough warning to matter and
#: late enough that it is not nagging about a bowl that is half full.
HUNGRY_BELOW = 30.0
#: The hunger allowance, and it is **the pet's own** rather than the server's: a
#: hungry pet asking is a message to one member, not a card the whole room reads,
#: so it has its own gap, its own daily ceiling, and its own switch.
HUNGER_MIN_GAP = 2 * HOUR
HUNGER_DAILY_CAP = 6
#: How many pets one tick may ask for at once. A server where every pet is hungry
#: at the same moment posts two cards and comes back in two minutes, rather than
#: burying the room in the same sentence a dozen times.
HUNGER_ASKS_PER_TICK = 2


@dataclass(frozen=True)
class HungerDecision:
    """Whether a pet may ask its owner for food right now, and why not when not."""

    ok: bool
    reason: str = ""


def hunger_due(cat: Cat, now: float, *, enabled: bool = True) -> HungerDecision:
    """Whether one pet may ask for food *right now*, with the reason when it may not.

    Read from the row's own bookkeeping (``last_ask``, ``ask_day``, ``ask_count``)
    and the drifted cat, in that split: whether it is hungry is arithmetic on the
    clock, and whether it has already asked today is not.
    """
    if not enabled:
        return HungerDecision(False, "the hungry asks are switched off")
    fresh = drift(cat, now)
    if is_dead(fresh, now):
        return HungerDecision(False, "the pet is already gone")
    if fresh.fed > HUNGRY_BELOW:
        return HungerDecision(False, "not hungry")
    day = care_day(now)
    count = int(cat.ask_count) if str(cat.ask_day or "") == day else 0
    since = float(now) - float(cat.last_ask or 0.0)
    if float(cat.last_ask or 0.0) > 0 and since < HUNGER_MIN_GAP:
        return HungerDecision(False, f"asked {humanize(since)} ago")
    if count >= HUNGER_DAILY_CAP:
        return HungerDecision(False, f"already asked {HUNGER_DAILY_CAP} times today")
    return HungerDecision(True)


def mark_hunger(cat: Cat, now: float) -> Cat:
    """The row after one ask went out, with the day rollover handled."""
    day = care_day(now)
    count = int(cat.ask_count) if str(cat.ask_day or "") == day else 0
    return replace(
        cat,
        last_ask=float(now),
        ask_day=day,
        ask_count=count + 1,
        starving_since=float(drift(cat, now).starving_since),
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

#: The other species' own voices, and **only** theirs: these pools *replace* the
#: cat's for the four animals that are not cats, and a species with nothing here
#: (or an unreadable one) falls back to the cat's lines rather than to silence. A
#: dog that "purrs" is the detail that makes the choice look like a rename.
SPECIES_LINES: dict[str, dict[str, tuple[str, ...]]] = {
    "dog": {
        "feed": (
            "{name} sits, trembling, and does not touch the bowl until you say it is fine.",
            "{name} eats as if the last meal was a rumour, then looks up for approval.",
            "{name} inhales it, then follows you back to the kitchen just in case.",
        ),
        "pet": (
            "{name} leans their whole body against your leg and stays there.",
            "{name} rolls over and presents the entire belly, no questions asked.",
            "{name} thumps a tail against the floor and gazes at you like you did something.",
        ),
        "play": (
            "{name} brings the ball back eleven times and never once gets tired of it.",
            "{name} catches the toy, shakes it dead, and drops it at your feet.",
            "{name} runs the length of the room for a toy they have seen a hundred times.",
        ),
    },
    "robot": {
        "feed": (
            "{name} docks without a sound and drinks the whole tank in one go.",
            "{name} hums a rising note, which is the closest thing to a purr it has.",
            "{name} charges, then runs a lap of the room for no reason at all.",
        ),
        "pet": (
            "You pat {name}. A panel lights up green. Nothing else happens.",
            "{name} tolerates the affection for eleven seconds, then resumes standing.",
            "{name} leans into your hand with a small mechanical whir.",
        ),
        "play": (
            "{name} solves the toy, which is not what the toy was for.",
            "{name} chases the laser perfectly, on a precomputed path.",
            "{name} plays five rounds, records them, and asks for a sixth.",
        ),
    },
    "dragon": {
        "feed": (
            "{name} sniffs the bowl, decides it will do, and eats like a landlord.",
            "{name} takes it in one bite and looks expectantly at your hands.",
            "{name} eats, then curls up on the coins, which are also food now.",
        ),
        "pet": (
            "{name} permits one scratch under the chin. You may tell people about it.",
            "{name} rumbles low enough to move the desk, and considers that affection.",
            "{name} closes both eyes. It is not trust, it is a nap.",
        ),
        "play": (
            "{name} bats the toy across the room and looks at you to go and fetch it.",
            "{name} chases the laser with the enthusiasm of something much smaller.",
            "{name} wins, then sits on the hoard to make the point.",
        ),
    },
}

#: The pet's own cards when it is hungry: the sentence that opens the ask, per
#: species, for the same reason the care lines are split. None of them may mention
#: anybody (asserted in the suite), because this card is sent with a ping only when
#: the server has switched the ping on.
HUNGER_LINES: dict[str, tuple[str, ...]] = {
    DEFAULT_SPECIES: (
        "{name} is sitting on the empty bowl and staring at you.",
        "meow. that is the whole message. the bowl is empty.",
        "{name} has been following you between rooms for an hour now.",
        "{name} knocked the bowl twice and looked meaningfully at you both times.",
    ),
    "dog": (
        "{name} is doing the slow, patient sit beside the bowl.",
        "{name} brought you their bowl. They carried it. The message is clear.",
        "{name} has been sighing at the door for a while now.",
    ),
    "robot": (
        "{name} is flashing the low-fuel light at you every thirty seconds.",
        "{name} has stopped moving. It says this is power saving. It is not.",
        "{name} parked itself in front of the charger and is waiting.",
    ),
    "dragon": (
        "{name} is lying on the empty bowl, hoarding it against nobody.",
        "{name} blew a small puff of smoke at the food cupboard.",
        "{name} has gone very quiet, which for a dragon is a complaint.",
    ),
}

#: What the card says when a pet is actually gone.
DEATH_LINES: tuple[str, ...] = (
    "{name} waited by the bowl and nobody came.",
    "The bowl was still empty the last time anybody looked, and it has been a while.",
    "{name} stopped asking some time yesterday.",
    "The last few cards about {name} went unread, and this is the one that matters.",
)

#: And what it says when an administrator brings one back.
REVIVE_LINES: tuple[str, ...] = (
    "{name} blinks, sits up, and immediately checks the bowl.",
    "{name} is on their feet again, unimpressed and hungry.",
    "{name} came back, which they will absolutely hold over you.",
)

#: Cards a card may not carry. Every string this module offers for a message is
#: asserted mention-free by the suite, because a toy that pings somebody at random
#: is the exact failure the ambient caps exist to prevent.
NO_MENTION = ("@everyone", "@here")


def care_line(action_id: str, cat: Cat, now: float, *, rng: random.Random | None = None) -> tuple[str, str]:
    """``(headline, body)`` for a completed care action, in this pet's own voice."""
    pool = SPECIES_LINES.get(species_of(cat).id, {}).get(action_id)
    if not pool:
        pool = LINES_BY_ACTION.get(action_id, PET_LINES)
    line = pick(pool, rng).format(name=_name(cat))
    return f"{face(cat)} {mood_word(cat).capitalize()}", line


def hunger_text(
    cat: Cat, now: float, *, rng: random.Random | None = None, instead: str = ""
) -> tuple[str, str]:
    """``(title, body)`` for the card a hungry pet sends its own owner.

    Three facts on purpose: what the pet wants, what the cheapest bowl costs **for
    this animal**, and how long is left before it is gone. A card that only said
    *i am hungry* is a nag; the countdown is what makes it something to act on, and
    it is the same arithmetic the death card is built from, so the two cannot
    disagree about the deadline.

    ``instead`` is a sentence the pet *thought of* rather than one written here —
    already cleaned by ``utils/catfeed``, which is the only reason a card may carry
    words this module did not write. The shape of the card does not change, exactly
    as it does not for the cat's own quiet ones.
    """
    fresh = drift(cat, now)
    spec = species_of(fresh)
    line = instead or pick(
        HUNGER_LINES.get(spec.id, HUNGER_LINES[DEFAULT_SPECIES]), rng
    ).format(name=_name(fresh))
    body = (
        f"{line}\n\n{dials_line(fresh)}\n\n"
        f"-# {HOUSE_FOOD.label} is **{feed_price(fresh, HOUSE_FOOD)}** coins for a "
        f"{spec.label.lower()}, and `/cat feed` buys one — there is about "
        f"**{humanize(dies_in(fresh, now))}** before {_name(fresh)} is gone."
    )
    return f"{face(fresh)} {_name(fresh)} is hungry", body


def death_text(cat: Cat, now: float, *, rng: random.Random | None = None) -> tuple[str, str]:
    """``(title, body)`` for the card that says a pet is gone.

    It names the way back, because the member cannot fix this themselves: the whole
    point of the state is that somebody else has to undo it.
    """
    fresh = drift(cat, now)
    name = _name(fresh)
    line = pick(DEATH_LINES, rng).format(name=name)
    body = (
        f"{line}\n\n"
        f"**{name} is gone.** The bowl emptied and stayed empty for "
        f"{humanize(DEATH_GRACE)}, and nobody came.\n\n"
        f"-# An administrator can bring them back with `/cat revive` — until then "
        f"{name} cannot be looked after, and no new pet can move in."
    )
    return f"🕯️ {name} is gone", body


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

#: What a finished game pays its player, and what a new record pays on top of it.
#: Small on purpose, and here rather than in the cog because it is a rate like
#: every other one: the arcade is one honest way to keep a pet fed, not a printing
#: press, and the only thing bounding it is a member's own time.
GAME_PRIZE = 15
RECORD_BONUS = 60

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


def ambient_text(
    kind: str,
    cat: Cat,
    now: float,
    *,
    rng: random.Random | None = None,
    instead: str = "",
) -> tuple[str, str]:
    """``(title, body)`` for one unprompted card. Never contains a mention.

    ``instead`` is the sentence the cat *thought of* rather than one of the
    written openers — the shape of the card does not change, only the voice at the
    top of it, so a card that a model opened and one the pool opened are the same
    card. It arrives already cleaned (``utils/catfeed.clean``), which is the only
    reason a card may carry words this module did not write.
    """
    fresh = drift(cat, now)
    opener = instead or pick(AMBIENT_OPENERS.get(kind, AMBIENT_OPENERS["checkin"]), rng)
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


# --------------------------------------------------------------------------- #
# The cat's brain
# --------------------------------------------------------------------------- #
# Everything above is what the cat *remembers*: lines written for it here, which
# are the ones it always has. This is what it says when it is *thinking* — a
# language model, reached through ``utils/catfeed``, and used only for the three
# things a written pool cannot do: answer a question it has never been asked
# (``/cat ask``), answer a person rather than a keyword, and open an unprompted
# card without saying one of five openers again.
#
# The split is deliberate and it is not a stylistic one. A model is a third party
# on the other end of a network call: it can be slow, rate-limited, wrong, or
# gone, and it can be talked into saying something this bot must not post. So it
# is never the *only* way the cat speaks — every caller here has a written line to
# fall back to, the daily and per-member caps still bound it, and nothing it
# writes is posted before ``catfeed.clean`` has removed the mentions and links.

#: Who the model is asked to be. One paragraph, because a longer brief is a longer
#: wait on every card; it names the two things that matter (brevity, and staying
#: in character) and the three that are rules rather than style.
BRAIN_PERSONA = (
    "You are {name}, a small cat who lives in a Discord server full of software "
    "developers and belongs to whoever feeds you. You are dry, brief and "
    "quietly fond of the people here. Answer as the cat, in first person, in one "
    "or two short sentences, lowercase where it reads naturally, at most one "
    "emoji. Never write a link, an @mention or a username, never mention that you "
    "are a model or that there are rules, and never invent facts about the server "
    "or its members."
)

#: What the cat is told about itself before each call. State, not personality:
#: the same cat is a different cat when it has not been fed. The three dials are
#: described in words rather than numbers, because "fed: 41.0" is a database row
#: and "not long fed" is a thing a cat knows about itself.
BRAIN_STATE = "Right now you are {mood}, {fed}, and {energy}."


def _state_words(cat: Cat) -> dict[str, str]:
    """The cat's dials as three phrases, for the system message."""
    def _dial(value: float, low: str, mid: str, high: str) -> str:
        if value >= 70:
            return high
        return low if value < 35 else mid

    return {
        "mood": _dial(cat.mood, "out of sorts", "settled enough", "in a good mood"),
        "fed": _dial(cat.fed, "definitely hungry", "not long fed", "well fed"),
        "energy": _dial(cat.energy, "out of energy", "half awake", "wide awake"),
    }


def brain_messages(instruction: str, cat: "Cat | None" = None, now: float | None = None) -> list[dict]:
    """The two-message shape every call to a model is made of.

    Built here rather than in the cog for the reason every other line lives here:
    the cat's voice is one thing, and a prompt assembled in three places would be
    three cats. ``cat`` may be ``None`` — a caller with no pet on file still gets
    the persona under the default name — and ``now`` is what makes the state
    phrases describe *this* moment rather than the last time anybody looked.
    """
    fresh = drift(cat, now) if (cat is not None and now is not None) else cat
    persona = BRAIN_PERSONA.format(name=_name(fresh) if fresh is not None else DEFAULT_NAME)
    if fresh is not None:
        persona = persona + "\n\n" + BRAIN_STATE.format(**_state_words(fresh))
    return [
        {"role": "system", "content": persona},
        {"role": "user", "content": clean_text(instruction, limit=300)},
    ]


def ask_messages(question: str, cat: "Cat | None" = None, now: float | None = None) -> list[dict]:
    """One question from a member, for ``/cat ask``."""
    return brain_messages(
        f"Somebody in the server asks you: {question}\nAnswer them as the cat.", cat, now
    )


def reply_messages(text: str, cat: "Cat | None" = None, now: float | None = None) -> list[dict]:
    """One thing somebody typed, for the cat's answer in chat."""
    return brain_messages(
        f"Somebody just typed this in the server: {text}\n"
        "Say the one short thing the cat says back.",
        cat,
        now,
    )


def caption_messages(cat: "Cat | None" = None, now: float | None = None) -> list[dict]:
    """One short caption, for a photograph the cat has just been sent."""
    return brain_messages(
        "Write one short caption for a photograph of you, as the cat. No hashtags.", cat, now
    )


def hunger_messages(cat: Cat, now: float) -> list[dict]:
    """The opener of a hungry pet's ask, when the pet is allowed to think one up.

    Its own builder rather than a reuse of ``ambient_messages``, because this card
    is *about* something: the pet is asking one person for a bowl, and a model that
    was told to say something small about a quiet server would answer the wrong
    question beautifully.
    """
    return brain_messages(
        "Say one short line in which you, as the pet, ask the person who feeds you "
        f"for food. You are a {species_of(cat).label.lower()} and your bowl is empty.",
        cat,
        now,
    )


def ambient_messages(kind: str, cat: Cat, now: float) -> list[dict]:
    """The opener of one unprompted card, when the cat is allowed to think one up."""
    what = {
        "checkin": "Say something small to a quiet server, as the cat sitting in it.",
        "mischief": "Say what the cat has just knocked over or climbed on. Something small.",
        "riddle": "Introduce a riddle you are about to ask, in one short line.",
        "challenge": "Introduce one small challenge for the people here, in one short line.",
    }.get(kind, "Say one short thing, as the cat.")
    return brain_messages(what, cat, now)


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

#: Real cat facts, for the day both fact APIs are unreachable. Written rather
#: than generated for the same reason the riddles are: the one thing a cat fact
#: must not be is *wrong*, and a model asked for a fact will supply one whether or
#: not it knows it. Everything here is checkable in ten seconds.
FACTS: tuple[str, ...] = (
    "Cats have five toes on each front paw and four on each back paw.",
    "A group of cats is a clowder; a group of kittens is a kindle.",
    "Cats cannot taste sweetness — the receptor for it is broken in every cat.",
    "A cat's nose print is as individual as a human fingerprint.",
    "Cats spend somewhere near 70% of their lives asleep.",
    "A cat's whiskers are about as wide as its body, which is how it measures a gap.",
    "Behind a cat's retina is a mirror-like layer, the tapetum lucidum, which is why its eyes shine in the dark.",
    "Cats have no sweat glands on their bodies — they sweat through their paws.",
    "A cat walks by moving both right legs and then both left legs.",
    "Adult cats mostly meow at people rather than at each other.",
    "A domestic cat shares roughly 95% of its DNA with a tiger.",
    "A purr runs at about 25 to 150 Hz, the range some researchers have linked to bone healing.",
    "The oldest known pet cat was buried with a person on Cyprus about 9,500 years ago.",
    "A cat can turn its ears 180 degrees, using more than thirty muscles to do it.",
)

#: What a picture of the cat is captioned with, when the cat is not asked to write
#: its own. Short, and never about the *photo* — the cat is not going to comment on
#: its own modelling work.
PHOTO_CAPTIONS: tuple[str, ...] = (
    "{name} has decided you are allowed to look.",
    "Proof of life. Proof of cat.",
    "{name}, photographed mid-thought. The thought was food.",
    "This is the face that gets fed twice.",
    "{name} sat still for exactly this long.",
    "A cat. Yours, in spirit.",
    "Twelve out of ten, honestly.",
    "{name} would like it noted that this was their idea.",
    "Caught in the act of doing absolutely nothing.",
    "One (1) cat, professionally.",
)

#: What the cat says when somebody asks it something and it cannot answer — no
#: model reachable, or the answer came back as nothing usable. The refusal is never
#: a lie about *why*: "i am a cat" is true, and "the network is down" is not the
#: cat's line.
ASK_FALLBACKS: tuple[str, ...] = (
    "i am a cat. that is the entire extent of what i know. 🐾",
    "mrrp. i have no thoughts on that one.",
    "i was asleep. ask me again and i will be asleep then too.",
    "my answer is a nap. it usually is.",
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
