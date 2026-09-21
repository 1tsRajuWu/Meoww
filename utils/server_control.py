"""Whole-server actions, with their own undo.

`/lock` and `/unlock` have existed per channel for a while. This module is the
*server-wide* version — the thing an administrator reaches for in an incident —
and it is written around one rule that the per-channel command never had to
answer: **a bulk permission change needs its own undo, and the undo has to be the
server's, not mine.**

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
* **Slowmode is not a lockdown.** It is offered because it is what people
  actually want during a raid — the server stays readable, nobody can flood it —
  and it is a per-channel *value* rather than a permission, so it is written
  straight to ``slowmode_delay`` with the same plan/apply split.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

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


def _everyone_overwrite(channel):
    guild = getattr(channel, "guild", None)
    everyone = getattr(guild, "default_role", None)
    if everyone is None:
        return None
    return getattr(channel, "overwrites", {}).get(everyone) if hasattr(channel, "overwrites") else None


def _denies(overwrite) -> bool:
    """Whether an overwrite already denies everything a lockdown would remove."""
    if overwrite is None:
        return False
    deny = getattr(overwrite, "deny", None)
    if deny is None:
        return False
    return all(bool(getattr(deny, permission, False)) for permission in LOCKED_PERMISSIONS)


def _describe(overwrite) -> str:
    """An overwrite as the three numbers the ledger stores.

    ``""`` means there was no overwrite at all, which is the case the unlock path
    has to be able to tell from an overwrite that allows nothing.
    """
    if overwrite is None:
        return ""
    allow = getattr(overwrite, "allow", None)
    deny = getattr(overwrite, "deny", None)
    allowed = int(getattr(allow, "value", 0) or 0)
    denied = int(getattr(deny, "value", 0) or 0)
    return f"{allowed}:{denied}"


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
    #: Channels whose `@everyone` would gain the deny.
    change: list = field(default_factory=list)
    #: Channels already denied — nothing to change, and saying so is the point.
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
        overwrite = _everyone_overwrite(channel)
        if _denies(overwrite):
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
    detail: str = ""
    failures: list[tuple] = field(default_factory=list)
    channels: int = 0

    def sentence(self) -> str:
        if not self.ok:
            return self.detail or "That did not happen."
        if self.action == "lockdown":
            said = f"Locked **{self.changed}** channel(s)."
            if self.already:
                said += f" {self.already} were already locked."
            if self.blocked:
                said += f" {self.blocked} could not be managed."
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
            "failures": [{"channel": str(channel), "detail": detail} for channel, detail in self.failures],
            "detail": self.detail,
            "said": self.sentence(),
        }


async def _set_deny(guild, channel, actor: moderation_actions.Actor | None) -> tuple[bool, str]:
    """Add the lockdown's deny to one channel's `@everyone` overwrite."""
    import discord

    everyone = getattr(guild, "default_role", None)
    if everyone is None:
        return False, "that server has no default role to change"
    overwrite = discord.PermissionOverwrite(**{name: False for name in LOCKED_PERMISSIONS})
    try:
        await channel.set_permissions(
            everyone, overwrite=overwrite,
            reason=f"lockdown by {actor.label if actor else 'the website'}",
        )
    except Exception as exc:  # noqa: BLE001 — one channel must not stop the rest
        return False, f"{type(exc).__name__}: {exc}"[:200]
    return True, ""


async def _restore(guild, channel, previous: str, actor: moderation_actions.Actor | None) -> tuple[bool, str]:
    """Put one channel's `@everyone` overwrite back the way the ledger found it."""
    import discord

    everyone = getattr(guild, "default_role", None)
    if everyone is None:
        return False, "that server has no default role to change"
    parsed = _from_description(previous)
    try:
        if parsed is None:
            # There was no overwrite. Deleting it is the restore; allowing it
            # would leave a permission the server never granted.
            current = _everyone_overwrite(channel)
            if current is None:
                return True, ""
            await channel.set_permissions(
                everyone, overwrite=None,
                reason=f"lockdown lifted by {actor.label if actor else 'the website'}",
            )
            return True, ""
        allow, deny = parsed
        overwrite = discord.PermissionOverwrite.from_pair(
            discord.Permissions(allow), discord.Permissions(deny)
        )
        await channel.set_permissions(
            everyone, overwrite=overwrite,
            reason=f"lockdown lifted by {actor.label if actor else 'the website'}",
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]
    return True, ""


def _now() -> float:
    import time

    return time.time()


async def lockdown(guild, actor: moderation_actions.Actor | None = None) -> Report:
    """Lock every channel this bot can manage, remembering what it changed.

    The ledger is written **first**: a bulk permission change that applied and
    failed to record itself would have no undo, and the undo is the whole reason
    this is safe to offer from a web page.
    """
    report = Report(action="lockdown")
    work = plan(guild)
    report.already = len(work.already_locked)
    report.blocked = len(work.blocked)
    report.channels = len(work.change)
    if not work.change:
        report.ok = True
        report.detail = "Every channel that could be locked already is."
        return report

    ledger = [(int(getattr(channel, "id", 0)), _describe(_everyone_overwrite(channel))) for channel in work.change]
    try:
        await db.save_lockdown(report_guild_id(guild), ledger, int(getattr(actor, "id", 0) or 0), _now())
    except Exception as exc:  # noqa: BLE001 — no ledger, no lockdown
        report.detail = f"I could not write down what to undo, so nothing was changed ({exc})."
        return report

    for channel in work.change:
        ok, detail = await _set_deny(guild, channel, actor)
        if ok:
            report.changed += 1
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
    if not rows:
        report.ok = True
        report.detail = "No lockdown is recorded for this server, so nothing was reverted."
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
