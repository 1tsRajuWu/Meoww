"""Alone Time: one voice channel, locked to the one person holding it.

The feature reads as a single sentence — *lock this room to me* — and the honest
version of it needs three mechanisms, because the obvious one does not work:

* **Discord's `Administrator` permission and the guild owner bypass channel
  permission overwrites by design.** So `Connect: false` is a locked-looking door
  that an administrator walks through, and a feature that stopped there would be
  a promise this project's users would catch out within a day.
* **The room's capacity is the layer that holds.** ``user_limit`` is enforced at
  Discord's voice gateway for everybody, and no permission bit raises it — set it
  to the number of people allowed inside and the next person is told the channel
  is full, administrator or not. This is what "not even an admin" actually rests
  on.
* **The sweep is the backstop.** An ``on_voice_state_update`` listener puts
  anybody who still ends up inside — the owner, a role-level `Connect` allow,
  somebody who slipped in while the bot was offline — back where they came from,
  or out of voice. It needs **Move Members**, and where Discord refuses the move
  the refusal is *recorded and reported* rather than papered over.

The two layers are in force together and the status card says so, because they
fail differently: with no guests the cap alone turns everybody away instantly,
and a guest list raises the cap to fit the guests, which leaves the sweep doing
the deciding. Saying *"locked, nobody can get in"* and meaning only the second
would be the kind of claim this repository exists to avoid making.

What is deliberately **not** here, so the next reader does not go looking for it:
a per-room expiry timer. A lock ends because its holder released it, because the
holder left (unless the room is set to stay locked), because the server set a
`max_hours` ceiling, because an administrator released it, or because the system
was switched off — never because a clock nobody asked for ran out.

The split is the one ``utils/company.py`` and ``utils/server_control.py`` already
use: the decisions and the sentences are pure and testable without a gateway, and
the engine takes whatever object the caller hands it, so a ``test_`` double is a
channel like any other. ``discord`` is imported inside the functions that write to
it — an import at module scope would make this module unimportable by the very
tests that are supposed to drive it.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

from . import db, features

#: The one switch this module answers to, named once because several gates read
#: it (the cog's ``interaction_check``, every command's refusal, the sweep, the
#: reconcile loop) and a second copy of the string is how they come to disagree.
MODULE = "alonetime"

#: The root command. One slot in Discord's picker for the whole system.
ROOT = "alone"

#: Where a quiet-hours window lives in ``guild_settings``. Two keys rather than a
#: table: it is one fact about one server (until when, asked by whom) and every
#: other per-server switch in this bot is a row in that same table.
SERVER_UNTIL_KEY = "alonetime_server_until"
SERVER_BY_KEY = "alonetime_server_by"

#: What the prefix on a locked room's name is. A locked room is otherwise
#: indistinguishable from an open one in Discord's sidebar, and the name is the
#: only place a member can read it before they try to join.
LOCK_MARK = "🔒 "

#: The three answers the rule can give about a voice-state change. ``SKIP`` is a
#: decision and not a missing one: the bot's own connection and another bot's are
#: both things this feature deliberately does not touch.
ALLOW, EVICT, SKIP = "allow", "evict", "skip"

#: Who a server lets lock a room.
WHO_EVERYONE, WHO_ROLE, WHO_ADMINS = "everyone", "role", "admins"
#: Where a turned-away member goes.
EVICT_ORIGIN, EVICT_DISCONNECT, EVICT_AFK = "origin", "disconnect", "afk"

#: Discord's own ceiling on a voice channel's user limit.
MAX_LIMIT = 99


# --------------------------------------------------------------------------- #
# The server's settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    """One server's choices about Alone Time, as plain values."""

    who: str = WHO_EVERYONE
    role_name: str = ""
    guard: bool = True
    keep: bool = False
    announce: bool = True
    log_cases: bool = True
    evict: str = EVICT_ORIGIN
    cooldown_seconds: int = 60
    max_hours: int = 0
    server_minutes: int = 60

    def role_matches(self, member) -> bool:
        """Whether ``member`` holds the role this server named, by name or by id.

        Both spellings on purpose: a server administrator types a name into the
        dashboard, and an id is what survives a rename — accepting either means
        the setting keeps working when the role is renamed, which is the half of
        the job a name-only match quietly loses.
        """
        wanted = str(self.role_name or "").strip().lower()
        if not wanted:
            return False
        for role in getattr(member, "roles", None) or ():
            if str(getattr(role, "id", "")).strip() == wanted:
                return True
            if str(getattr(role, "name", "")).strip().lower() == wanted:
                return True
        return False

    def may_lock(self, member) -> bool:
        """Whether this member is allowed to start a lock in this server.

        Administrators always may — including under *a role I name*, which is the
        difference between restricting a feature and locking its owner out of it.
        """
        permissions = getattr(member, "guild_permissions", None)
        administrator = bool(getattr(permissions, "administrator", False))
        if self.who == WHO_ADMINS:
            return administrator or bool(getattr(permissions, "manage_channels", False))
        if self.who == WHO_ROLE:
            return administrator or self.role_matches(member)
        return True


def settings_from(raw) -> Settings:
    """A :class:`Settings` from raw stored values, through the registry's rules.

    The fallback rules are ``features.setting_value``'s, not a second copy of
    them, so a value stored out of range reads as the default here exactly as it
    does in every other reader — the drift this bot's registry exists to stop.
    """
    values: dict = {}

    def value(setting_id, default):
        spec = features.get_setting(MODULE, setting_id)
        if spec is None:
            return default
        return features.setting_value(spec, (raw or {}).get(setting_id))

    return Settings(
        who=str(value("who", WHO_EVERYONE) or WHO_EVERYONE),
        role_name=str(value("role_name", "") or ""),
        guard=bool(value("guard", True)),
        keep=bool(value("keep", False)),
        announce=bool(value("announce", True)),
        log_cases=bool(value("log_cases", True)),
        evict=str(value("evict", EVICT_ORIGIN) or EVICT_ORIGIN),
        cooldown_seconds=int(value("cooldown_seconds", 60) or 0),
        max_hours=int(value("max_hours", 0) or 0),
        server_minutes=int(value("server_minutes", 60) or 60),
    )


async def get_settings(guild_id) -> Settings:
    """This server's settings. A failed read is the default, said plainly.

    Every key comes from the registry (:func:`features.settings_for`), so a
    setting added to the card is a setting this reader already answers, and one
    removed from it cannot be read here by accident.
    """
    if not guild_id:
        return Settings()
    raw: dict = {}
    for spec in features.settings_for(MODULE):
        try:
            raw[spec.id] = await db.get_setting(
                guild_id, features.setting_key(MODULE, spec.id), None
            )
        except Exception:  # noqa: BLE001 — a failed read is the default
            raw[spec.id] = None
    return settings_from(raw)


def off_sentence() -> str:
    """The refusal a switched-off system answers with, in the registry's words."""
    feature = features.get(MODULE)
    label = feature.name if feature else MODULE.replace("_", " ").title()
    return (
        f"⚠️ The **{label}** system is currently **turned off** on this server.\n"
        "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard)."
    )


# --------------------------------------------------------------------------- #
# One locked room
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Lock:
    """One locked room, and everything releasing it needs to give it back."""

    guild_id: int
    channel_id: int
    holder_id: int = 0
    holder_name: str = ""
    guests: tuple[int, ...] = ()
    guest_roles: tuple[int, ...] = ()
    note: str = ""
    #: The `@everyone` overwrite as ``"allow:deny"`` — ``""`` means there was none.
    previous: str = ""
    previous_limit: int = 0
    previous_name: str = ""
    keep: bool = False
    turned_away: int = 0
    started_at: float = 0.0
    #: What the room was renamed **to** while locked: the holder's custom name
    #: when they gave one, otherwise ``🔒 <previous_name>``. Empty for a row
    #: written before the column existed, which :func:`restore_name` falls back
    #: for. It is *what we set* rather than something derived from the old name,
    #: because only a name this feature put there may be taken back — a custom
    #: name released against the computed ``🔒 old`` would leave the room wearing
    #: the lock's name for ever.
    locked_as: str = ""

    @property
    def server_wide(self) -> bool:
        """Whether this is a quiet-hours lock rather than somebody's own room."""
        return not self.holder_id

    @property
    def holder_label(self) -> str:
        return self.holder_name or (f"member {self.holder_id}" if self.holder_id else "nobody")

    def admits(self, member_id, role_ids=(), *, bot=False, bot_id=0) -> bool:
        """Whether this room lets that member in — :func:`verdict`'s answer."""
        return verdict(
            holder_id=self.holder_id,
            guests=self.guests,
            guest_roles=self.guest_roles,
            member_id=member_id,
            role_ids=role_ids,
            bot=bot,
            bot_id=bot_id,
        ) == ALLOW

    def held_seconds(self, *, now=None) -> int:
        return max(0, int((now if now is not None else time.time()) - self.started_at))


def from_row(row) -> Lock:
    """A :class:`Lock` from one stored row (the shape ``utils.db`` answers with)."""
    return Lock(
        guild_id=int(row.get("guild_id") or 0),
        channel_id=int(row.get("channel_id") or 0),
        holder_id=int(row.get("holder_id") or 0),
        holder_name=str(row.get("holder_name") or ""),
        guests=tuple(int(value) for value in (row.get("guests") or ())),
        guest_roles=tuple(int(value) for value in (row.get("guest_roles") or ())),
        note=str(row.get("note") or ""),
        previous=str(row.get("previous") or ""),
        previous_limit=int(row.get("previous_limit") or 0),
        previous_name=str(row.get("previous_name") or ""),
        locked_as=str(row.get("locked_as") or ""),
        keep=bool(row.get("keep")),
        turned_away=int(row.get("turned_away") or 0),
        started_at=float(row.get("started_at") or 0),
    )


def verdict(
    *,
    holder_id: int,
    guests: Iterable[int] = (),
    guest_roles: Iterable[int] = (),
    member_id: int,
    role_ids: Iterable[int] = (),
    bot: bool = False,
    bot_id: int = 0,
) -> str:
    """What should happen to somebody arriving in a locked room. Pure and total.

    Three answers, and the two ``SKIP`` cases are decisions rather than gaps: the
    bot's own connection (which may be a radio in the same room) is never this
    feature's business, and neither is another bot's. Every other arrival that is
    not the holder, a named guest, or a holder of a guest role is turned away.
    """
    if bot or (bot_id and int(member_id) == int(bot_id)):
        return SKIP
    if holder_id and int(member_id) == int(holder_id):
        return ALLOW
    if int(member_id) in {int(value) for value in guests or ()}:
        return ALLOW
    if {int(value) for value in role_ids or ()} & {int(value) for value in guest_roles or ()}:
        return ALLOW
    return EVICT


def capacity_for(guild, guests=(), guest_roles=()) -> int:
    """How many people the room's cap allows in: the holder, the guests, and a seat
    per member of each guest role.

    A guest role is counted rather than guessed, because the cap has to be raised
    *before* a guest arrives (nobody can be let past a full room), and a role whose
    members cannot be read is given one seat so the room never locks a guest out
    entirely. Clamped to Discord's own ceiling: a room with more guests than 99 is
    a room this layer cannot describe, and the sweep is what it leans on then.
    """
    slots = 1 + len(tuple(guests or ()))
    for role in guest_roles or ():
        members = getattr(role, "members", None)
        slots += max(1, len(members)) if members is not None else 1
    return max(1, min(MAX_LIMIT, slots))


def strays_in(channel, lock: Lock, *, bot_id: int = 0) -> list:
    """Everyone standing in a locked room the lock does not admit.

    The sweep answers *arrivals*, and an arrival is not the only way somebody is
    inside a room: a lock starts under two people, a restart leaves a room
    locked with its holder gone, and Discord's own Administrator permission
    walks through overwrites by design. So a lock is followed by one pass over
    the room as it is — the same rule the sweep applies, asked once about the
    members Discord is already reporting. Bots are skipped for the sweep's own
    reason: another bot's connection is not this feature's business.
    """
    found = []
    for member in getattr(channel, "members", None) or ():
        if getattr(member, "bot", False):
            continue
        member_id = int(getattr(member, "id", 0) or 0)
        if bot_id and member_id == int(bot_id):
            continue
        role_ids = [
            int(getattr(role, "id", 0) or 0) for role in getattr(member, "roles", None) or ()
        ]
        if lock.admits(member_id, role_ids, bot_id=bot_id):
            continue
        found.append(member)
    return found


# --------------------------------------------------------------------------- #
# The ledger, and putting a room back
# --------------------------------------------------------------------------- #
def encode_overwrite(overwrite) -> str:
    """An overwrite as the two numbers the ledger stores (``""`` = there was none).

    ``""`` and ``"0:0"`` are different answers — *no overwrite* and *an overwrite
    that says nothing* — and the release path has to be able to tell them apart,
    because one is restored by deleting and the other by writing.
    """
    if overwrite is None:
        return ""
    allow = getattr(overwrite, "allow", None)
    deny = getattr(overwrite, "deny", None)
    return f"{int(getattr(allow, 'value', 0) or 0)}:{int(getattr(deny, 'value', 0) or 0)}"


def decode_overwrite(text) -> tuple[int, int] | None:
    """``(allow, deny)`` out of a stored ledger entry, or ``None`` for no overwrite."""
    if not text:
        return None
    allow, _, deny = str(text).partition(":")
    try:
        return int(allow or 0), int(deny or 0)
    except ValueError:
        return None


def locked_name(name: str) -> str:
    """The room's name while it is locked, inside Discord's own length limit."""
    text = str(name or "").strip()
    if text.startswith(LOCK_MARK):
        return text[:100]
    return (LOCK_MARK + text)[:100]


def restore_name(previous: str, current: str, locked_as: str = "") -> str | None:
    """The name to put back on release, or ``None`` to leave the room alone.

    Only restores a name this feature set: the room has to still be called what
    the lock renamed it to — *what the lock actually set*, which is
    ``locked_as`` when it is there and the computed ``🔒 <previous>`` for rows
    written before that column existed. A room somebody has renamed since is
    their decision and not ours to undo, which is the same rule
    ``utils/server_control`` applies to the overwrites it restores.
    """
    previous = str(previous or "")
    if not previous:
        return None
    was = str(locked_as or "") or locked_name(previous)
    if str(current or "") != was:
        return None
    if str(current or "") == previous:
        return None
    return previous


def release_reason(
    lock: Lock,
    *,
    settings: Settings,
    holder_present=None,
    channel_exists: bool = True,
    now=None,
) -> str:
    """Why this lock should lift by itself, or ``""`` when it should not.

    Pure, and deliberately narrow: three reasons, each one a way a room is left
    locked with nobody left to want it. ``holder_present`` is ``None`` when the
    answer is unknown (a cold cache, a voice state that could not be read), and
    unknown never releases — a lock that lifted itself because a reading failed
    would be worse than one that is a moment late.
    """
    if not channel_exists:
        return "the room is gone"
    if lock.server_wide:
        return ""  # a quiet-hours window ends by its clock, not per room
    hours = int(settings.max_hours or 0)
    if hours and lock.started_at and (now or time.time()) - lock.started_at >= hours * 3600:
        return f"this server's ceiling of {hours}h passed"
    if not (lock.keep or settings.keep) and holder_present is False:
        return "the holder left the voice channel"
    return ""


def eviction_target(choice: str, *, origin, afk):
    """Which room a turned-away member is put in, or ``None`` to disconnect them.

    Pure, so the three answers are testable without a guild. *Back where they came
    from* is the default because it is the only one that leaves somebody connected
    to voice — a member who hopped between two rooms is put back in the one they
    were sitting in, rather than dropped out of the call entirely for a mistake
    about a room.
    """
    if choice == EVICT_ORIGIN and origin is not None:
        return origin
    if choice == EVICT_AFK and afk is not None and afk is not origin:
        return afk
    return None


def duration_words(seconds: int) -> str:
    """A span of time as a person says it, for a card that has one line for it."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours, rest = divmod(seconds, 3600)
    if rest < 60:
        return f"{hours}h"
    return f"{hours}h {rest // 60}m"


def status_lines(lock: Lock, settings: Settings, *, now=None, guard_ready=True) -> list[tuple[str, str]]:
    """The facts under a room's status card, as ``(label, value)`` pairs.

    One function so the command, the console and the tests read the same reading —
    and so the *"which layers are in force"* sentence is written once, which is the
    part a second reader would get wrong: the cap and the sweep are not the same
    promise, and this is where a card has to be honest about which one it holds.
    """
    layers = []
    if settings.guard:
        layers.append("Move anybody out who still gets in" if guard_ready
                      else "Move-out is switched on, but I lack **Move Members**")
    else:
        layers.append("Move-out is switched **off**")
    holder = lock.holder_label if not lock.server_wide else "nobody — quiet hours"
    guests = len(lock.guests) + len(lock.guest_roles)
    rows = [
        ("Holder", holder),
        ("Held for", duration_words(lock.held_seconds(now=now)) if lock.started_at else "—"),
        ("Capacity", "the people allowed in, and nobody else"),
        ("Guests", f"{len(lock.guests)} member(s), {len(lock.guest_roles)} role(s)" if guests else "none"),
        ("In force", " · ".join(layers)),
        ("Turned away", str(lock.turned_away)),
        # The honest answer to the one request this feature cannot grant. Activity
        # sharing — whether a member's voice activity appears on their profile — is
        # a **per-member Discord setting**, not a channel permission: there is no
        # bot API for it, and the only server-side lever is hiding the channel from
        # the viewer entirely, which would hide the room from everybody. Saying so
        # on the card is the difference between a promise and a policy.
        (
            "Privacy",
            "Whether your voice activity shows on your profile is your own Discord "
            "setting (Activity Sharing) — no bot can change it, and this does not try.",
        ),
    ]
    if lock.note:
        rows.insert(1, ("Why", lock.note[:200]))
    return rows


# --------------------------------------------------------------------------- #
# The cache — one row set, read by two threads
# --------------------------------------------------------------------------- #
#: ``guild_id -> {channel_id: Lock}``. The bot's voice listener asks about every
#: arrival and departure in every server, and a SQLite read per event is a query
#: per member per hop — so the rows are read once and dropped on every write.
#: ``utils/modules.py`` is the shape this follows, for the same reason: the Flask
#: thread and the gateway's loop are the same process here, and two caches would
#: be two answers about one room.
CACHE: dict[int, dict[int, Lock]] = {}
_cache_lock = threading.RLock()


def invalidate(guild_id=None) -> None:
    """Drop one server's locked rooms, or the whole cache after a write."""
    with _cache_lock:
        if guild_id is None:
            CACHE.clear()
            return
        CACHE.pop(int(guild_id), None)


def cached(guild_id) -> dict[int, Lock]:
    """Every locked room in one server, from the cache, loading it if needed.

    A failed read answers ``{}`` rather than raising: the listener runs on the
    gateway's own loop, and a database hiccup there would take a voice event down
    with it — while an empty set is exactly the safe answer, since nothing is
    released and nothing is evicted on the strength of it.
    """
    key = int(guild_id or 0)
    if not key:
        return {}
    with _cache_lock:
        found = CACHE.get(key)
        if found is not None:
            return found
    try:
        rows = db.read_alone_time_rows_sync(key)
    except Exception:  # noqa: BLE001 — an unreadable table is not a crash
        rows = []
    loaded = {int(row["channel_id"]): from_row(row) for row in rows}
    with _cache_lock:
        CACHE[key] = loaded
        return loaded


def lock_for(guild_id, channel_id) -> Lock | None:
    """The lock on one room, or ``None``."""
    return cached(guild_id).get(int(channel_id or 0))


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
@dataclass
class Report:
    """What a lock action did, in numbers a card can print."""

    action: str
    ok: bool = False
    detail: str = ""
    channel: str = ""
    changed: int = 0
    skipped: int = 0
    capacity: int = 0
    #: True when the sweep's own permission is missing, which is not a failure of
    #: the lock — it is the half of the promise that will not run.
    guard_missing: bool = False
    #: Set when the room's *name* could not be changed, or put back, while the
    #: lock or the release itself applied. Decoration is not the lock, so the two
    #: are carried apart — a card can then say "the room is open, but its name is
    #: still the one the lock gave it" instead of claiming a failure.
    name_warning: str = ""
    failures: list = field(default_factory=list)

    def sentence(self) -> str:
        if not self.ok:
            return self.detail or "That did not happen."
        if self.action == "lock":
            said = f"🔒 **{self.channel}** is locked to you."
            if self.capacity:
                said += (
                    f" Room capacity is **{self.capacity}** — Discord refuses everybody else, "
                    "administrator or not."
                )
            if self.detail:
                said += f" {self.detail}"
            if self.name_warning:
                said += f" {self.name_warning}"
            return said
        if self.action == "release":
            said = self.detail or f"🔓 **{self.channel}** is back the way it was."
            if self.name_warning:
                said += f" {self.name_warning}"
            return said
        if self.action == "server":
            if not self.changed and self.skipped:
                said = f"🔇 Every voice channel was already locked (**{self.skipped}** of them)."
            else:
                said = f"🔇 Quiet hours: **{self.changed}** voice channel(s) locked."
                if self.skipped:
                    said += f" {self.skipped} were already locked."
                if self.failures:
                    said += f" {len(self.failures)} could not be managed."
            return said + self.detail
        if self.action == "evict":
            return self.detail or "Moved them out."
        return self.detail or "Done."

    def to_dict(self) -> dict:
        return {
            "success": self.ok,
            "action": self.action,
            "channel": self.channel,
            "changed": self.changed,
            "skipped": self.skipped,
            "capacity": self.capacity,
            "guard_missing": self.guard_missing,
            "name_warning": self.name_warning,
            "failures": [
                {"channel": str(channel), "detail": detail}
                for channel, detail in self.failures
            ],
            "said": self.sentence(),
        }


def _name(thing) -> str:
    return str(getattr(thing, "name", thing) or "?")


def _everyone(guild):
    return getattr(guild, "default_role", None)


def _everyone_overwrite(channel):
    """The `@everyone` overwrite on a channel, or ``None`` if there is not one."""
    overwrites = getattr(channel, "overwrites", None)
    if overwrites is None:
        return None
    guild = getattr(channel, "guild", None)
    everyone = _everyone(guild) if guild is not None else None
    if everyone is None:
        return None
    try:
        return overwrites.get(everyone)
    except Exception:  # noqa: BLE001 — a mapping that cannot be asked is not one
        return None


def _manageable(channel) -> tuple[bool, str]:
    """Whether this bot may change this room's permissions.

    Checked rather than attempted, because a failed write here leaves a room that
    looks locked and is not — the one outcome this feature cannot afford.
    """
    guild = getattr(channel, "guild", None)
    me = getattr(guild, "me", None)
    if me is None:
        return True, ""  # a stand-in with no member object: try it
    permissions = getattr(channel, "permissions_for", None)
    if not callable(permissions):
        return True, ""
    try:
        granted = permissions(me)
    except Exception:  # noqa: BLE001
        return True, ""
    if getattr(granted, "manage_channels", False):
        return True, ""
    return False, "Manage Channels"


def guard_ready(guild) -> bool:
    """Whether the bot holds **Move Members**, which the sweep cannot work without."""
    me = getattr(guild, "me", None)
    permissions = getattr(me, "guild_permissions", None)
    if permissions is None:
        return True  # a stand-in: not evidence that it is missing
    return bool(getattr(permissions, "move_members", False))


def _reason(actor, prefix: str) -> str:
    """The audit reason Discord shows, naming whoever asked.

    ``actor`` is a person's name as a string from the cogs (they already know how
    to name a member) and may be an object with a ``label`` from anywhere else, so
    both spellings work rather than one caller having to invent a wrapper for the
    other.
    """
    if isinstance(actor, str):
        label = actor.strip()
    else:
        label = str(getattr(actor, "label", "") or getattr(actor, "name", "") or "").strip()
    return f"{prefix} by {label or 'somebody'}"


async def lock_channel(
    guild,
    channel,
    holder=None,
    *,
    settings: Settings | None = None,
    guests: Iterable[int] = (),
    guest_roles=(),
    note: str = "",
    name: str = "",
    keep=None,
    actor=None,
) -> Report:
    """Lock one voice channel to ``holder`` (or to nobody, for quiet hours).

    Order matters and is the whole safety story: the room's previous state is
    written down **before** the first permission change, because a lock that
    applied and failed to record itself has no way back — the same rule
    ``utils/server_control.lockdown`` follows for the same reason. If the writes
    then fail, the row is dropped again so nothing is left claiming a room is
    locked when it is not.

    The **name is a separate, best-effort step** and never rolls the lock back:
    Discord rate-limits a channel rename to about two per ten minutes, so a 429
    there is ordinary — and while it shared the permission write's ``try`` it
    un-wrote the ledger row and answered "nothing happened" for a room that was
    in fact locked, leaving nobody able to release it. A refused rename is
    reported on the card (:attr:`Report.name_warning`) and changes nothing else.
    """
    import discord  # imported here: see the module docstring

    settings = settings or Settings()
    report = Report(action="lock", channel=_name(channel))
    everyone = _everyone(guild)
    if everyone is None:
        report.detail = "That server has no default role for me to change."
        return report
    ok, why = _manageable(channel)
    if not ok:
        report.detail = (
            f"I need **{why}** in **{_name(channel)}** before I can lock it. "
            "Give me that permission and try again."
        )
        return report

    guest_ids = tuple(int(value) for value in guests or ())
    role_objects = tuple(guest_roles or ())
    capacity = capacity_for(guild, guest_ids, role_objects)
    previous = encode_overwrite(_everyone_overwrite(channel))
    previous_limit = int(getattr(channel, "user_limit", 0) or 0)
    previous_name = str(getattr(channel, "name", "") or "")
    #: What the room is renamed to: the holder's own word for it, or the old name
    #: wearing the lock's mark. Computed before the ledger write because the row
    #: is what a release compares the room's name against.
    marker = locked_name(str(name or "").strip() or previous_name)
    holder_id = int(getattr(holder, "id", 0) or 0)
    holder_name = str(
        getattr(holder, "display_name", None) or getattr(holder, "name", "") or ""
    )

    try:
        await db.save_alone_time(
            getattr(guild, "id", 0),
            getattr(channel, "id", 0),
            holder_id=holder_id,
            holder_name=holder_name,
            guests=guest_ids,
            guest_roles=tuple(int(getattr(role, "id", 0) or 0) for role in role_objects),
            note=note,
            previous=previous,
            previous_limit=previous_limit,
            previous_name=previous_name,
            locked_as=marker,
            keep=bool(settings.keep if keep is None else keep),
            started_at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001 — no ledger, no lock
        report.detail = (
            f"I could not write down what to undo, so nothing was changed ({type(exc).__name__})."
        )
        return report

    try:
        await channel.set_permissions(
            everyone,
            overwrite=discord.PermissionOverwrite(connect=False),
            reason=_reason(actor, "Alone Time lock"),
        )
        await channel.edit(
            user_limit=capacity,
            reason=_reason(actor, "Alone Time capacity"),
        )
    except Exception as exc:  # noqa: BLE001 — one refusal, one sentence
        await db.forget_alone_time(getattr(guild, "id", 0), [getattr(channel, "id", 0)])
        invalidate(getattr(guild, "id", 0))
        report.detail = (
            f"Discord refused the change ({type(exc).__name__}), so the room is "
            "open and nothing was written down."
        )
        return report

    # The rename, on its own — see the docstring. A refusal here is decoration
    # lost, not a lock lost, so the row stays and the lock stands.
    if marker != previous_name:
        try:
            await channel.edit(name=marker, reason=_reason(actor, "Alone Time name"))
        except Exception as exc:  # noqa: BLE001 — decoration, not the lock
            report.name_warning = (
                "The room is locked, but Discord would not take its new name just "
                f"now ({type(exc).__name__}) — it limits renames to about two per ten "
                "minutes, so the lock stands and the name can wait."
            )

    invalidate(getattr(guild, "id", 0))
    report.ok = True
    report.capacity = capacity
    report.guard_missing = not guard_ready(guild)
    if report.guard_missing and settings.guard:
        report.detail = (
            "⚠️ I do not have **Move Members** here, so I cannot turn away anybody "
            "who gets past the locked door once the room is empty. The capacity "
            "still holds while you are inside it."
        )
    return report


async def release_channel(guild, lock: Lock, *, actor=None, reason: str = "") -> Report:
    """Put one room back exactly the way the ledger found it, and forget the row."""
    import discord

    report = Report(action="release", channel=str(lock.channel_id))
    getter = getattr(guild, "get_channel", None)
    channel = getter(int(lock.channel_id)) if callable(getter) else None
    if channel is None:
        report.ok = True
        report.detail = "That room no longer exists, so there was nothing to restore."
        await db.forget_alone_time(getattr(guild, "id", 0), [lock.channel_id])
        invalidate(getattr(guild, "id", 0))
        return report
    report.channel = _name(channel)
    everyone = _everyone(guild)
    if everyone is None:
        report.detail = "That server has no default role for me to change."
        return report

    parsed = decode_overwrite(lock.previous)
    try:
        if parsed is None:
            # There was no overwrite: deleting is the restore. Allowing it instead
            # would leave a permission this server never granted — the exact
            # mistake ``utils/server_control`` documents for its own undo.
            if _everyone_overwrite(channel) is not None:
                await channel.set_permissions(
                    everyone, overwrite=None,
                    reason=_reason(actor, "Alone Time released"),
                )
        else:
            allow, deny = parsed
            await channel.set_permissions(
                everyone,
                overwrite=discord.PermissionOverwrite.from_pair(
                    discord.Permissions(allow), discord.Permissions(deny)
                ),
                reason=_reason(actor, "Alone Time released"),
            )
        await channel.edit(
            user_limit=int(lock.previous_limit or 0),
            reason=_reason(actor, "Alone Time capacity restored"),
        )
    except Exception as exc:  # noqa: BLE001
        report.detail = (
            f"Discord refused the restore in **{report.channel}** "
            f"({type(exc).__name__}) — I need **Manage Channels** there."
        )
        return report

    # The name goes back on its own, for the reason the lock's rename does: a
    # 429 on this edit must not leave the room looking locked with a row nobody
    # can find. The row is forgotten whatever the name does.
    wanted = restore_name(
        lock.previous_name, getattr(channel, "name", ""), getattr(lock, "locked_as", "")
    )
    if wanted:
        try:
            await channel.edit(name=wanted, reason=_reason(actor, "Alone Time name restored"))
        except Exception as exc:  # noqa: BLE001 — decoration, not the release
            report.name_warning = (
                "The room is open again, but its name is still the one the lock "
                f"gave it ({type(exc).__name__}) — Discord limits renames to about "
                "two per ten minutes, so rename it by hand if it matters."
            )

    await db.forget_alone_time(getattr(guild, "id", 0), [lock.channel_id])
    invalidate(getattr(guild, "id", 0))
    report.ok = True
    report.detail = (
        f"🔓 **{report.channel}** is back the way it was"
        + (f" — {reason}." if reason else ".")
    )
    return report


async def write_guests(guild_id, channel_id, lock: Lock, *, guests=(), guest_roles=()) -> Lock:
    """Save a room's guest list, and raise its cap to fit them.

    The cap is raised here rather than on arrival because nobody can be let past a
    full room: a guest who cannot get in past the capacity is a guest list that
    does not work. A cap that is already high enough is left alone, so a guest list
    that shrinks back to nothing leaves the strongest cap in place.
    """
    await db.write_alone_guests(guild_id, channel_id, guests, guest_roles)
    invalidate(guild_id)
    updated = await db.alone_time_row(guild_id, channel_id)
    return from_row(updated) if updated else lock


async def rename_locked(guild, channel, lock: Lock, name: str, *, actor=None) -> Report:
    """Give a locked room a name of the holder's choosing, best-effort.

    Same rule as the lock's own rename, plus the one thing that makes a custom
    name releasable at all: ``locked_as`` is written **after** the edit takes, so
    a release compares the room against the name it is actually wearing. Written
    before, a refused rename would leave the ledger claiming a name the room never
    had and the release would skip the restore — the room would keep a name the
    lock gave it for ever. A refusal here changes nothing about the lock itself.
    """
    report = Report(action="rename", channel=_name(channel))
    current = str(getattr(channel, "name", "") or "")
    marker = locked_name(str(name or "").strip() or current)
    if marker == current:
        report.ok = True
        report.detail = f"It is already called **{marker}**."
        return report
    try:
        await channel.edit(
            name=marker, reason=_reason(actor, "Alone Time renamed by the holder")
        )
    except Exception as exc:  # noqa: BLE001 — decoration, not the lock
        report.detail = (
            f"Discord would not take the new name ({type(exc).__name__}) — it limits "
            "renames to about two per ten minutes, so the room is still yours and the "
            "name can wait."
        )
        return report
    try:
        await db.set_alone_locked_as(getattr(guild, "id", 0), lock.channel_id, marker)
    except Exception as exc:  # noqa: BLE001 — the name took; the ledger is a note
        report.name_warning = (
            f"The room is called **{marker}** now, but I could not write that down "
            f"({type(exc).__name__}) — it may not go back to its old name on release."
        )
    invalidate(getattr(guild, "id", 0))
    report.ok = True
    report.detail = f"✏️ **{marker}** is what this room is called for the rest of the session."
    return report


async def evict(guild, member, target, *, lock: Lock, actor=None) -> Report:
    """Move one member out of a locked room — back to ``target``, or out of voice.

    Discord's answer is the report. A refusal is not raised and not swallowed: the
    holder is told which member Discord would not move, because a lock that
    silently does nothing about one person is worse than one that says so.
    """
    report = Report(action="evict", channel=str(lock.channel_id))
    who = str(getattr(member, "display_name", None) or getattr(member, "name", "") or member)
    try:
        await member.move_to(target, reason="Alone Time — this room is held by somebody else")
    except Exception as exc:  # noqa: BLE001 — one member must not stop the room
        report.detail = (
            f"Discord would not move **{who}** out of **{_name(target) if target else 'that room'}** "
            f"({type(exc).__name__}) — they are above me in the role list, so give my "
            "role a higher position if this has to hold against them."
        )
        return report
    total = await db.bump_alone_turned_away(getattr(guild, "id", 0), lock.channel_id)
    invalidate(getattr(guild, "id", 0))
    report.ok = True
    report.changed = total
    report.detail = (
        f"↩️ **{who}** was moved "
        + (f"back to {_name(target)}." if target is not None else "out of voice.")
    )
    return report


def _is_voice(channel) -> bool:
    """Whether this is a room a quiet-hours window applies to.

    Read as a string rather than by importing the enum, so a stand-in in a test is
    a channel like any other — the same trick ``utils/server_control`` uses to tell
    text from voice. Stages are excluded: a stage is a performance, and stopping
    one is not what "quiet hours" means.
    """
    name = str(getattr(channel, "type", "")).lower()
    if "stage" in name:
        return False
    return "voice" in name


def voice_targets(guild) -> list:
    """Every voice channel a quiet-hours window would lock, in declaration order."""
    channels = list(getattr(guild, "voice_channels", None) or [])
    afk = getattr(guild, "afk_channel", None)
    afk_id = getattr(afk, "id", None)
    return [
        channel for channel in channels
        if _is_voice(channel) and getattr(channel, "id", None) != afk_id
    ]


async def lock_all_voice(guild, *, minutes: int, settings: Settings | None = None, actor=None) -> Report:
    """Lock every voice channel for a window — the server-wide quiet hours.

    Reuses :func:`lock_channel` per room, so a quiet-hours lock *is* an Alone Time
    lock: same ledger, same cap, same release, same sweep. A room already locked by
    somebody is counted as skipped rather than re-locked, which is what keeps one
    member's room from being handed back by the server's window when it ends.
    """
    settings = settings or Settings()
    report = Report(action="server")
    minutes = max(1, min(24 * 60, int(minutes or 0)))
    for channel in voice_targets(guild):
        if lock_for(getattr(guild, "id", 0), getattr(channel, "id", 0)) is not None:
            report.skipped += 1
            continue
        result = await lock_channel(guild, channel, None, settings=settings, actor=actor)
        if result.ok:
            report.changed += 1
        else:
            report.failures.append((_name(channel), result.detail))
    # Locked *something* is a success, and so is *everything was already locked*:
    # a window that finds the server quiet has done its job. Nothing changed at all
    # is the refusal, and it is the permission that is missing.
    report.ok = bool(report.changed or report.skipped)
    if not report.ok:
        report.detail = "No voice channel could be locked — I need **Manage Channels** in them."
    if report.ok:
        await db.set_setting(
            getattr(guild, "id", 0), SERVER_UNTIL_KEY, time.time() + minutes * 60
        )
        await db.set_setting(
            getattr(guild, "id", 0), SERVER_BY_KEY, int(getattr(actor, "id", 0) or 0)
        )
    return report


async def server_window_end(guild_id) -> float:
    """When this server's quiet-hours window ends, or ``0.0`` for no window."""
    try:
        stored = await db.get_setting(guild_id, SERVER_UNTIL_KEY, 0)
        return float(stored or 0)
    except Exception:  # noqa: BLE001
        return 0.0


async def release_server_window(guild, *, actor=None, reason: str = "") -> Report:
    """End a quiet-hours window: restore every room it holds, and forget the clock."""
    report = Report(action="release")
    rows = [from_row(row) for row in db.read_alone_time_rows_sync(getattr(guild, "id", 0))]
    for lock in rows:
        if not lock.server_wide:
            continue  # somebody's own room is not the window's to give back
        result = await release_channel(guild, lock, actor=actor, reason=reason)
        if result.ok:
            report.changed += 1
        else:
            report.failures.append((str(lock.channel_id), result.detail))
    try:
        await db.clear_setting(getattr(guild, "id", 0), SERVER_UNTIL_KEY)
        await db.clear_setting(getattr(guild, "id", 0), SERVER_BY_KEY)
    except Exception as exc:  # noqa: BLE001 — the rooms are back; the clock is bookkeeping
        print(f"[ALONE] could not clear the quiet-hours clock: {exc}", flush=True)
    report.ok = report.changed > 0 or not report.failures
    report.detail = (
        f"🔊 Quiet hours are over — **{report.changed}** room(s) restored."
        if report.changed else "No quiet-hours room was left to restore."
    )
    return report


async def release_by_channel_id(guild, channel_id, *, actor=None) -> Report:
    """Release one room by id, from a surface that only has an id (the dashboard)."""
    row = await db.alone_time_row(getattr(guild, "id", 0), channel_id)
    if row is None:
        return Report(
            action="release", ok=True,
            detail="That room is not locked any more — nothing to give back.",
        )
    return await release_channel(guild, from_row(row), actor=actor)


async def release_all(guild, *, actor=None, reason: str = "", only_server_wide: bool = False) -> Report:
    """Lift every lock this system holds in one server (the off switch, mostly)."""
    report = Report(action="release")
    rows = [from_row(row) for row in db.read_alone_time_rows_sync(getattr(guild, "id", 0))]
    for lock in rows:
        if only_server_wide and not lock.server_wide:
            continue
        result = await release_channel(guild, lock, actor=actor, reason=reason)
        if result.ok:
            report.changed += 1
        else:
            report.failures.append((str(lock.channel_id), result.detail))
    report.ok = report.changed > 0 or not report.failures
    if not report.ok:
        report.detail = "Discord refused the restore — I need **Manage Channels** in those rooms."
    return report


def locked_reading(guild_id, *, now=None) -> list[dict]:
    """Every locked room in one server as plain rows, for a page or a panel.

    Synchronous on purpose: the dashboard renders it, and the dashboard cannot
    await. Ids and counts only — the reading is published, and this project's
    published readings carry no member names (see ``utils/figures``).
    """
    reading = []
    for channel_id, lock in sorted(cached(guild_id).items()):
        reading.append({
            "channel_id": str(channel_id),
            "holder_id": str(lock.holder_id or ""),
            "server_wide": lock.server_wide,
            "guests": len(lock.guests) + len(lock.guest_roles),
            "held_seconds": lock.held_seconds(now=now),
            "turned_away": lock.turned_away,
            "started_at": lock.started_at,
        })
    return reading
