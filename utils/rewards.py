"""Reward mathematics for the leveling system.

XP and coins are *computed*, never rolled. On the text side a message produces
two numbers in ``[0, 1]``:

* an **effort quality** — a saturating curve over characters typed and files
  attached, so the tenth paragraph is worth less than the first; and
* an **integrity discount** — what the member's last few minutes of posting look
  like (identical text repeated, sustained flood, an emoji-only "message").

The effort sets which part of the guild's configured coin band you land in, and
the two together set the multiplier, so a quiet long message beats a spam storm.

On the voice side an **activity factor** is the product of three measurable
things, because Discord never tells a bot who is *speaking*:

* **company** — somebody else has to be in the channel (solo = nothing at all);
* **presence** — not deafened, not server-muted, not a suppressed stage
  audience, halved while self-muted, bonused while streaming or on camera; and
* **freshness** — the channel must have had *some* observable event recently,
  which decays the longer the channel is completely static.

Everything here is pure, takes ``now`` as an argument, and imports no discord,
so the formulas are testable without a gateway connection.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import deque
from typing import NamedTuple

# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #
#: A member earns from text at most once per this many seconds. Messages inside
#: the window still count toward their message total, they just pay nothing.
REWARD_COOLDOWN_SECONDS = 10.0
#: Below this many effort points a message is not worth paying at all ("k", ".",
#: "??"). It still counts as a message.
MIN_EFFORT_POINTS = 3.0
#: A photo, clip or file is treated as this many characters of effort...
ATTACHMENT_POINTS = 25.0
#: ...capped, so a twenty-file dump is not a novel.
MAX_ATTACHMENT_POINTS = 150.0
#: Characters at which effort reaches ~63% of its ceiling: `1 - e^(-x/half)`.
EFFORT_HALF_LIFE = 90.0
#: How far back the anti-spam screen looks.
HISTORY_SECONDS = 300.0
#: Rewarded messages per member per window before the tempo penalty starts.
TEMPO_ALLOWANCE = 18
#: Identical-text window: reposting the same thing inside this is discounted.
DUPLICATE_SECONDS = 120.0
DUPLICATE_PENALTY = 0.85
TEMPO_PENALTY = 0.60
LOW_SIGNAL_PENALTY = 0.40
#: Even with every penalty stacked, a real message keeps this much integrity.
MIN_INTEGRITY = 0.05
#: How much of the payout the integrity discount can remove entirely.
SPAM_DISCOUNT = 0.75
#: The multiplier band: a bare-minimum message gets 0.5x, the best 2.5x.
BASE_MULTIPLIER = 0.5
TOP_MULTIPLIER = 2.5
#: Quality assumed when message text is invisible to the bot (Message Content
#: intent off). Roughly what a normal-length message scores.
UNREADABLE_QUALITY = 0.35
#: Message history kept per member, and the memory ceiling for the screen.
HISTORY_KEPT = 24
HISTORY_MAX_USERS = 4096

_NON_WORD = re.compile(r"[^0-9a-z]+")


class Payout(NamedTuple):
    """What a rewarded event pays out."""

    coins: int
    xp: int


class SpamStats(NamedTuple):
    """The anti-spam reading for one message."""

    duplicate_ratio: float
    tempo_ratio: float
    low_signal: bool


def clamp01(value: float) -> float:
    """Clamp a number into ``[0, 1]``, tolerating garbage input."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def normalize_text(text: str | None) -> str:
    """Lowercase and strip everything that isn't a letter or digit."""
    return _NON_WORD.sub("", (text or "").lower())


def text_fingerprint(text: str | None) -> str | None:
    """Short stable hash of a message's normalized text, or None for no text."""
    if text is None:
        return None
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()[:16]


def effort_points(content_length: int, attachments: int = 0) -> float:
    """Characters typed plus credit for attachments, if any."""
    points = max(0.0, float(content_length or 0))
    if attachments:
        points += min(float(attachments) * ATTACHMENT_POINTS, MAX_ATTACHMENT_POINTS)
    return points


def effort_quality(points: float) -> float:
    """Saturating ``[0, 1]`` curve: ``1 - e^(-points / EFFORT_HALF_LIFE)``.

    Diminishing returns are the point: without them a wall of text is the most
    efficient way to farm, so 60 characters buy about twice what 20 do and 600
    characters buy almost exactly what 300 do.
    """
    return 1.0 - math.exp(-max(0.0, float(points or 0)) / EFFORT_HALF_LIFE)


def integrity(duplicate_ratio: float, tempo_ratio: float, low_signal: bool = False) -> float:
    """``[MIN_INTEGRITY, 1]`` discount for how spammy this message looks.

    ``duplicate_ratio`` is the share of the member's recent messages that said
    the same thing; ``tempo_ratio`` is messages sent divided by the allowance, so
    anything above 1 is over budget and the penalty grows from there.
    """
    score = 1.0
    score *= 1.0 - DUPLICATE_PENALTY * clamp01(duplicate_ratio)
    score *= 1.0 - TEMPO_PENALTY * clamp01(float(tempo_ratio or 0.0) - 1.0)
    if low_signal:
        score *= LOW_SIGNAL_PENALTY
    return max(MIN_INTEGRITY, min(1.0, score))


def payout_multiplier(quality: float) -> float:
    """Map a ``[0, 1]`` quality onto the ``BASE_MULTIPLIER..TOP_MULTIPLIER`` band."""
    return BASE_MULTIPLIER + (TOP_MULTIPLIER - BASE_MULTIPLIER) * clamp01(quality)


def text_reward(
    *,
    content_length: int,
    attachments: int = 0,
    readable: bool = True,
    text_xp: int,
    text_min_coins: int,
    text_max_coins: int,
    duplicate_ratio: float = 0.0,
    tempo_ratio: float = 0.0,
    low_signal: bool = False,
) -> Payout:
    """Coins and XP for one message, from its effort and its integrity.

    ``readable=False`` means the bot cannot see the text at all (no Message
    Content intent) — the tempo penalty still applies, since timestamps are
    visible regardless.
    """
    low = max(1, int(text_min_coins))
    high = max(low, int(text_max_coins))
    base_xp = max(1, int(text_xp))

    if readable:
        score = integrity(duplicate_ratio, tempo_ratio, low_signal)
        quality = clamp01(effort_quality(effort_points(content_length, attachments)) * score)
    else:
        # Nothing read: score the message as ordinary and only punish flooding.
        score = integrity(0.0, tempo_ratio, False)
        quality = clamp01(UNREADABLE_QUALITY * score)

    discount = 1.0 - SPAM_DISCOUNT * (1.0 - score)
    multiplier = payout_multiplier(quality)
    coins = round((low + (high - low) * quality) * multiplier * discount)
    xp = round(base_xp * multiplier * discount)
    return Payout(coins=max(1, int(coins)), xp=max(1, int(xp)))


class SpamScreen:
    """Bounded per-member message history behind the anti-spam numbers.

    The screen keeps the newest :data:`HISTORY_KEPT` messages per member inside
    :data:`HISTORY_SECONDS`, and reports how much of that history repeats the
    message being judged plus how fast the member is going. It never stores raw
    text — only a short fingerprint — so it is safe to hold in memory.
    """

    def __init__(self, kept: int = HISTORY_KEPT, max_users: int = HISTORY_MAX_USERS):
        self._kept = max(1, int(kept))
        self._max_users = max(1, int(max_users))
        self._history: dict[tuple[int, int], deque[tuple[float, str | None]]] = {}

    def record(
        self, guild_id: int, user_id: int, text: str | None, now: float
    ) -> SpamStats:
        """Add a message to the history and return what it looks like."""
        key = (guild_id, user_id)
        entries = self._history.get(key)
        if entries is None:
            entries = self._history[key] = deque(maxlen=self._kept)

        while entries and now - entries[0][0] > HISTORY_SECONDS:
            entries.popleft()

        fingerprint = text_fingerprint(text) if text else None
        low_signal = bool(text) and len(normalize_text(text)) < 2

        # Compare against the *previous* messages: a member's first message is
        # never a duplicate of itself.
        recent = [entry for entry in entries if now - entry[0] <= DUPLICATE_SECONDS]
        repeats = sum(1 for _, known in recent if known is not None and known == fingerprint)
        duplicate_ratio = (repeats / len(recent)) if recent else 0.0
        tempo_ratio = (len(entries) + 1) / float(TEMPO_ALLOWANCE)

        entries.append((now, fingerprint))

        if len(self._history) > self._max_users:
            self._prune(now)
        return SpamStats(
            duplicate_ratio=duplicate_ratio,
            tempo_ratio=tempo_ratio,
            low_signal=low_signal,
        )

    def _prune(self, now: float) -> None:
        """Drop quiet members, then the least recently seen, until under the cap."""
        for key, entries in list(self._history.items()):
            if not entries or now - entries[-1][0] > HISTORY_SECONDS:
                del self._history[key]
        while len(self._history) > self._max_users:
            oldest = min(
                self._history.items(),
                key=lambda item: item[1][-1][0] if item[1] else 0.0,
            )
            del self._history[oldest[0]]


# --------------------------------------------------------------------------- #
# Voice
# --------------------------------------------------------------------------- #
#: Non-bot humans a channel needs (including the member) before it pays at all.
VC_COMPANY_MIN = 2
#: How long a channel can go with no observable event and still pay full rate.
VC_FULL_SECONDS = 1200.0
#: Past this much total silence in the channel, nobody earns.
VC_DEAD_SECONDS = 21600.0
#: The rate at the edge of that cliff, for a channel that has been quiet a while.
VC_STALE_FLOOR = 0.40
#: A self-muted member is listening, not stuck in a dead client: half rate.
VC_SELF_MUTE_FACTOR = 0.50
#: Being on camera or live is proof of presence, so it lifts the ceiling.
VC_STREAM_BONUS = 1.20
#: Hard ceiling on the whole voice factor.
VC_MAX_FACTOR = 1.25
VC_PULSE_LIMIT = 4096


def voice_presence(
    *,
    self_deaf: bool = False,
    deaf: bool = False,
    mute: bool = False,
    self_mute: bool = False,
    suppress: bool = False,
    self_video: bool = False,
    self_stream: bool = False,
) -> float:
    """``0`` when the member cannot be participating, else a factor around 1.

    Deafened (the client is not even receiving audio), server-muted, or sitting
    in a stage channel's audience are all disqualifying: there is no way that
    member is in the conversation.
    """
    if self_deaf or deaf or mute or suppress:
        return 0.0
    factor = VC_SELF_MUTE_FACTOR if self_mute else 1.0
    if self_video or self_stream:
        factor *= VC_STREAM_BONUS
    return min(VC_MAX_FACTOR, factor)


def voice_company_factor(companions: int) -> float:
    """Worth of having ``companions`` other humans in the channel.

    Zero when alone — a member sitting by themselves is the classic AFK farm —
    then rising logarithmically: a pair is worth 0.78, four people the full 1.0.
    """
    if companions < 1:
        return 0.0
    return 0.55 + 0.45 * min(1.0, math.log(companions + 1) / math.log(4))


def voice_freshness(age_seconds: float) -> float:
    """Rate for a channel whose last observable event was ``age_seconds`` ago."""
    if age_seconds <= VC_FULL_SECONDS:
        return 1.0
    if age_seconds >= VC_DEAD_SECONDS:
        return 0.0
    span = VC_DEAD_SECONDS - VC_FULL_SECONDS
    progress = (age_seconds - VC_FULL_SECONDS) / span
    return VC_STALE_FLOOR + (1.0 - VC_STALE_FLOOR) * (1.0 - progress)


def voice_factor(*, presence: float, company: float, freshness: float) -> float:
    """Combined activity factor: any zero makes the whole minute worthless."""
    present = max(0.0, min(VC_MAX_FACTOR, float(presence or 0.0)))
    factor = present * clamp01(company) * clamp01(freshness)
    if factor <= 0.0:
        return 0.0
    return min(VC_MAX_FACTOR, factor)


def voice_reward(factor: float, *, vc_xp: int, vc_coins: int) -> Payout:
    """Coins and XP for one paid minute of voice, scaled by the activity factor.

    The configured per-minute rates are the *ceiling*, not a flat salary: only a
    busy, fresh, undeafened channel reaches them, and a member who earns anything
    at all earns at least one coin and one XP for that minute.
    """
    factor = max(0.0, min(VC_MAX_FACTOR, float(factor or 0.0)))
    if factor <= 0.0:
        return Payout(coins=0, xp=0)
    coins = max(1, round(int(vc_coins) * factor)) if vc_coins else 0
    xp = max(1, round(int(vc_xp) * factor)) if vc_xp else 0
    return Payout(coins=int(coins), xp=int(xp))


class PulseClock:
    """Newest observable voice event per channel.

    Discord exposes no "who is speaking" signal, so the only evidence of life a
    bot gets is a member joining, leaving, moving, muting, unmuting, deafening,
    going live, or typing in the text channels while sitting in voice. That is
    what this records; a channel with none of it for hours stops paying.
    """

    def __init__(self, limit: int = VC_PULSE_LIMIT):
        self._limit = max(1, int(limit))
        self._pulses: dict[int, float] = {}

    def touch(self, channel_id: int, now: float) -> None:
        self._pulses[int(channel_id)] = float(now)
        if len(self._pulses) > self._limit:
            self.prune(now)

    def age(self, channel_id: int, now: float) -> float:
        """Seconds since the last event, or ``0.0`` for a channel never seen."""
        last = self._pulses.get(int(channel_id))
        if last is None:
            return 0.0
        return max(0.0, float(now) - last)

    def seen(self, channel_id: int, now: float) -> float:
        """Age of the last event, starting the clock for a channel new to us.

        Without this a bot that just restarted would read every occupied channel
        as infinitely stale and stop paying real members until someone happened
        to toggle something.
        """
        if int(channel_id) not in self._pulses:
            self.touch(channel_id, now)
            return 0.0
        return self.age(channel_id, now)

    def forget(self, channel_id: int) -> None:
        self._pulses.pop(int(channel_id), None)

    def prune(self, now: float, keep: float = VC_DEAD_SECONDS) -> None:
        """Drop idle channels, then the oldest, until the clock is inside its cap.

        Age alone is not enough: a long-lived process that sees thousands of
        channels inside one window would hold every one of them.
        """
        for channel_id, last in list(self._pulses.items()):
            if float(now) - last > keep:
                del self._pulses[channel_id]
        if len(self._pulses) > self._limit:
            oldest = sorted(self._pulses.items(), key=lambda item: item[1])
            for channel_id, _ in oldest[: len(self._pulses) - self._limit]:
                del self._pulses[channel_id]

    def __len__(self) -> int:
        return len(self._pulses)
