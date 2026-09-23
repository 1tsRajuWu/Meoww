"""Company in a voice channel: the bot sits with somebody who is alone in one.

Somebody sitting in a voice channel on their own is the loneliest surface this
bot has, and it is the one place it can do something about it without being asked:
join, be visibly *there*, and say so. Everything below is about making that a
feature rather than an intrusion, because a bot that joins a voice channel
uninvited gets kicked out of servers.

Four rules, and every one of them is a reason a person would otherwise be right to
be annoyed:

* **Alone means alone.** The trigger is a channel whose only occupant is one human
  being. Another bot in there is company of a kind, so a channel with a radio bot
  in it is not a channel where anyone is alone — and two bots sitting silently in
  the same room is a joke nobody asked for.
* **It listens to nothing and says nothing out loud.** The connection is deafened
  and muted: the bot cannot transmit audio and cannot receive it, and that is a
  state a member can *see* in the channel (the deafen icon) rather than a promise
  in a privacy policy.
* **It leaves when its reason to be there is gone** — the channel empties, or a
  second human arrives, which is the company it was standing in for.
* **It never follows somebody who walked out on it.** A member who leaves the
  channel while the bot is in it, and rejoins within the cooldown, is asking to be
  alone; the bot waits before it comes back.

The honesty this module owes: **Discord does not let a bot watch a Go Live
stream.** There is no API for it, and no amount of voice connection changes that.
What a bot can see is the ``self_stream`` flag on somebody's voice state, which is
the *fact* that they are live and nothing about the content. So the greeting says
exactly that, and the console shows who is streaming rather than pretending to
show a picture.

Per-server settings live in ``guild_settings`` like every other switch: ``mode``
(auto or off) and whether the bot says hello when it arrives. Unset means auto,
which is what every server gets, so turning it *off* is the deliberate act.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import db

#: Where the two per-server choices live.
MODE_KEY = "company_mode"
GREETING_KEY = "company_greeting"

#: The emoji the greeting carries. A custom emoji is only usable by a client that
#: shares a server with it, so the *tag* is resolved at send time (see
#: :func:`emoji_markup`) and the picture travels separately as the card's
#: thumbnail — which is why the picture still appears in a server that cannot use
#: the tag.
EMOJI_ID = 1518478117369417821
EMOJI_NAME = "pastelpink"
#: Written out rather than interpolated from :data:`EMOJI_ID` on purpose: the
#: weekly media watch finds hardcoded media URLs by *scanning the source*, and a
#: URL assembled from a placeholder is skipped as "not a URL yet" — so a picture
#: built from the id would never be probed, and this one is a picture the bot
#: actually sends. A test ties the two back together.
EMOJI_URL = "https://cdn.discordapp.com/emojis/1518478117369417821.webp?size=64&animated=true"

#: What the bot does, as a decision rather than an action. This module stays free
#: of the discord client so the rules are testable without a gateway.
JOIN, LEAVE, STAY, SKIP = "join", "leave", "stay", "skip"

#: Modes a server may be in.
AUTO, OFF = "auto", "off"
MODES: tuple[str, ...] = (AUTO, OFF)

#: How long somebody has to have been alone before the bot arrives. Long enough
#: that a channel emptying because two people are mid-swap is not a trigger, short
#: enough that "I am sitting here alone" gets an answer while it still is one.
GRACE_SECONDS = 12.0

#: How long after somebody leaves a channel the bot will stay out of it. A member
#: who walked out of a channel the bot was in has had their answer.
COOLDOWN_SECONDS = 300.0

#: What it says on arrival. Written in the bot's voice, and written as a
#: continuation rather than a headline: the emoji is the last thing on the line.
GREETINGS: tuple[str, ...] = (
    "hi — you're not alone in here, I'm with you",
    "sat down next to you. Take your time, I'm not going anywhere",
    "hello! The room was quiet, so I brought it some company",
    "hey — I saw the empty room and thought you might want a friend in it",
)

#: Said instead when the person is live. The stream itself is the one thing the
#: bot cannot see, so the sentence does not pretend otherwise.
STREAM_GREETING = "you're live — I can't watch along, but you're not up there on your own"
#: ...and the field under it.
#:
#: Written for the person streaming, in the second person, because that is who reads
#: a card in a voice channel's chat. It used to explain `self_stream` and the absence
#: of a Go Live API to them — a true sentence that told its reader nothing they could
#: use, about a mechanism they did not ask about. What they are told instead is the
#: only thing that matters to them: they were noticed, and it is nice.
STREAM_NOTE = (
    "You're live, and I'd watch if I could — Discord doesn't show bots a stream, so "
    "picture me as the one listener in the corner, cheering you on anyway."
)


@dataclass(frozen=True)
class Settings:
    """One server's choice about being kept company."""

    mode: str = AUTO
    greeting: bool = True

    @property
    def enabled(self) -> bool:
        return self.mode == AUTO

    def describe(self) -> str:
        return (
            f"**{self.mode}** — "
            + ("I sit with somebody who is alone in a voice channel"
               if self.enabled else "I never join a voice channel by myself")
            + (", and I say hello when I arrive." if self.greeting else ", and I arrive silently.")
        )


def parse_mode(value) -> str | None:
    """Read a written mode, or ``None`` if it is not one."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in MODES:
        return text
    # The words people actually type for each half.
    if text in ("on", "yes", "true", "enabled", "enable", "join"):
        return AUTO
    if text in ("off", "no", "false", "disabled", "disable", "never"):
        return OFF
    return None


def parse_greeting(value) -> bool | None:
    """Read a written yes/no about the hello, or ``None`` if it is neither."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("on", "yes", "true", "1", "enabled"):
        return True
    if text in ("off", "no", "false", "0", "disabled", "silent"):
        return False
    return None


def settings_from(mode_value, greeting_value) -> Settings:
    """The settings a stored pair of values describes (pure, total)."""
    mode = parse_mode(mode_value) or AUTO
    greeting = parse_greeting(greeting_value)
    return Settings(mode=mode, greeting=True if greeting is None else greeting)


async def get(guild_id: int | None) -> Settings:
    """This server's settings. Never raises; anything odd means the default."""
    if not guild_id:
        return Settings()
    try:
        mode = await db.get_setting(int(guild_id), MODE_KEY, None)
        greeting = await db.get_setting(int(guild_id), GREETING_KEY, None)
    except Exception:
        return Settings()
    return settings_from(mode, greeting)


async def set_mode(guild_id: int | None, value) -> Settings | None:
    """Save the mode. ``None`` when the value was not a mode."""
    mode = parse_mode(value)
    if mode is None or not guild_id:
        return None
    await db.set_setting(int(guild_id), MODE_KEY, mode)
    return await get(guild_id)


async def set_greeting(guild_id: int | None, value) -> Settings | None:
    """Save whether the bot says hello. ``None`` when the value was not yes/no."""
    greeting = parse_greeting(value)
    if greeting is None or not guild_id:
        return None
    await db.set_setting(int(guild_id), GREETING_KEY, "on" if greeting else "off")
    return await get(guild_id)


# --------------------------------------------------------------------------- #
# Who is where, and what that means
# --------------------------------------------------------------------------- #
def alone(members, *, bot_id: int) -> bool:
    """True when ``members`` is one human and nothing else.

    Bots are counted, deliberately: a channel with a radio bot in it is not a room
    somebody is alone in, and a second *companion* bot would be a comedy.
    """
    people = [member for member in members or () if getattr(member, "id", None) != bot_id]
    if len(people) != 1:
        return False
    only = people[0]
    return not getattr(only, "bot", False)


def humans_in(channel, *, bot_id: int = 0) -> int:
    """How many people (not bots) are in a channel right now."""
    return sum(
        1
        for member in getattr(channel, "members", None) or ()
        if not getattr(member, "bot", False) and getattr(member, "id", None) != bot_id
    )


def is_stage(channel) -> bool:
    """Whether this channel is a stage.

    Read twice on purpose: ``discord.StageChannel`` says so in its class name and
    every channel also says so in ``.type``. A stage is a performance rather than a
    room, so arriving on one would put the bot *on stage* — the reading has to be
    right either way, because being wrong costs somebody their event.
    """
    if type(channel).__name__ == "StageChannel":
        return True
    kind = getattr(channel, "type", None)
    # ``discord.ChannelType.stage_voice``, named when the type is an enum and read
    # as-is when somebody hands us the string.
    named = getattr(kind, "name", None) or (kind if isinstance(kind, str) else None)
    if named == "stage_voice":
        return True
    # ...and by number, for a type handed over as the bare value
    # (``ChannelType.stage_voice == 13``), enum or not.
    return kind == 13 or getattr(kind, "value", None) == 13


def eligible(channel, *, bot_id: int, afk_channel_id: int | None = None) -> bool:
    """Whether this channel is one the bot may keep somebody company in."""
    if channel is None:
        return False
    if afk_channel_id is not None and getattr(channel, "id", None) == afk_channel_id:
        return False  # the AFK channel is where Discord puts people who are gone
    if is_stage(channel):
        return False
    return alone(getattr(channel, "members", None), bot_id=bot_id)


def decide(*, humans: int, here: bool, alone_channel=None) -> str:
    """What to do about one channel, as a decision (pure).

    ``humans`` counts people in the channel this decision is about, ``here`` says
    whether the bot is connected *to that channel*, and ``alone_channel`` is the
    channel whose only occupant is one human anywhere in the guild — which is the
    only thing that may make the bot join. Everything not covered is :data:`SKIP`,
    so a decision is always total and never an accident.
    """
    if here:
        if humans == 0:
            return LEAVE  # the room emptied; there is nobody left to be with
        if humans >= 2:
            return LEAVE  # real company arrived, which is what I was standing in for
        return STAY
    if alone_channel is None:
        return SKIP
    if humans >= 1:
        # Somebody is already in the channel that wants company, or the room is
        # not empty: either way there is nothing to fix by arriving.
        return SKIP
    return JOIN


# --------------------------------------------------------------------------- #
# What it says
# --------------------------------------------------------------------------- #
def emoji_markup(emoji) -> str:
    """The ``<:name:id>`` tag for a custom emoji, or ``""`` when unusable.

    Discord only renders a custom emoji tag for a client that shares a server with
    it, which for a bot means: only if ``get_emoji`` found it in its own guilds.
    Anywhere else the tag is printed as literal text, so a card that cannot use it
    says nothing instead — and the picture still travels as the thumbnail.
    """
    if emoji is None:
        return ""
    name = getattr(emoji, "name", None)
    identifier = getattr(emoji, "id", None)
    if not name or not identifier:
        return ""
    prefix = "a" if getattr(emoji, "animated", False) else ""
    return f"<{prefix}:{name}:{identifier}>"


def resolve_emoji(client):
    """The emoji, if this client can actually use it.

    ``get_emoji`` searches the guilds the bot is *in*, which is exactly the
    condition Discord applies — so a deployment that is not in the emoji's server
    gets ``None`` rather than a tag that renders as text.
    """
    getter = getattr(client, "get_emoji", None)
    if getter is None:
        return None
    try:
        return getter(EMOJI_ID)
    except Exception:
        return None


def picture_url(emoji) -> str:
    """The picture for the card: the resolved emoji's own URL, or the known one."""
    url = getattr(emoji, "url", None)
    return str(url) if url else EMOJI_URL


def greeting(name: str | None = None, *, streaming: bool = False, pick=None) -> str:
    """The line the bot says on arrival.

    ``pick`` is the chooser for the non-streaming variants, injectable so a test
    can pin which one it gets; ``name`` is the person's display name, which is only
    used when the card has no other way to say who it is talking to.
    """
    if streaming:
        return STREAM_GREETING
    chooser = pick or (lambda options: options[0])
    line = chooser(list(GREETINGS))
    return f"{line}, {name}." if name else f"{line}."


#: What is said when the bot has to walk out on somebody to go and play radio.
#:
#: The scenario both features share one socket for: a person is alone with the bot,
#: somebody in **another** room presses play, and the radio moves the connection —
#: because one Discord client sits in one voice channel at a time. Whoever is left
#: behind watches the bot leave and is told nothing, which reads as *it left
#: because of me*. The truth is that it is two rooms away doing what it was asked,
#: so it gets said: ``brb``, in the bot's own voice, and it comes back.
LEAVING: tuple[str, ...] = (
    "brb — somebody asked for the radio in another room, so I'm on speaker duty",
    "brb, somebody wants the radio next door and I'm the speaker again",
    "one minute — the radio is wanted in the other room, I'll be right back",
)


def leaving(*, pick=None) -> str:
    """The line said when the radio takes the connection away. Injectably chosen,
    like :func:`greeting`, so a test can pin which variant it gets."""
    chooser = pick or (lambda options: options[0])
    return chooser(list(LEAVING))


def leaving_footer() -> str:
    """How the errand ends, held to the same rule as :func:`footer`.

    **No command name.** This card lands in front of the same people the greeting
    does — whoever was on their own — and the switch is a Manage Server command
    they cannot see, let alone run. It says when the bot is back and who can turn
    the behaviour off, and names no syntax either of them would have to try.
    """
    return "back when the song is finished · a server admin can turn this off for the server"


def body(markup: str, line: str) -> str:
    """The greeting as card text, with the emoji where the eye lands last."""
    return f"{markup} {line}".strip() if markup else line


def fields(*, streaming: bool, channel: str | None = None) -> list[tuple[str, str]]:
    """The facts under the greeting — one field, and only when it is a fact."""
    out: list[tuple[str, str]] = []
    if streaming:
        out.append(("You're live", STREAM_NOTE))
    if channel:
        out.append((
            "Where",
            f"I'm in {channel} with you, muted and deafened so nothing of mine "
            "gets in your way.",
        ))
    return out


def footer() -> str:
    """How to get rid of it, said plainly, on the card that arrived uninvited.

    **No command name, and that is the fix rather than an omission.** The card
    lands in front of whoever was alone — usually somebody who *cannot* run the
    switch: ``/company`` is declared with the Manage Server audience, so Discord
    hides it from their picker entirely, and a footer naming a command the reader
    cannot see is a card that reads as broken. It used to name one anyway, which is
    what "why are you showing a command to a normal user" was about.

    So the sentence says *who can act on it*, which is useful to the member and to
    the administrator, and names no syntax either of them would have to try. Where
    the switch lives is on the surfaces that belong to the person who can throw it:
    the command's own description, the help panel, and the dashboard's Systems page.
    """
    return (
        "I only came in because you were on your own · a server admin can turn "
        "this off for the whole server whenever they like"
    )



# --------------------------------------------------------------------------- #
# The pure provider the console and the tests read
# --------------------------------------------------------------------------- #
def alone_rows(guild, *, bot_id: int) -> list[dict]:
    """Every channel in this guild whose only occupant is one human.

    Counts and ids only — no names — which is the rule every read in this project
    follows when it might be published (see ``utils/figures``).
    """
    rows: list[dict] = []
    for channel in voice_channels(guild):
        members = list(getattr(channel, "members", None) or ())
        people = [m for m in members if not getattr(m, "bot", False) and getattr(m, "id", None) != bot_id]
        if len(people) != 1 or len(members) != 1:
            continue
        state = getattr(people[0], "voice", None)
        rows.append({
            "channel_id": str(getattr(channel, "id", "")),
            "channel": getattr(channel, "name", None),
            "user_id": str(getattr(people[0], "id", "")),
            "streaming": bool(getattr(state, "self_stream", False)),
            "video": bool(getattr(state, "self_video", False)),
        })
    return rows


def stream_rows(guild) -> list[dict]:
    """Everybody in this guild who is live or on camera, and where."""
    rows: list[dict] = []
    for channel in voice_channels(guild):
        for member in getattr(channel, "members", None) or ():
            if getattr(member, "bot", False):
                continue
            state = getattr(member, "voice", None)
            if not (getattr(state, "self_stream", False) or getattr(state, "self_video", False)):
                continue
            rows.append({
                "channel_id": str(getattr(channel, "id", "")),
                "channel": getattr(channel, "name", None),
                "user_id": str(getattr(member, "id", "")),
                "streaming": bool(getattr(state, "self_stream", False)),
                "video": bool(getattr(state, "self_video", False)),
            })
    return rows


def voice_channels(guild) -> list:
    """Voice channels and stages, tolerating a stand-in with only one of them."""
    channels = list(getattr(guild, "voice_channels", None) or [])
    channels += list(getattr(guild, "stage_channels", None) or [])
    return channels
