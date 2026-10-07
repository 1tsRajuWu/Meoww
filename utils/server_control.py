"""Whole-server actions, with their own undo.

`/lock` and `/unlock` have existed per channel for a while, and this module is
where they live now: a per-channel lock writes the same ledger, one row at a
time, which is what lets one channel be opened by *restoring* what it had rather
than by allowing `@everyone` to talk. This module is also the *server-wide*
version — the thing an administrator reaches for in an incident — and it is
written around one rule that the per-channel command never had to answer: **a
bulk permission change needs its own undo, and the undo has to be the server's,
not mine.**

A lockdown denies `Send Messages` (and thread creation, and reactions) for
`@everyone` in every channel the bot can manage. The naive undo is to allow it
again, and that is wrong on exactly the servers where it matters: a server that
had deliberately locked one channel before the incident would have that channel
*opened* by the undo — the lockdown's own reversal handing out a permission
nobody asked for. So every channel the lockdown touches is written to
``lockdown_state`` first, with the overwrite that was there before (or the fact
that there was none), and :func:`unlock` restores exactly that.

Three more decisions, each a way this feature is annoying when it is written
carelessly:

* **Report, don't iterate blindly.** :func:`plan` is pure and returns what would
  change, what is already locked, and what the bot cannot manage — so the panel
  can say *"14 channels, 2 already locked, 3 I cannot touch"* **before** anything
  happens, and the count is the same number the lockdown then applies.
* **Nothing here is a replacement for a permission.** The bot needs **Manage
  Channels** for the channel it is changing, and a channel it cannot manage is
  reported as skipped with the reason rather than silently left open.
* **A permission cannot stop an administrator, so a message is removed instead.**
  Discord's `Administrator` permission and the guild owner bypass channel
  overwrites by design, which is how a channel this bot had *already locked* still
  had an administrator talking in it. The ledger is therefore read by a second
  caller — the force-delete listener in ``cogs/server_control.py`` — and a locked
  channel is held by the two together: the deny is what reads as locked, and
  anything posted anyway is deleted, whoever posted it, until the row is gone.
  The bot's own messages stay, because the card that says the channel is locked is
  one of them.
* **A lock older than the ledger can still be recognised.** Every `/lockdown`
  before this module wrote a permission and nothing else, which left channels that
  are locked and unrecorded — a deny the enforcement walks straight past, because
  the ledger is what it reads. :func:`adopt_legacy_locks` writes the missing row,
  but only for the locks Discord's audit log proves are **this bot's own**: a
  channel an administrator denied by hand is staff talking among themselves, and a
  sweep that started deleting their messages and timing them out would be this
  feature mistaking somebody else's decision for its own. Which *kind* of lock it
  was is read from the find as well: the old server-wide command left the same
  overwrite on every text channel and nothing else does, so a guild whose every
  manageable channel is holding the shape is adopted as one server-wide lockdown
  — the kind that leaves administrators and the owner talking.
* **The deny has to reach the overwrites somebody was let in through.** Discord
  applies a role's and a member's channel overwrite **after** `@everyone`'s, so
  an allow there beats the deny: a lock that denies `@everyone` alone is a lock on
  paper on any channel a person has been granted access to — which is every
  channel somebody was ever let into, private ones included. A lock therefore
  denies its permissions on *every* overwrite that grants them, and the ledger
  records each one's old value so the undo puts back exactly what the lock took.
  It **merges** `@everyone`'s overwrite rather than replacing it, too: a
  replacement says nothing about the bits it does not mention, and a lockdown
  once handed a channel back to the whole server by leaving `View Channel` out of
  the overwrite it wrote.
* **A lock can also be asked to hide.** ``hide`` denies `View Channel` on the way
  past, which is what makes a channel *private* rather than merely quiet, and it
  spares the overwrites of the people who can open it again — whoever asked, and
  any role holding **Manage Channels**. Their sight is spared and their messages
  are not: an ordinary lock still holds them, because "nobody can talk" is what a
  lock means.
* **Slowmode is not a lockdown.** It is offered because it is what people
  actually want during a raid — the server stays readable, nobody can flood it —
  and it is a per-channel *value* rather than a permission, so it is written
  straight to ``slowmode_delay`` with the same plan/apply split.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable

import discord

# ``moderation_actions`` is imported for its `Actor` rather than for a function:
# the same object the member panel builds is what names the person in a
# lockdown's audit reason (``utils.moderation_actions.Actor``). Annotating it is
# what keeps the two panels on one definition of "who asked" instead of two
# duck-typed shapes that agree until one of them changes.
from . import db, moderation_actions

#: The permissions a lockdown removes from `@everyone`. Send Messages is the
#: point; the other two are what the same flood looks like in a thread and as a
#: wall of reactions, and leaving them on is how a "locked" server keeps moving.
LOCKED_PERMISSIONS: tuple[str, ...] = ("send_messages", "create_public_threads", "add_reactions")

#: What makes a channel *private* rather than merely quiet: the bit Discord hides
#: a channel with. Denied only by a lock that was asked to hide — never by the
#: ordinary one, because a lock that also took the room off everybody's list would
#: be a different decision wearing the same name.
HIDDEN_PERMISSIONS: tuple[str, ...] = ("view_channel",)

#: Every bit a lockdown command ever writes, as one mask: the four names above,
#: read off Discord's own enum so the bits and the names cannot drift apart. Used
#: by :func:`looks_like_our_lock` to tell a lock from a mask somebody made by hand
#: — Discord's own deny-everything toggle denies the same Send Messages a lock
#: does, and it is not this bot's overwrite.
LOCK_MASK: int = sum(
    int(getattr(discord.Permissions, name).flag)
    for name in ("send_messages", "create_public_threads", "add_reactions", "view_channel")
)


def _only_lock_bits(deny) -> bool:
    """Whether every bit this deny holds is one a lockdown command writes.

    The vocabulary is :func:`lock_names` in full — the three a `/lockdown` denies
    and the fourth a hidden one adds — read off Discord's own enum rather than
    typed beside it. Anything else denied means a **person** made this mask: the
    wall a "private" category carries in Discord's own panel, or a server's
    everything-off toggle. That is a different decision wearing the lock's clothes,
    and crediting it to this bot tells an administrator their hand-made privacy is
    the bot's old lockdown *and* that the pass will not touch it.
    """
    return (_mask(deny) & ~LOCK_MASK) == 0

#: Who a lock holds, as the ledger records it. ``channel`` is the per-channel
#: `/lockdown`: held for everyone, administrators and the owner included, which is
#: what closing one room means. ``server`` is `/lockdown-server`: members stop,
#: **administrators and the owner keep talking**, because a permission cannot stop
#: them anyway and a whole server nobody can coordinate through is a lockdown the
#: staff cannot run.
DEFAULT_LOCK_SCOPE = "channel"
LOCK_SCOPES: tuple[str, ...] = (DEFAULT_LOCK_SCOPE, "server")


def staff_exempt(scope) -> bool:
    """Whether a lock with this scope leaves administrators and the owner talking.

    Pure, and deliberately one condition: only the server-wide lock exempts rank.
    A blank or unknown scope answers ``False`` — the strict reading — because the
    row is the only thing that knows which command wrote it, and a missing answer
    must never loosen a lock.
    """
    return str(scope or "") == "server"


def _is_text(channel) -> bool:
    """Whether this channel is one a lockdown applies to.

    Announcement and forum channels are included, voice channels are not: a
    lockdown is about text, and muting a stage is a different decision.
    """
    kind = getattr(channel, "type", None)
    if kind is None:
        return False
    # Category/voice/stage are excluded by name rather than by importing the enum,
    # so a fake channel in a test is a channel like any other.
    name = str(kind).lower()
    return name in ("text", "news", "public_thread", "private_thread", "forum") or "text" in name


def lock_applies(channel) -> bool:
    """Whether a lockdown is about this channel at all.

    :func:`_is_text` under a public name, because `/check` has to ask the same
    question the server-wide lockdown asks when it decides what to walk. A voice
    channel or a category is not a channel a lockdown holds, so a report that told
    an administrator to run a `fix` on one was pointing at something the adoption
    pass can never do — a dead end they could reach twice.
    """
    return _is_text(channel)


def everyone_overwrite(channel):
    """A channel's `@everyone` overwrite, or ``None`` when it has none.

    Public because `/check` asks the same question the lock asks — *is this room
    private*, *does this channel still hold its deny* — and a report that read the
    overwrite its own way could disagree with the engine that writes it. One
    reader, both callers.
    """
    return _everyone_overwrite(channel)


def _everyone_overwrite(channel):
    guild = getattr(channel, "guild", None)
    everyone = getattr(guild, "default_role", None)
    if everyone is None:
        return None
    return getattr(channel, "overwrites", {}).get(everyone) if hasattr(channel, "overwrites") else None


def _overwrites(channel) -> dict:
    """A channel's own overwrites as ``{target: overwrite}``, or ``{}`` if unreadable.

    A cache that has not filled, or a stand-in channel in a test, is a channel
    with nothing to leak through rather than a reason to raise: the other half of
    the lock — the deny on `@everyone` — is written either way.
    """
    held = getattr(channel, "overwrites", None)
    if held is None:
        return {}
    try:
        return dict(held)
    except Exception:  # noqa: BLE001
        return {}


def _mask(permissions) -> int:
    """A permission bitfield as an int, whether it is Discord's or a stand-in's."""
    return int(getattr(permissions, "value", 0) or 0)


def _pair(overwrite):
    """An overwrite's ``(allow, deny)`` bitfields, from either shape it arrives in.

    **``discord.PermissionOverwrite`` has no ``allow`` attribute** — it has
    ``pair()`` — and reading one off the object by name answered ``None`` on the
    only shape that matters on Discord. Every overwrite therefore looked like it
    granted nothing and denied nothing, which is how this engine's first version
    passed its own tests (whose stand-in *did* have the attribute) and did nothing
    at all in the server it was written for. The stand-in is shaped like the real
    object now, so a reader of the wrong attribute cannot pass either way.
    """
    if overwrite is None:
        return None, None
    read = getattr(overwrite, "pair", None)
    if callable(read):
        try:
            return read()
        except Exception:  # noqa: BLE001 — an unreadable overwrite grants nothing
            return None, None
    return getattr(overwrite, "allow", None), getattr(overwrite, "deny", None)


def overwrite_pair(overwrite):
    """An overwrite's ``(allow, deny)`` bitfields; the one way to read one.

    Public for the same reason :func:`everyone_overwrite` is: on Discord the
    fields are not attributes but what ``pair()`` returns, and the first version
    of this engine read them by name, got ``None``, and did nothing on a real
    server while passing every law. A caller that reads an overwrite through this
    cannot repeat that — and a stand-in in a test, shaped like Discord's object,
    fails the same way the real one did.
    """
    return _pair(overwrite)


def _allows(overwrite, names) -> bool:
    """Whether this overwrite **explicitly grants** any of ``names``.

    Read from the overwrite rather than from a member's computed permissions,
    because only an allow in the channel's own overwrites can beat the deny this
    lock is about to write — a permission a role holds at the server level is
    cleared by the `@everyone` deny on its way down.
    """
    allow, _deny = _pair(overwrite)
    return any(bool(getattr(allow, name, False)) for name in names)


def _denies_all(overwrite, names) -> bool:
    """Whether this overwrite explicitly denies every one of ``names``."""
    _allow, deny = _pair(overwrite)
    return all(bool(getattr(deny, name, False)) for name in names)


def leaks(channel, names=LOCKED_PERMISSIONS) -> list:
    """The overwrites that would let somebody through a lock on this channel.

    `@everyone` is never one of them: it is where the deny is written. Everything
    else that grants one of the locked permissions is — the role the server gave
    the room to, the member a ticket let in — and this list is why a lock holds.
    """
    everyone = getattr(getattr(channel, "guild", None), "default_role", None)
    return [
        target
        for target, overwrite in _overwrites(channel).items()
        if target is not everyone and _allows(overwrite, names)
    ]


def _holds(channel, names=LOCKED_PERMISSIONS) -> bool:
    """Whether this channel already holds a lock: nobody is allowed in.

    Both halves are asked, and the second is the one that was missing. The
    `@everyone` deny is what reads as locked in Discord's own panel; an allow on
    somebody else's overwrite is what makes it a lock in name only — so a channel
    whose deny is being beaten is a channel a lockdown should *change* rather than
    report as already locked and walk past.
    """
    return _denies_all(_everyone_overwrite(channel), names) and not leaks(channel, names)


def lock_names(hide: bool = False) -> tuple[str, ...]:
    """What a lock denies: the three, plus `View Channel` when it was asked to hide."""
    return LOCKED_PERMISSIONS + (HIDDEN_PERMISSIONS if hide else ())


def _target_id(target) -> int:
    return int(getattr(target, "id", 0) or 0)


def _target_key(guild, target) -> str:
    """How one overwrite is named in the ledger: ``everyone``, or its own id.

    An id rather than a kind and an id: Discord's snowflakes are unique across
    roles and members, and an id is also what a restore can still act on when the
    role — or the member's cache entry — is no longer around to be looked up.
    """
    if target is not None and target is getattr(guild, "default_role", None):
        return "everyone"
    return str(_target_id(target))


def _resolve_target(guild, key):
    """The role or member a ledger key names, or an ``Object`` standing in for one.

    The stand-in matters: an overwrite that was put back on the wrong target, or
    skipped because a member fell out of the cache, would leave the channel open
    in exactly the way this ledger exists to prevent.
    """
    import discord

    if str(key) == "everyone":
        return getattr(guild, "default_role", None)
    try:
        target_id = int(key)
    except (TypeError, ValueError):
        return None
    get_role = getattr(guild, "get_role", None)
    role = get_role(target_id) if callable(get_role) else None
    if role is not None:
        return role
    get_member = getattr(guild, "get_member", None)
    member = get_member(target_id) if callable(get_member) else None
    return member if member is not None else discord.Object(id=target_id)


def _overwrite_for(channel, target_id):
    """A channel's overwrite for one target id, with the key it is stored under."""
    if not int(target_id or 0):
        return None, None
    for target, overwrite in _overwrites(channel).items():
        if _target_id(target) == int(target_id):
            return target, overwrite
    return None, None


@dataclass
class _Change:
    """One overwrite a lock is about to rewrite, and what it looks like now."""

    target: object
    #: The channel's overwrite for that target as it is right now, or ``None``.
    overwrite: object
    #: ``"allow:deny"`` as :func:`_describe` writes it, or ``""`` for no overwrite.
    previous: str
    #: The permissions to deny **here** — a hidden lock spares a keeper's sight.
    names: tuple


def keepers(guild, actor=None, *, hide: bool = False) -> frozenset:
    """Whose **sight** of a hidden channel the lock spares, by overwrite id.

    Empty for an ordinary lock. ``hide`` is where this matters: it takes a channel
    off everybody's list, and the people who have to be able to open it again are
    the ones who already run the server — whoever asked, and any role holding
    **Manage Channels**. Keeping those keeps their sight of the room only: their
    allow to *talk* is still denied, because "nobody can talk" is what a lock
    means. The bot's own overwrite is never a target at all, whether it is hidden
    or not — see :func:`_lock_targets`.
    """
    if not hide:
        return frozenset()
    keep = set()
    member = getattr(actor, "member", None)
    if member is not None:
        keep.add(_target_id(member))
        for role in list(getattr(member, "roles", None) or []):
            keep.add(_target_id(role))
    for role in list(getattr(guild, "roles", None) or []):
        if bool(getattr(getattr(role, "permissions", None), "manage_channels", False)):
            keep.add(_target_id(role))
    keep.discard(0)
    return frozenset(keep)


def _lock_targets(guild, channel, names, keep=frozenset()) -> list:
    """The overwrites one lock has to change: `@everyone` first, then the leaks.

    `@everyone` is here whenever it does not already deny everything the lock
    denies, and every other overwrite is here only while it **grants** one of
    them. That second half was the defect: Discord applies a role's and a member's
    overwrite *after* `@everyone`'s, so an allow there beats the deny, and a lock
    that denied `@everyone` alone was one people could see and type through.

    ``keep`` protects a target's hold on `View Channel` — never its voice in the
    channel, which is the lock — so a hidden room stays reachable to the people
    who would have to open it again. **The bot's own overwrite is not a target at
    all**, and not out of politeness: the card it posts into the room it just
    locked, and the messages it removes from it, both need the allow it already
    had.
    """
    everyone = getattr(guild, "default_role", None)
    bot_id = _target_id(getattr(guild, "me", None))
    held = _overwrites(channel)
    changes = []
    current = held.get(everyone) if everyone is not None else None
    if not _denies_all(current, names):
        changes.append(
            _Change(target=everyone, overwrite=current, previous=_describe(current), names=tuple(names))
        )
    for target, overwrite in held.items():
        if target is everyone or _target_id(target) == bot_id or _target_id(target) in keep:
            continue
        if not _allows(overwrite, names):
            continue
        changes.append(
            _Change(
                target=target, overwrite=overwrite, previous=_describe(overwrite), names=tuple(names)
            )
        )
    for target, overwrite in held.items():
        # A keeper keeps sight of a hidden channel and nothing else: it is the
        # `View Channel` allow that is spared, not the allow that lets them talk.
        if target is everyone or _target_id(target) == bot_id or _target_id(target) not in keep:
            continue
        wanted = tuple(name for name in names if name not in HIDDEN_PERMISSIONS)
        if _allows(overwrite, wanted):
            changes.append(
                _Change(
                    target=target, overwrite=overwrite, previous=_describe(overwrite), names=wanted
                )
            )
    return changes


def _denied(overwrite, names):
    """``overwrite`` with ``names`` denied — **merged**, never replaced.

    The old body sent a fresh ``PermissionOverwrite(**{name: False})``, which
    Discord stores as *deny these three, say nothing about anything else*. A
    replacement overwrite is not a deny, it is a wipe: on a channel that had been
    made private by hand, `@everyone view_channel=False` was one of the bits it
    said nothing about, so the lock handed the room back to every member it had
    just locked. This keeps every bit the overwrite had and adds the deny to it.
    """
    import discord

    allow, deny = _pair(overwrite)
    merged = discord.PermissionOverwrite.from_pair(
        discord.Permissions(_mask(allow)), discord.Permissions(_mask(deny))
    )
    merged.update(**{name: False for name in names})
    return merged


def _previous_document(guild, changes) -> str:
    """Every overwrite one lock is about to rewrite, as the ledger's one column.

    A JSON object keyed by :func:`_target_key`, because a lock can now change
    more than `@everyone` and the undo has to put back *each* one. A row written
    before this holds one ``allow:deny`` string, and one string can only ever have
    been about `@everyone` — :func:`_ledger_previous` reads it as exactly that, so
    the old rows keep restoring the way they always did.
    """
    if not changes:
        return ""
    return json.dumps({_target_key(guild, change.target): change.previous for change in changes})


def _ledger_previous(previous) -> dict:
    """``{target key: "allow:deny"}`` from a ledger row, in either shape."""
    raw = str(previous or "")
    if not raw:
        return {}
    if not raw.startswith("{"):
        return {"everyone": raw}
    try:
        held = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(held, dict):
        return {}
    return {str(key): str(value or "") for key, value in held.items()}


def _describe(overwrite) -> str:
    """An overwrite as the three numbers the ledger stores.

    ``""`` means there was no overwrite at all, which is the case the unlock path
    has to be able to tell from an overwrite that allows nothing.
    """
    if overwrite is None:
        return ""
    allow, deny = _pair(overwrite)
    return f"{_mask(allow)}:{_mask(deny)}"


def _from_description(text: str):
    """``(allow, deny)`` out of a stored description, or ``None`` for no overwrite."""
    if not text:
        return None
    allow, _, deny = str(text).partition(":")
    try:
        return int(allow or 0), int(deny or 0)
    except ValueError:
        return None


@dataclass
class Plan:
    """What a lockdown would do, before it does it."""

    guild_id: int = 0
    #: Channels whose overwrites would gain the deny.
    change: list = field(default_factory=list)
    #: Channels that already hold it — nothing to change, and saying so is the point.
    already_locked: list = field(default_factory=list)
    #: Channels this bot cannot change a permission on, with the reason.
    blocked: list[tuple] = field(default_factory=list)
    #: Channels a lockdown does not apply to (voice, categories).
    skipped: list = field(default_factory=list)

    @property
    def counts(self) -> dict:
        return {
            "change": len(self.change),
            "already_locked": len(self.already_locked),
            "blocked": len(self.blocked),
            "skipped": len(self.skipped),
        }

    @property
    def worth_changing(self) -> bool:
        return bool(self.change)

    def sentence(self) -> str:
        """One line for a panel, in the numbers it actually has."""
        parts = [f"{len(self.change)} channel(s) would be locked"]
        if self.already_locked:
            parts.append(f"{len(self.already_locked)} already locked")
        if self.blocked:
            parts.append(f"{len(self.blocked)} I cannot manage")
        return ", ".join(parts) + "."


def _manageable(channel) -> tuple[bool, str]:
    """Whether this bot may change this channel's permissions."""
    guild = getattr(channel, "guild", None)
    me = getattr(guild, "me", None)
    if me is None:
        return False, "I could not read my own member in that server."
    perms = getattr(channel, "permissions_for", None)
    if not callable(perms):
        # A fake channel in a test, or a cache that has not filled: treat as
        # manageable so the plan is about the *server* rather than about pytest.
        return True, ""
    granted = perms(me)
    if getattr(granted, "manage_channels", False):
        return True, ""
    return False, "Manage Channels"


def plan(guild) -> Plan:
    """What a lockdown would change in this server. Pure: reads, never writes."""
    result = Plan(guild_id=int(getattr(guild, "id", 0) or 0))
    for channel in list(getattr(guild, "channels", []) or []):
        if not _is_text(channel):
            result.skipped.append(channel)
            continue
        ok, why = _manageable(channel)
        if not ok:
            result.blocked.append((channel, why))
            continue
        if _holds(channel):
            result.already_locked.append(channel)
            continue
        result.change.append(channel)
    return result


@dataclass
class Report:
    """What a lockdown actually did. Counts are real; a list is never a guess."""

    action: str
    ok: bool = False
    changed: int = 0
    restored: int = 0
    already: int = 0
    blocked: int = 0
    #: Overwrites beyond `@everyone` that had been allowed to talk and are not now.
    evicted: int = 0
    detail: str = ""
    failures: list[tuple] = field(default_factory=list)
    channels: int = 0

    def sentence(self) -> str:
        if not self.ok:
            return self.detail or "That did not happen."
        if self.action == "lockdown":
            if not self.changed and self.already:
                # A lock that found its own deny already in place changed nothing and
                # succeeded. Saying so is the whole sentence: before this it read
                # *Locked 0 channel(s)*, and the per-channel command's card said
                # Discord had refused a permission nobody asked it for.
                return (
                    "Nothing needed changing: it already held this lock."
                    if self.channels == 1
                    else "Nothing needed changing: every channel that could be locked already was."
                )
            said = f"Locked **{self.changed}** channel(s)."
            if self.already:
                said += f" {self.already} were already locked."
            if self.blocked:
                said += f" {self.blocked} could not be managed."
            if self.evicted:
                said += (
                    f" {self.evicted} overwrite(s) that had been let in — a role or a "
                    "member the deny on `@everyone` does not reach — were denied too."
                )
            return said
        if self.action == "unlock":
            said = f"Restored **{self.restored}** channel(s) to what they were before."
            if self.blocked:
                said += f" {self.blocked} could not be managed."
            return said
        return f"Set slowmode on **{self.changed}** channel(s)."

    def to_dict(self) -> dict:
        return {
            "success": self.ok,
            "action": self.action,
            "changed": self.changed,
            "restored": self.restored,
            "already": self.already,
            "blocked": self.blocked,
            "evicted": self.evicted,
            "failures": [{"channel": str(channel), "detail": detail} for channel, detail in self.failures],
            "detail": self.detail,
            "said": self.sentence(),
        }


async def _apply_lock(
    guild, channel, actor: moderation_actions.Actor | None, *, names=LOCKED_PERMISSIONS,
    keep=frozenset(),
) -> tuple[int, int, str]:
    """Write one channel's lock; ``(overwrites, leaks closed, refusal or "")``.

    Every overwrite that grants one of ``names`` is rewritten, not only
    `@everyone`, because an allow there beats the deny — that is the whole reason
    this is a loop over a list rather than one call to Discord. The first refusal
    stops this channel and is reported as it happened; the caller carries on with
    the next one, exactly as it did when there was one overwrite to write.
    """
    changes = _lock_targets(guild, channel, names, keep)
    everyone = getattr(guild, "default_role", None)
    if everyone is None and changes:
        return 0, 0, "that server has no default role to change"
    reason = f"{'hide' if HIDDEN_PERMISSIONS[0] in names else 'lockdown'} by "
    reason += actor.label if actor else "the website"
    written = closed = 0
    for change in changes:
        try:
            await channel.set_permissions(
                change.target, overwrite=_denied(change.overwrite, change.names), reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — one overwrite must not stop the rest
            return written, closed, f"{type(exc).__name__}: {exc}"[:200]
        written += 1
        if change.target is not everyone:
            closed += 1
    return written, closed, ""


async def _restore(guild, channel, previous: str, actor: moderation_actions.Actor | None) -> tuple[bool, str]:
    """Put back **every overwrite** the lock changed, from the row that recorded them.

    A row written before the lock could touch more than `@everyone` holds one
    ``allow:deny`` string, and one string can only ever have been about
    `@everyone` — :func:`_ledger_previous` reads it as exactly that, so an old row
    restores the way it always did. An *empty* row is a lock older than the ledger:
    there is nothing to put back, so the deny is **removed** rather than inverted,
    which is the closest thing to an undo that is still honest.
    """
    import discord

    everyone = getattr(guild, "default_role", None)
    if everyone is None:
        return False, "that server has no default role to change"
    reason = f"lockdown lifted by {actor.label if actor else 'the website'}"
    recorded = _ledger_previous(previous)
    if not recorded:
        existing, _overwrite = _overwrite_for(channel, _target_id(everyone))
        if existing is None:
            return True, ""
        try:
            await channel.set_permissions(everyone, overwrite=None, reason=reason)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"[:200]
        return True, ""
    refused = []
    for key, described in recorded.items():
        target = _resolve_target(guild, key)
        if target is None:
            refused.append(f"{key} (not a role or member of this server)")
            continue
        current, _overwrite = _overwrite_for(channel, _target_id(target))
        parsed = _from_description(described)
        try:
            if parsed is None:
                # There was no overwrite for this target. Deleting it is the
                # restore; allowing it would leave a permission nobody granted.
                if current is None:
                    continue
                await channel.set_permissions(target, overwrite=None, reason=reason)
                continue
            allow, deny = parsed
            await channel.set_permissions(
                target,
                overwrite=discord.PermissionOverwrite.from_pair(
                    discord.Permissions(allow), discord.Permissions(deny)
                ),
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — one refusal must not stop the rest
            refused.append(f"{key}: {type(exc).__name__}")
    if refused:
        return False, "; ".join(refused)[:200]
    return True, ""


def _now() -> float:
    import time

    return time.time()


async def _record(
    guild,
    channels,
    actor: moderation_actions.Actor | None,
    *,
    scope: str = DEFAULT_LOCK_SCOPE,
    names=LOCKED_PERMISSIONS,
    keep=frozenset(),
) -> str:
    """Write the channels a lock is about to change down; a refusal, or ``""``.

    **The ledger is written first**: a change that applied and failed to record
    itself would have no undo, and the undo is the whole reason this is safe to
    offer at all. A channel that is *already* in the ledger is left exactly as it
    is, because its row holds the overwrites from **before the first lock** —
    recording it again would store the lock's own deny and leave the undo
    restoring the lock instead of the room. That is what a second `/lockdown` on
    an already-locked channel reaches, and what a per-channel lock inside a
    server-wide one reaches.

    What a row holds is :func:`_previous_document`: the old value of *every*
    overwrite this lock is about to rewrite, `@everyone`'s first — one JSON object
    in the column that used to hold one ``allow:deny`` string, which
    :func:`_ledger_previous` still reads.
    """
    try:
        held = {
            int(channel_id)
            for channel_id, _previous, _at in await db.lockdown_rows(report_guild_id(guild))
        }
    except Exception as exc:  # noqa: BLE001
        # A ledger that cannot be read reads as empty, and the write below is the
        # half that must not fail: refusing here would make one failed read the
        # lock's weak point. The rows it can see are ``INSERT OR REPLACE``d, so
        # the worst case is a row corrected with the overwrite it has right now.
        print(f"[CONTROL] could not read the lockdown ledger: {exc}", flush=True)
        held = set()
    ledger = [
        (
            int(getattr(channel, "id", 0) or 0),
            _previous_document(guild, _lock_targets(guild, channel, names, keep)),
        )
        for channel in channels
        if int(getattr(channel, "id", 0) or 0) not in held
    ]
    if not ledger:
        return ""
    try:
        await db.save_lockdown(
            report_guild_id(guild), ledger, int(getattr(actor, "id", 0) or 0), _now(),
            scope=scope,
        )
    except Exception as exc:  # noqa: BLE001 — no ledger, no lockdown
        return f"I could not write down what to undo, so nothing was changed ({exc})."
    return ""


async def lockdown(guild, actor: moderation_actions.Actor | None = None) -> Report:
    """Lock every channel this bot can manage, remembering what it changed.

    The ledger is written **first**: a bulk permission change that applied and
    failed to record itself would have no undo, and the undo is the whole reason
    this is safe to offer from a web page.
    """
    report = Report(action="lockdown")
    names = lock_names()
    keep = keepers(guild, actor)
    work = plan(guild)
    report.already = len(work.already_locked)
    report.blocked = len(work.blocked)
    report.channels = len(work.change)
    if not work.change:
        report.ok = True
        report.detail = "Every channel that could be locked already is."
        return report

    # The whole-server lock is the one that leaves administrators and the owner
    # able to talk: it stops members, and the rank that has to run the incident
    # through it cannot be stopped by a permission anyway.
    refusal = await _record(
        guild, work.change, actor, scope="server", names=names, keep=keep
    )
    if refusal:
        report.detail = refusal
        return report
    forget_lock_cache(report_guild_id(guild))
    forget_spam(report_guild_id(guild))

    for channel in work.change:
        written, closed, detail = await _apply_lock(
            guild, channel, actor, names=names, keep=keep
        )
        if written:
            report.changed += 1
            report.evicted += closed
            continue
        report.failures.append((getattr(channel, "name", getattr(channel, "id", "?")), detail))

    report.ok = report.changed > 0
    if not report.ok:
        report.detail = "Discord refused every channel — I need **Manage Channels** there."
    return report


def report_guild_id(guild) -> int:
    return int(getattr(guild, "id", 0) or 0)


async def unlock(guild, actor: moderation_actions.Actor | None = None) -> Report:
    """Put back exactly what a lockdown changed, and forget the ledger."""
    report = Report(action="unlock")
    try:
        rows = await db.lockdown_rows(report_guild_id(guild))
    except Exception as exc:  # noqa: BLE001
        report.detail = f"I could not read what the lockdown changed ({exc})."
        return report
    # Read above, so nothing here needs the cached answer — but the rows are away
    # from this moment on, and the listener must not delete one message more.
    forget_lock_cache(report_guild_id(guild))
    forget_spam(report_guild_id(guild))
    if not rows:
        report.ok = True
        report.detail = (
            "No lockdown is recorded for this server, so nothing was reverted. "
            "`/unlock` in a channel that is locked by hand opens that one."
        )
        return report

    released: list[int] = []
    for channel_id, previous, _at in rows:
        channel = guild.get_channel(channel_id)
        if channel is None:
            # A channel deleted during the lockdown has nothing to restore; its
            # row goes with it rather than being retried forever.
            released.append(channel_id)
            continue
        ok, detail = await _restore(guild, channel, previous, actor)
        if ok:
            report.restored += 1
            released.append(channel_id)
        else:
            report.failures.append((getattr(channel, "name", channel_id), detail))
    report.blocked = len(report.failures)
    try:
        await db.forget_lockdown(report_guild_id(guild), released)
    except Exception as exc:  # noqa: BLE001 — the permissions are back; the ledger is bookkeeping
        print(f"[CONTROL] could not tidy the lockdown ledger: {exc}", flush=True)
    report.ok = report.restored > 0 or not report.failures
    if not report.ok:
        report.detail = "Discord refused the restore — I need **Manage Channels** in those channels."
    return report


async def lock_channel(
    guild, channel, actor: moderation_actions.Actor | None = None, *, hide: bool = False
) -> Report:
    """Lock **one** channel, by the same door the server-wide lockdown uses.

    The per-channel `/lockdown` used to set the overwrite and write nothing down,
    which is why unlocking that channel could only *allow* `@everyone` to talk —
    a permission the server may never have granted. It keeps the same ledger now:
    one row, the same undo, and the same row the force-delete listener reads.

    ``hide`` is the same lock with `View Channel` denied on the way past: the
    channel is taken off everybody's list rather than merely going quiet, which is
    what a person means by *making it private*. Whoever asked and every role that
    can manage channels keep their sight of it, because a channel nobody can see
    is a channel nobody can open again.
    """
    names = lock_names(hide)
    keep = keepers(guild, actor, hide=hide)
    report = Report(action="lockdown")
    report.channels = 1
    refusal = await _record(guild, [channel], actor, names=names, keep=keep)
    if refusal:
        report.detail = refusal
        return report
    forget_lock_cache(report_guild_id(guild))
    forget_spam(report_guild_id(guild))
    written, closed, detail = await _apply_lock(guild, channel, actor, names=names, keep=keep)
    if written:
        report.changed = 1
        report.evicted = closed
        report.ok = True
        return report
    if not detail:
        # Nothing to write, and nothing refused: `_lock_targets` came back empty
        # because this channel already denies everything the lock denies and no
        # overwrite hands it back — a second `/lockdown` on a channel that holds, or
        # a repair whose leak somebody closed by hand a moment before the write. The
        # row is written either way, which is what makes it mine from here on, so
        # this is a success with no permission change and not a permission problem.
        report.already = 1
        report.ok = True
        return report
    report.blocked = 1
    report.failures.append((getattr(channel, "name", getattr(channel, "id", "?")), detail))
    report.detail = "Discord refused the permission change — I need **Manage Channels** there."
    return report


async def undo_stray_unlock(
    guild, channel, actor: moderation_actions.Actor | None = None
) -> tuple[bool, str]:
    """Take back the `@everyone` overwrite an old `/unlock` left on one room.

    **This is the undo of a bug, not a permission change.** `/unlock` and
    `/unlock-server` before the ledger opened a room by *allowing* `@everyone` to
    send — ``set_permissions(everyone, send_messages=True)`` — where the room had
    no overwrite of its own (the live server shows 27 of them, written minutes
    apart on one evening in September). Discord stores that as an overwrite in its
    own right, and an overwrite in its own right is the **de-synced** case: the room
    stops taking its category's permissions, so a category that hides the room
    stops hiding it. Removing the overwrite is the only honest undo, because the
    two states are the same one — the room had no `@everyone` overwrite before.

    Only the exact fingerprint is removed (:func:`is_stray_unlock`), read through
    the same reader every lock is read through, so this can never take away a
    lock's deny, an "everything off" mask somebody drew by hand, or a permission a
    person chose.
    """
    overwrite = everyone_overwrite(channel)
    if overwrite is None:
        # No overwrite of its own: this room takes its category's permissions
        # already, which is the state this function exists to reach.
        return True, ""
    if not is_stray_unlock(overwrite):
        return False, (
            "that room's `@everyone` overwrite is not the one an old `/unlock` "
            "left — it is not exactly *allow Send Messages, deny nothing*"
        )
    if not everyone_can_send(guild):
        return False, (
            "`@everyone` can send in that room *only* through this overwrite, so "
            "removing it would take the permission away rather than put one back"
        )
    everyone = getattr(guild, "default_role", None)
    if everyone is None:
        return False, "that server has no default role to change"
    label = actor.label if actor else "the website"
    try:
        await channel.set_permissions(
            everyone,
            overwrite=None,
            reason=f"a stray unlock from before the ledger removed by {label}",
        )
    except Exception as exc:  # noqa: BLE001 — a refusal is reported, not raised
        return False, f"{type(exc).__name__}: {exc}"[:200]
    forget_lock_cache(report_guild_id(guild))
    return True, ""


async def unlock_channel(
    guild, channel, actor: moderation_actions.Actor | None = None
) -> Report:
    """Give one channel back, restoring what the ledger says was there.

    A channel that is **not** in the ledger is still unlocked: it is opened by
    *removing* the deny, not by allowing `@everyone` to talk, which is what the
    per-channel command did before it kept a ledger — and on a channel whose
    pre-lock overwrite nobody recorded, removing it is the closest thing to an
    undo that is still honest.
    """
    report = Report(action="unlock")
    report.channels = 1
    channel_id = int(getattr(channel, "id", 0) or 0)
    try:
        rows = await db.lockdown_rows(report_guild_id(guild))
    except Exception as exc:  # noqa: BLE001
        report.detail = f"I could not read what the lockdown changed ({exc})."
        return report
    previous = next(
        (row[1] for row in rows if int(row[0]) == channel_id), ""
    )
    ok, detail = await _restore(guild, channel, previous, actor)
    if not ok:
        report.blocked = 1
        report.failures.append((getattr(channel, "name", getattr(channel, "id", "?")), detail))
        report.detail = "Discord refused the restore — I need **Manage Channels** there."
        return report
    report.restored = 1
    report.ok = True
    try:
        await db.forget_lockdown(report_guild_id(guild), [channel_id])
    except Exception as exc:  # noqa: BLE001 — the permission is back; the ledger is bookkeeping
        print(f"[CONTROL] could not tidy the lockdown ledger: {exc}", flush=True)
    forget_lock_cache(report_guild_id(guild))
    forget_spam(report_guild_id(guild))
    return report


async def set_slowmode(
    guild,
    seconds: int,
    actor: moderation_actions.Actor | None = None,
    *,
    channels: Iterable = (),
) -> Report:
    """Set the same slowmode on every text channel (or on the ones named).

    A different tool from a lockdown on purpose: the server stays readable and
    nobody can flood it, which is what people actually want during a raid. The
    value is clamped to Discord's own ceiling rather than refused, and `0` is a
    legitimate answer — it is how slowmode is turned off.
    """
    report = Report(action="slowmode")
    seconds = max(0, min(21600, int(seconds or 0)))
    targets = [channel for channel in (list(channels) or list(getattr(guild, "channels", []) or [])) if _is_text(channel)]
    for channel in targets:
        ok, why = _manageable(channel)
        if not ok:
            report.blocked += 1
            report.failures.append((getattr(channel, "name", "?"), why))
            continue
        try:
            await channel.edit(
                slowmode_delay=seconds,
                reason=f"slowmode set by {actor.label if actor else 'the website'}",
            )
            report.changed += 1
        except Exception as exc:  # noqa: BLE001
            report.failures.append((getattr(channel, "name", "?"), f"{type(exc).__name__}: {exc}"[:200]))
    report.channels = len(targets)
    report.ok = report.changed > 0
    if not report.ok:
        report.detail = "Discord refused every channel — I need **Manage Channels** there."
    return report


async def state(guild) -> dict:
    """What this server's lockdown looks like right now, for a panel or a page."""
    try:
        rows = await db.lockdown_rows(report_guild_id(guild))
    except Exception:  # noqa: BLE001
        rows = []
    channels = []
    for channel_id, _previous, locked_at in rows:
        channel = guild.get_channel(channel_id)
        channels.append({
            "id": str(channel_id),
            "name": str(getattr(channel, "name", "") or "deleted channel"),
            "locked_at": locked_at,
        })
    work = plan(guild)
    return {
        "locked": len(rows),
        "channels": channels[:40],
        "counts": work.counts,
        "plan": work.sentence(),
    }


# ── what a lockdown holds, for the listener that enforces it ─────────────────
#
# The ledger above is the undo. It is also the list of channels a message has no
# business being in, which is why the enforcement reads it rather than keeping a
# second registry that could disagree with the first: one row means one locked
# channel, both for the undo and for the delete.


#: Discord's own ceiling on one page of an audit log, and how many pages of each
#: overwrite action the adoption pass reads before giving up on a channel. A lock
#: old enough to predate the ledger is not necessarily recent, so these are a
#: bound rather than a promise — and the pass runs once, at boot, and only when a
#: channel actually needs asking about.
AUDIT_PAGE = 100
AUDIT_PAGES = 3


def _entry_id(entry) -> int:
    """Discord's own id for an audit-log entry: a snowflake, so ids order by time."""
    return int(getattr(entry, "id", 0) or 0)


def _hide_is_whole(deny) -> bool:
    """Whether a deny that hides is the **whole** lock, which is the only shape written.

    `View Channel` has only ever been denied *beside* the three: the hidden
    lockdown arrived with them (one commit introduced both, and nothing older in
    this tree denies a view). So a mask of Send Messages + View Channel and nothing
    else was written by somebody else, and a room hidden by hand is staff talking
    among themselves rather than a lock of ours to claim.
    """
    if not bool(getattr(deny, "view_channel", False)):
        return True
    return bool(getattr(deny, "create_public_threads", False)) and bool(
        getattr(deny, "add_reactions", False)
    )


def looks_like_our_lock(overwrite) -> bool:
    """Whether an overwrite has the shape the lockdown commands write.

    The per-channel `/lockdown` before this module kept a ledger called
    ``set_permissions(everyone, send_messages=False)``, which Discord stores as
    *allow nothing, deny Send Messages*. So the signature is three facts: Send
    Messages is denied, **nothing is granted** — the old command replaced the
    overwrite rather than editing it, so any allow bit at all means somebody has
    been here since and this is no longer only its lock — and **nothing outside the
    bits a lockdown writes is denied either** (:func:`_only_lock_bits`, with a hide
    held to its whole shape by :func:`_hide_is_whole`). The server-wide lockdown
    denies two more permissions and still matches, because the test is about what is
    denied rather than about what is not — but the *vocabulary* is the lock's own,
    which is what stops a hand-made mask being reported as this bot's.

    Matching the shape makes a channel a **candidate**, never a lock: only
    :func:`adopt_legacy_locks` decides, and it asks the audit log first.
    """
    allow, deny = _pair(overwrite)
    if not bool(getattr(deny, "send_messages", False)):
        return False
    if _mask(allow):
        return False
    return _only_lock_bits(deny) and _hide_is_whole(deny)


#: The one permission an old `/unlock` ever wrote: ``set_permissions(everyone,
#: send_messages=True)``. Read off Discord's own enum rather than typed beside it,
#: the way :data:`LOCK_MASK` is, so the bit and the name cannot drift apart.
STRAY_UNLOCK_ALLOW: int = int(discord.Permissions.send_messages.flag)


def is_stray_unlock(overwrite) -> bool:
    """Whether an overwrite is exactly what an old `/unlock` left behind.

    ``/unlock`` before the ledger opened a room by *allowing* `@everyone` to send,
    which Discord writes as an overwrite in its own right — and an overwrite in its
    own right is the **de-synced** case, so a category that hides the room stops
    hiding it. The fingerprint is exact rather than a taste: *allow Send Messages
    and nothing else, deny nothing at all*. A lock's deny fails the first half, any
    other allowed permission fails it too, and a role's or a member's allow is not
    `@everyone`'s to begin with.
    """
    allow, deny = _pair(overwrite)
    return _mask(allow) == STRAY_UNLOCK_ALLOW and _mask(deny) == 0


def everyone_can_send(guild) -> bool:
    """Whether `@everyone`'s **own role** allows Send Messages server-wide.

    Every channel's permissions start from this floor. So an overlay that grants a
    permission the role already grants takes nothing away when it is removed — and
    where the role does *not* grant it, that overlay is the room's only source of
    the permission, and removing it would silence the room rather than put one back.
    On the server this was written for the role grants it, which is why the
    leftovers there can be undone rather than argued about.
    """
    role = getattr(getattr(guild, "default_role", None), "permissions", None)
    return bool(getattr(role, "send_messages", False))


def stray_unlock_is_undoable(guild, channel) -> bool:
    """Whether this room carries the old unlock's mark **and** removing it is an undo.

    Two facts under one predicate, because a list the card says `fix` will put back
    has to be exactly the list `fix` puts back: the mark is the bug's, and the
    removal has to leave `@everyone` able to do everything it could do before.
    """
    return is_stray_unlock(everyone_overwrite(channel)) and everyone_can_send(guild)


def adoption_candidates(guild, recorded) -> list:
    """Channels that look locked by this bot's own commands and have no row.

    ``recorded`` is the ledger's channel ids, read once by the caller — this stays
    pure so the rule can be tested without a database or a gateway.
    """
    found = []
    for channel in list(getattr(guild, "channels", []) or []):
        if int(getattr(channel, "id", 0) or 0) in recorded:
            continue
        if not _is_text(channel):
            continue
        if not looks_like_our_lock(_everyone_overwrite(channel)):
            continue
        ok, _why = _manageable(channel)
        if not ok:
            continue
        found.append(channel)
    return found


def adoption_scope(guild, candidates) -> str:
    """Whether the old locks this pass found were one command or the whole server.

    The old `/lockdown-server` looped every text channel and wrote the same
    one-permission overwrite the per-channel command wrote, so per channel the
    audit log cannot tell the two apart. What tells them apart is *how many*:
    when **every text channel this bot can manage** is holding the same shape,
    the only command that produces that is the server-wide one — and adopting
    those as one server-wide lockdown is what keeps administrators and the owner
    able to talk through a raid the staff cannot otherwise coordinate. Anything
    less is adopted as the per-channel lock it looks like, which holds for
    everyone.
    """
    if len(candidates) < 2:
        return DEFAULT_LOCK_SCOPE
    manageable = [
        channel
        for channel in list(getattr(guild, "channels", []) or [])
        if _is_text(channel) and _manageable(channel)[0]
    ]
    return "server" if len(candidates) >= len(manageable) else DEFAULT_LOCK_SCOPE


async def _last_overwrite_by_this_bot(guild, candidates, bot_user) -> set:
    """The candidate channels whose `@everyone` overwrite this bot last changed.

    Discord's audit log is the only evidence a lock older than this ledger has,
    and the **user on the newest entry for that channel and that role** is what
    tells this bot's lock from an administrator's. That distinction is the whole
    safety of the pass: a channel somebody denied Send Messages in by hand is
    staff talking among themselves, and a sweep that started deleting their
    messages and timing them out would be this feature mistaking another person's
    decision for its own.

    The three overwrite actions are **separate reads**, so the newest change to a
    channel is not simply the first line that comes back: every entry about a
    candidate is kept, and the one with the highest :func:`_entry_id` — a
    snowflake, so ids order by time — is the one that decides. A bot edit from
    before an administrator's newer hand-deny is not evidence that today's deny
    is this bot's.

    A log that cannot be read is not evidence, so nothing is adopted and the
    reason is printed — the same rule the module follows everywhere else.
    """
    import discord

    everyone_id = int(getattr(getattr(guild, "default_role", None), "id", 0) or 0)
    bot_id = int(getattr(bot_user, "id", 0) or 0)
    if not everyone_id or not bot_id:
        return set()
    wanted = {int(getattr(channel, "id", 0) or 0) for channel in candidates}
    newest: dict[int, object] = {}
    actions = (
        discord.AuditLogAction.overwrite_update,
        discord.AuditLogAction.overwrite_create,
        discord.AuditLogAction.overwrite_delete,
    )
    for action in actions:
        try:
            pages = guild.audit_logs(limit=AUDIT_PAGE, action=action)
        except Exception as exc:  # noqa: BLE001 — a fake guild, or a refused read
            print(
                f"[CONTROL] could not read the audit log ({type(exc).__name__}: {exc}); "
                "no lock older than the ledger was adopted.",
                flush=True,
            )
            return set()
        # discord.py 2.x hands back an **async generator**, which crosses its own
        # pages and has **no ``.next()``** — that method belonged to the 1.x
        # ``AsyncIterator``. Calling it here raised ``AttributeError`` on every
        # real guild, the read was judged "broken", and nothing was ever adopted:
        # the boot said *0 adopted* while the sweep walked past every old lock.
        # The generator is consumed directly and bounded by ``limit`` — the page
        # loop it replaces drew from the same pool of entries either way. A fake
        # that still exposes ``.next()`` (the tests' old 1.x-shaped stand-in) is
        # paged as before, so neither shape silently stops early.
        try:
            if hasattr(pages, "next"):
                for _page in range(AUDIT_PAGES):
                    async for entry in pages:
                        channel_id = int(getattr(getattr(entry, "target", None), "id", 0) or 0)
                        if channel_id not in wanted:
                            continue
                        role_id = int(getattr(getattr(entry, "extra", None), "id", 0) or 0)
                        if role_id != everyone_id:
                            continue
                        held = newest.get(channel_id)
                        if held is None or _entry_id(entry) > _entry_id(held):
                            newest[channel_id] = entry
                    if not await pages.next():
                        break
            else:
                count = 0
                async for entry in pages:
                    count += 1
                    if count > AUDIT_PAGE * AUDIT_PAGES:
                        break
                    channel_id = int(getattr(getattr(entry, "target", None), "id", 0) or 0)
                    if channel_id not in wanted:
                        continue
                    role_id = int(getattr(getattr(entry, "extra", None), "id", 0) or 0)
                    if role_id != everyone_id:
                        continue
                    held = newest.get(channel_id)
                    if held is None or _entry_id(entry) > _entry_id(held):
                        newest[channel_id] = entry
        except Exception as exc:  # noqa: BLE001 — a refused or broken read is not evidence
            print(
                f"[CONTROL] could not read the audit log ({type(exc).__name__}: {exc}); "
                "no lock older than the ledger was adopted.",
                flush=True,
            )
            return set()
    return {
        channel_id
        for channel_id, entry in newest.items()
        if int(getattr(entry, "user_id", 0) or 0) == bot_id
    }


async def adopt_legacy_locks(guild, bot_user) -> list:
    """Arm the channels this bot locked before it kept a ledger.

    Every `/lockdown` before this pass wrote a permission and nothing else, so a
    channel locked then is a channel the sweep walks past: the deny is there and
    there is no row for it to read. Adoption is that row being written for the
    locks this bot can prove are its own — the audit log says who last touched the
    `@everyone` overwrite — which is what turns *"run `/unlock` and `/lockdown`
    again"* into *"it was already locked, and now it holds"*.

    The row records **no previous overwrite**, because there is none to restore:
    the old command replaced whatever the channel had, and it was never written
    down. Removing the deny is therefore what `/unlock` does to an adopted
    channel — the same action the old `/unlock` took, without the stray allow it
    handed out. Getting the channel back exactly as it was is not recoverable, and
    pretending otherwise would restore a lock instead of a room.

    **Every server is answered in the host log, whether or not anything was
    adopted** — how many channels were candidates and how many were claimed,
    with their names when there are any. A pass that says nothing when it finds
    nothing cannot be told from a pass that never ran, and this one is meant to
    be readable back from a deploy's log ring.

    What the pass cannot recover is *which command* wrote an old lock — and that
    is what decides whether staff may talk through it — so
    :func:`adoption_scope` answers it from the shape of the find: the old
    server-wide command left the same overwrite on every text channel, and
    nothing else does.

    Returns the channels adopted, so a caller can say so.
    """
    guild_id = report_guild_id(guild)
    try:
        rows = await db.lockdown_rows(guild_id)
    except Exception as exc:  # noqa: BLE001 — an unreadable ledger adopts nothing
        print(f"[CONTROL] could not read the lockdown ledger: {exc}", flush=True)
        return []
    candidates = adoption_candidates(guild, {int(row[0]) for row in rows})
    mine = await _last_overwrite_by_this_bot(guild, candidates, bot_user) if candidates else set()
    adopted = [channel for channel in candidates if int(getattr(channel, "id", 0) or 0) in mine]
    scope = adoption_scope(guild, candidates)
    if adopted:
        ledger = [(int(getattr(channel, "id", 0) or 0), "") for channel in adopted]
        try:
            await db.save_lockdown(
                guild_id, ledger, int(getattr(bot_user, "id", 0) or 0), _now(), scope=scope
            )
        except Exception as exc:  # noqa: BLE001 — no ledger, no lock
            print(f"[CONTROL] could not record the lockdowns I adopted: {exc}", flush=True)
            adopted = []
        else:
            forget_lock_cache(guild_id)
            forget_spam(guild_id)
    names = ", ".join(f"#{getattr(channel, 'name', '?')}" for channel in adopted)
    whole = " as one server-wide lockdown" if names and staff_exempt(scope) else ""
    print(
        f"[CONTROL] locks older than the ledger in guild {guild_id}: "
        f"{len(candidates)} candidate(s), adopted {len(adopted)}"
        + (f" ({names}){whole}" if names else "")
        + ".",
        flush=True,
    )
    return adopted


def is_removable(message) -> bool:
    """Whether a message in a locked channel is removed.

    Everything a *person* wrote: a member's, a moderator's, the one who placed
    the lock, the owner's — none of them is something a permission can deny, so
    the sweep is the only thing that answers any of them. The bot's own messages
    stay, because the card that announces the lockdown is one of them, and a
    lockdown whose own notice deleted itself would look like the failure it exists
    to prevent. Discord's own notices — a pin, a join — are not somebody talking,
    and they are most of what Discord refuses to delete anyway.
    """
    if bool(getattr(getattr(message, "author", None), "bot", False)):
        return False
    kind = getattr(message, "type", None)
    if kind is None:
        return True
    return str(getattr(kind, "name", kind)).lower() in ("default", "reply")


#: The locked channels of each server, as the last read left them — each one with
#: the scope of the lock that holds it. Read on every message, so it cannot be a
#: query per message: a lock changes rarely and messages arrive in bursts. Every
#: lock and unlock path below forgets what it touched, and the entry expires
#: anyway — a row written by a process this one does not share still takes effect,
#: one window late.
_LOCKED_CACHE: dict[int, tuple[float, dict]] = {}
_LOCKED_CACHE_SECONDS = 10.0


def forget_lock_cache(guild_id=None) -> None:
    """Drop the cached locks — one server's, or every server's."""
    if guild_id is None:
        _LOCKED_CACHE.clear()
    else:
        _LOCKED_CACHE.pop(int(guild_id or 0), None)


async def locked_channels(guild_id) -> dict:
    """``{channel_id: scope}`` for this server's locked channels.

    A read that fails answers **nothing is locked** rather than raising: the one
    caller of this deletes messages, and a database that could not be read is not
    a reason to start removing what people wrote.
    """
    import time

    key = int(guild_id or 0)
    now = time.monotonic()
    cached = _LOCKED_CACHE.get(key)
    if cached is not None and now - cached[0] < _LOCKED_CACHE_SECONDS:
        return cached[1]
    try:
        rows = await db.lockdown_locks(key)
    except Exception as exc:  # noqa: BLE001
        print(f"[CONTROL] could not read the lockdown ledger: {exc}", flush=True)
        return cached[1] if cached is not None else {}
    held = {
        int(channel_id): str(scope or DEFAULT_LOCK_SCOPE) for channel_id, scope in rows
    }
    _LOCKED_CACHE[key] = (now, held)
    return held


async def locked_channel_ids(guild_id) -> frozenset:
    """Which channels this server has recorded as locked, whoever they hold."""
    return frozenset(await locked_channels(guild_id))


async def is_locked(guild_id, channel) -> bool:
    """Whether this channel is held by a lockdown — the listener's question.

    The argument is a channel rather than an id because a **thread** is held by
    the channel it hangs under: the lockdown denies *creating* a public thread,
    but a thread that already existed is still a place to type, and its
    permissions come from its parent. An id in the ledger and a parent id in it
    both answer yes.
    """
    held = await locked_channels(guild_id)
    if not held:
        return False
    channel_id = int(getattr(channel, "id", 0) or 0)
    parent_id = int(getattr(channel, "parent_id", None) or 0)
    return channel_id in held or (parent_id != 0 and parent_id in held)


async def lock_scope(guild_id, channel) -> str:
    """What the lock on this channel holds — ``""`` when it is not locked at all.

    Asked only after :func:`is_locked` has answered yes, and answered from the
    same cached read, so the listener pays one query per window for both
    questions. A thread answers with its parent's scope, exactly as it does for
    ``is_locked``.
    """
    held = await locked_channels(guild_id)
    channel_id = int(getattr(channel, "id", 0) or 0)
    if channel_id in held:
        return held[channel_id]
    parent_id = int(getattr(channel, "parent_id", None) or 0)
    if parent_id and parent_id in held:
        return held[parent_id]
    return ""


# ── what a burst into a locked channel earns ─────────────────────────────────
#
# Removing the messages is the wall. This is what happens when somebody walks
# into it five times in ten seconds anyway: the sweep stops treating it as one
# person who did not read the notice, and the person is timed out. The ladder is
# in memory on purpose — it is a rate rather than a record, and what is durable
# about a timeout is the case it opens and the card it puts in the log.

#: How many messages into a locked channel, inside the window, count as a burst.
SPAM_WINDOW_SECONDS = 10.0
SPAM_LIMIT = 5
#: What a burst earns, in order: ten minutes, an hour, a day — and a day again.
#: Discord's own ceiling is 28 days; a day is where this stops.
SPAM_TIMEOUTS = (10 * 60, 60 * 60, 24 * 60 * 60)
#: How long a rung is remembered. A burst an hour after the last one starts at the
#: bottom again, because a ladder nobody can climb down is not a rule this bot
#: should be holding.
SPAM_ESCALATION_SECONDS = 60 * 60
#: The map's ceiling. One entry per member per channel is a slow leak during a
#: busy incident, and this is the prune ``utils.ratelimit`` uses for the same job.
SPAM_MAX_KEYS = 2000


@dataclass
class Strike:
    """One removed message that was one too many."""

    #: How many messages were inside the window when the burst was counted.
    messages: int
    #: Which rung of the ladder this is: 1 for the first burst.
    offences: int
    #: The timeout that rung means, in seconds.
    seconds: int

    def sentence(self) -> str:
        return f"{self.messages} in {int(SPAM_WINDOW_SECONDS)}s — timeout #{self.offences}"


@dataclass
class _Burst:
    """What one member has pushed at one locked channel, recently."""

    at: list = field(default_factory=list)
    offences: int = 0
    last: float = 0.0


_SPAM: dict = {}


def spam_timeout(offences: int) -> int:
    """The ladder, clamped: rung 1 is ten minutes and the last rung repeats."""
    rung = max(1, int(offences or 1)) - 1
    return SPAM_TIMEOUTS[min(rung, len(SPAM_TIMEOUTS) - 1)]


def forget_spam(guild_id=None) -> None:
    """Drop what the sweep remembers about who pushed at a lock.

    Called by every lock and every unlock: the ladder is per incident, and a
    channel that has just been lifted or locked is not the one somebody was
    climbing.
    """
    if guild_id is None:
        _SPAM.clear()
        return
    key = int(guild_id or 0)
    for entry in [entry for entry in _SPAM if entry[0] == key]:
        _SPAM.pop(entry, None)


def note_locked_message(guild_id, channel_id, member_id, *, now=None) -> Strike | None:
    """Count one message removed from a locked channel; a :class:`Strike` if that
    was one too many.

    Counted only for messages the sweep actually removed, so somebody already
    timed out — who cannot send — is not counted for anything. A burst that earns
    a strike resets the window, so the next strike is a *second* burst rather
    than the same one being counted again. Synchronous on purpose: it is
    arithmetic over a dict, and the listener must not await its own bookkeeping
    between the delete and the timeout.
    """
    import time

    now = time.monotonic() if now is None else float(now)
    key = (int(guild_id or 0), int(channel_id or 0), int(member_id or 0))
    burst = _SPAM.get(key)
    if burst is None:
        _prune_spam(now)
        burst = _Burst()
        _SPAM[key] = burst
    burst.at = [stamp for stamp in burst.at if now - stamp < SPAM_WINDOW_SECONDS]
    burst.at.append(now)
    if burst.offences and now - burst.last > SPAM_ESCALATION_SECONDS:
        # An hour of quiet is the bottom of the ladder again.
        burst.offences = 0
    if len(burst.at) < SPAM_LIMIT:
        return None
    messages = len(burst.at)
    burst.offences += 1
    burst.last = now
    burst.at = []
    return Strike(
        messages=messages,
        offences=burst.offences,
        seconds=spam_timeout(burst.offences),
    )


def _prune_spam(now: float) -> None:
    """Forget the bursts that are over, when the map has grown past its ceiling."""
    if len(_SPAM) <= SPAM_MAX_KEYS:
        return
    for key, burst in list(_SPAM.items()):
        if not burst.at and now - burst.last > SPAM_ESCALATION_SECONDS:
            _SPAM.pop(key, None)
