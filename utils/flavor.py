"""How the bot looks in *one* server: cat, dog, or nothing cute at all.

Everything the fun commands say was written for a cat. That is a real part of
this bot's character, but it is not everybody's taste — a server that does not
want cats in every card had no way to say so short of turning the whole Fun
module off, which also removes the commands they *did* want. This is that missing
axis: one setting per server that changes the flavour of the fun surfaces, from
"cat" (the default, and the behaviour every server already had) to "dog" to
"neutral".

Two boundaries, stated because they are the honest limits of this feature:

* **It is presentation only.** Flavour changes the picture, the wording and the
  emoji. It cannot change which sounds are bundled — the audio files in
  ``assets/sounds`` *are* cat recordings — so the soundboard keeps its audio and
  says where it comes from instead of pretending to be a dog. A server that wants
  no animal sounds at all turns the Fun module off, which is one switch and
  already per-server.
* **It is per guild, not per user.** Discord stores a member's preferences
  nowhere a bot may read, so a choice that two people in one channel disagree
  about has to be the server's, decided once, rather than a coin toss per look.

Unset means :data:`DEFAULT` — cat — so a server that has never touched this keeps
exactly what it had before the setting existed, which is the only safe default for
a value that changes how every card looks.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import db

#: Where the choice lives in ``guild_settings``.
SETTING_KEY = "fun_flavor"

#: The value an unset or unreadable setting resolves to.
DEFAULT = "cat"


@dataclass(frozen=True)
class Flavor:
    """One server's flavour, as the commands need it."""

    id: str
    label: str
    emoji: str
    #: The pet whose picture appears, or ``None`` for "draw nothing".
    pet: str | None
    #: What the pet is called in a sentence ("cat", "dog", nothing).
    noun: str
    #: What the pet says, in the bot's voice.
    sounds: tuple[str, ...]
    #: Title above the soundboard card.
    soundboard_title: str
    #: Where the audio actually comes from, as a footer would say it.
    audio_credit: str
    #: One line for the picker, so the choice is not a mystery.
    summary: str

    def describe(self) -> str:
        """A sentence a member can read without knowing the internals."""
        picture = {
            "cat": "cat pictures and gifs",
            "dog": "dog pictures",
        }.get(self.pet or "", "no animal pictures")
        return f"**{self.label}** {self.emoji} — {picture}; **{self.noun}** wording."


FLAVORS: dict[str, Flavor] = {
    "cat": Flavor(
        id="cat",
        label="Cat",
        emoji="🐱",
        pet="cat",
        noun="cat",
        sounds=(
            "Meow.",
            "Mreoww!",
            "Mrrrp?",
            "Nya!",
            "*purrrr*",
            "Meow meow!",
            "Hiss!",
            "Feed me right now. I mean, meow.",
            "Prrbt!",
            "*stares at you judgingly*",
        ),
        soundboard_title="Meow Soundboard",
        audio_credit="Real cat audio",
        summary="cats in the pictures and meows in the copy",
    ),
    "dog": Flavor(
        id="dog",
        label="Dog",
        emoji="🐶",
        pet="dog",
        noun="dog",
        sounds=(
            "Woof.",
            "Bork!",
            "Awooo!",
            "*tail wagging intensifies*",
            "Bark bark!",
            "Squirrel?! Where?!",
            "Throw the ball. Throw the ball. Throw the ball.",
            "*happy panting*",
            "*stares at you hopefully*",
        ),
        soundboard_title="Sound Board",
        audio_credit="Bundled sound clips",
        summary="dogs in the pictures and woofs in the copy",
    ),
    "neutral": Flavor(
        id="neutral",
        label="Neutral",
        emoji="🐾",
        pet=None,
        noun="pet",
        sounds=(
            "Hello.",
            "Beep boop.",
            "*waves*",
            "Here.",
            "Hi there!",
            "What's up?",
            "*judges you quietly*",
        ),
        soundboard_title="Sound Board",
        audio_credit="Bundled sound clips",
        summary="no animal pictures and no animal wording",
    ),
}


def choices() -> list[Flavor]:
    """Every flavour, in the order a picker should show them."""
    return [FLAVORS[key] for key in ("cat", "dog", "neutral")]


def parse(value) -> str | None:
    """Read a written flavour name, or ``None`` if it is not one.

    Kept here rather than in the cog so the accepted spellings are testable
    without a Discord client.
    """
    if value is None:
        return None
    if isinstance(value, Flavor):
        return value.id
    text = str(value).strip().lower()
    if text in FLAVORS:
        return text
    for flavor in FLAVORS.values():
        if text in (flavor.label.lower(), f"{flavor.emoji} {flavor.label.lower()}"):
            return flavor.id
    # A couple of things people actually type for "I don't want the cat stuff".
    if text in ("none", "plain", "off", "no", "neutral-only"):
        return "neutral"
    return None


def get_cached(guild_id: int, value) -> Flavor:
    """The flavour for a stored value, tolerating anything (pure)."""
    return FLAVORS.get(parse(value) or "", FLAVORS[DEFAULT])


async def get(guild_id: int | None) -> Flavor:
    """This server's flavour. Never raises; anything odd means the default."""
    if not guild_id:
        return FLAVORS[DEFAULT]
    try:
        stored = await db.get_setting(int(guild_id), SETTING_KEY, None)
    except Exception:
        return FLAVORS[DEFAULT]
    return get_cached(int(guild_id), stored)


async def set_flavor(guild_id: int | None, value) -> Flavor | None:
    """Save a choice. Returns the flavour, or ``None`` if the value was not one."""
    flavor_id = parse(value)
    if flavor_id is None or not guild_id:
        return None
    await db.set_setting(int(guild_id), SETTING_KEY, flavor_id)
    return FLAVORS[flavor_id]
