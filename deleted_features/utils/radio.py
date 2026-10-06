"""How a request becomes a station: the defaults, the pool, and the pick.

Everything here is pure — no Discord, no database, no network — because *which
station does this request mean* is the one question in the radio system that
deserves to be answerable without a gateway. :mod:`utils.radio_catalogue` is the
table, :mod:`utils.radio_stream` plays a row from it, and this is the decision
between rows.

**The defaults are the request**, and each was chosen rather than inherited:

* **English**, because a bot with no better information should not pick a
  language at random, and a server that has chosen one gets its own back — the
  command writes the language down for the server when somebody with permission
  asks for one.
* **Shuffle**, because a radio with no station named is a radio being asked to
  choose, and asking again should not play the same thing.
* **Ad-free.** This is the one default that changed shape in the rebuild. The
  table's ``ad-free`` tag is the only thing in it a listener *chooses on*, and
  the honest default for "play me something" is the station that will not talk
  over the music. It is a *preference* rather than a guarantee: a pool with no
  ad-free station in it is played anyway and the card says why, because a setting
  must never leave a room with nothing to play. ``strict`` is the mode that
  refuses — including a station a member names by hand — and it is the one a
  server has to ask for.
* **24/7 until stopped.** A radio that leaves after five quiet minutes is a
  player, not a radio. The seat is remembered per server, and ``/radio stop`` is
  what ends it.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from . import radio_catalogue as catalogue
from .radio_catalogue import CATALOGUE, Station

#: What the pick is in when nobody has said: the bot's own default, and the
#: language the stored setting is read against.
DEFAULT_LANGUAGE = "English"

#: What ``/radio`` can be told about advertising. Three modes rather than a
#: switch, because *no advertising* is two different requests: one is *"do not
#: put ads in front of me when you choose"* and the other is *"never, even if I
#: name that station"* — and the second has to be able to say no to a member,
#: which is a thing a server opts into deliberately.
ADS_MODES: dict[str, str] = {
    "ad-free": "when I choose, I stick to stations that carry no advertising",
    "any": "advertising is not filtered at all",
    "strict": "never a station that carries advertising — even one you name",
}

#: The mode a server gets before it asks for one.
DEFAULT_ADS = "ad-free"

#: Whether the seat is held before a server asks for anything: the radio is 24/7
#: until somebody stops it, which is the whole difference between a radio and a
#: player. A server that has stopped it keeps its answer.
DEFAULT_STAY = True

#: What the command stores as "no preference": a word rather than the empty
#: string at the command's edge, because *any* is a thing a person can be told
#: and ``''`` is not. The storage keeps the empty string.
LANGUAGE_ANY = "any"


@dataclass(frozen=True)
class Choice:
    """What came of a request: a station, or the reason there is not one."""

    station: Station | None
    reason: str
    #: Set when the pick had to compromise — the pool was empty under the ad
    #: preference, or the named language has no station and the table was used
    #: instead. Printed under the station card, never swallowed.
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.station is not None

    @property
    def from_memory(self) -> bool:
        return self.reason == "memory"


@dataclass(frozen=True)
class Pool:
    """The stations a request may pick from, and what it cost to get them."""

    stations: list[Station]
    note: str = ""


def language_name(value: str) -> str:
    """The table's spelling of a language somebody typed, or ``''``.

    Forgiving about case and spacing because this is autocompleted *and* typable,
    and exact about everything else: a name the table does not hold is ``''``,
    which every caller turns into a refusal naming the real list rather than a
    stored value nothing can satisfy.
    """
    wanted = catalogue._flat(value)  # noqa: SLF001 — the table's own comparison
    if not wanted:
        return ""
    for language in catalogue.languages():
        if catalogue._flat(language) == wanted:  # noqa: SLF001
            return language
    return ""


def ads_mode(value: str) -> str:
    """A stored or typed advertising mode, never a value the table has not got.

    Anything unrecognised reads as the default, which is what makes a hand-edited
    database and a value from a future version both mean *do what you would have
    done* rather than *refuse to play anything*.
    """
    key = (value or "").strip().lower().replace("_", "-")
    return key if key in ADS_MODES else DEFAULT_ADS


def country_name(value: str) -> str:
    """The table's spelling of a country somebody typed, or ``''``."""
    wanted = catalogue._flat(value)  # noqa: SLF001
    if not wanted:
        return ""
    for country in catalogue.countries():
        if catalogue._flat(country) == wanted:  # noqa: SLF001
            return country
    return ""


def pool(*, language: str = "", country: str = "", ads: str = DEFAULT_ADS) -> Pool:
    """What a request may pick from, narrowed by the two settings that apply.

    The order is not arbitrary. The **country** is the narrowest thing a person
    can ask for and it wins outright: knowing which country you want tells the
    table more than knowing which language does. The **language** narrows what is
    left, and the **advertising mode** narrows it again.

    What never happens is a *request* being widened to keep the room busy. A
    country or a language somebody asked for is a request, so a table with
    nothing there answers with a sentence naming what was asked for — never with a
    station from somewhere else, which is how *"play me Fiji"* would otherwise
    become a French ambient stream with nobody the wiser. ``ad-free`` is a
    *preference* and gives way instead: the pick stays inside the pool that was
    asked for, plays a station that carries advertising, and says so. ``strict``
    is the only mode that can empty a pool, because it is a rule — and a rule that
    can be talked out of is not one.
    """
    mode = ads_mode(ads)
    asked_language = (language or "").strip()
    if asked_language.lower() == LANGUAGE_ANY:
        asked_language = ""
    asked_country = (country or "").strip()
    stations: list[Station]

    if asked_country:
        wanted_country = country_name(asked_country)
        if not wanted_country:
            return Pool(
                [],
                f"I don't have a station for a country called **{asked_country}** — "
                f"{len(catalogue.countries())} countries have one.",
            )
        stations = catalogue.in_country(wanted_country)
        where = wanted_country
    elif asked_language:
        wanted_language = language_name(asked_language)
        if not wanted_language:
            return Pool(
                [],
                f"I don't have a language called **{asked_language}** — the table "
                f"speaks {len(catalogue.languages())}: "
                + ", ".join(catalogue.languages())
                + ".",
            )
        stations = catalogue.in_language(wanted_language)
        where = wanted_language
    else:
        stations = list(CATALOGUE)
        where = ""

    if mode == "strict":
        stations = [station for station in stations if station.ad_free]
        if not stations:
            return Pool([], "Nothing matches under *never any advertising*.")
        return Pool(stations)

    if mode == "ad-free":
        qualifying = [station for station in stations if station.ad_free]
        if qualifying:
            return Pool(qualifying)
        if where:
            return Pool(
                stations,
                f"No ad-free station in **{where}** yet — this pick carries "
                "advertising, and an administrator can ask for another language "
                "or country.",
            )
    return Pool(stations)


def choose(
    *,
    named: str = "",
    remembered: str = "",
    language: str = "",
    country: str = "",
    ads: str = DEFAULT_ADS,
    rng: random.Random | None = None,
) -> Choice:
    """The station a request means, or the reason there is not one.

    In order: a station somebody **named** (the only case a pool cannot veto,
    except under ``strict``), then the station this server **remembered** if it
    still qualifies, then a **shuffled** pick out of the pool. A remembered
    station that the current settings would not choose is set aside rather than
    played, because a server's standing choice has to be able to change — and the
    card says the pick was the bot's when that happens, which is the difference
    between a promise and a coincidence.

    ``language`` and ``country`` are what somebody typed, not table spellings:
    :func:`pool` resolves them, and refusing a value the table has never heard of
    is the point of passing them through rather than filtering them away here.
    """
    mode = ads_mode(ads)
    gambler = rng or random

    wanted = (named or "").strip()
    if wanted:
        found = catalogue.find(wanted)
        if found is None:
            return Choice(None, "unknown", wanted)
        if mode == "strict" and not found.ad_free:
            # The *station's* name, not what was typed: the sentence is read by
            # somebody who may have sent a key from the picker, and it is about
            # the station they will not be hearing.
            return Choice(None, "strict", found.name)
        return Choice(found, "named")

    picked = pool(language=language, country=country, ads=mode)
    if not picked.stations:
        return Choice(None, "empty", picked.note)

    if remembered:
        earlier = catalogue.by_key(remembered) or catalogue.find(remembered)
        if earlier is not None and any(
            station.key == earlier.key for station in picked.stations
        ):
            return Choice(earlier, "memory", picked.note)

    return Choice(gambler.choice(picked.stations), "shuffled", picked.note)


def refusal_sentence(choice: Choice) -> str:
    """What a card says when there is no station, in the words the case deserves.

    Each of the three is a different problem with a different fix, which is why
    they are not folded into one line: a name the table does not hold is a typo
    (or a station somebody remembers from another bot), ``strict`` is this
    server's own rule and only an administrator can change it, and an empty pool
    is a gap in the table that nobody in the room can do anything about.
    """
    if choice.reason == "unknown":
        return (
            f"🔎 I don't have a station called **{choice.note}** — which is not the same "
            "as it not existing. Start typing in the station box to search every genre, "
            "language and country in the table."
        )
    if choice.reason == "strict":
        return (
            f"🚫 **{choice.note}** carries advertising, and this server is set to "
            "*never any advertising* — including by name.\n"
            f"-# {len(catalogue.ad_free_stations())} of {len(CATALOGUE)} stations qualify, "
            "and an administrator can change the setting by passing a different "
            "`ads` to `/radio`."
        )
    return f"📭 {choice.note or 'Nothing in the table matches that yet.'}"


def reason_sentence(choice: Choice, *, language: str = "") -> str:
    """The line under a station card that says why *this* station is on air."""
    if choice.reason == "named":
        return ""
    if choice.reason == "country":
        return "-# the capital's own station"
    if choice.reason == "memory":
        return "-# This server's last station — name one to change it"
    pool_line = (
        f"in {language}" if language and not choice.reason == "country" else "in the table"
    )
    return f"-# shuffled from the stations {pool_line}"
