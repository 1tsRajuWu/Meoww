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
  feature mistaking somebody else's decision for its own.
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


async def _record(guild, channels, actor: moderation_actions.Actor | None) -> str:
    """Write the channels a lock is about to change down; a refusal, or ``""``.

    **The ledger is written first**: a change that applied and failed to record
    itself would have no undo, and the undo is the whole reason this is safe to
    offer at all. A channel that is *already* in the ledger is left exactly as it
    is, because its row holds the overwrite from **before the first lock** —
    recording it again would store the lock's own deny and leave the undo
    restoring the lock instead of the room. That is what a second `/lockdown` on
    an already-locked channel reaches, and what a per-channel lock inside a
    server-wide one reaches.
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
        (int(getattr(channel, "id", 0) or 0), _describe(_everyone_overwrite(channel)))
        for channel in channels
        if int(getattr(channel, "id", 0) or 0) not in held
    ]
    if not ledger:
        return ""
    try:
        await db.save_lockdown(
            report_guild_id(guild), ledger, int(getattr(actor, "id", 0) or 0), _now()
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
    work = plan(guild)
    report.already = len(work.already_locked)
    report.blocked = len(work.blocked)
    report.channels = len(work.change)
    if not work.change:
        report.ok = True
        report.detail = "Every channel that could be locked already is."
        return report

    refusal = await _record(guild, work.change, actor)
    if refusal:
        report.detail = refusal
        return report
    forget_lock_cache(report_guild_id(guild))
    forget_spam(report_guild_id(guild))

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
    guild, channel, actor: moderation_actions.Actor | None = None
) -> Report:
    """Lock **one** channel, by the same door the server-wide lockdown uses.

    The per-channel `/lockdown` used to set the overwrite and write nothing down,
    which is why unlocking that channel could only *allow* `@everyone` to talk —
    a permission the server may never have granted. It keeps the same ledger now:
    one row, the same undo, and the same row the force-delete listener reads.
    """
    report = Report(action="lockdown")
    report.channels = 1
    refusal = await _record(guild, [channel], actor)
    if refusal:
        report.detail = refusal
        return report
    forget_lock_cache(report_guild_id(guild))
    forget_spam(report_guild_id(guild))
    ok, detail = await _set_deny(guild, channel, actor)
    if ok:
        report.changed = 1
        report.ok = True
        return report
    report.blocked = 1
    report.failures.append((getattr(channel, "name", getattr(channel, "id", "?")), detail))
    report.detail = "Discord refused the permission change — I need **Manage Channels** there."
    return report


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


def looks_like_our_lock(overwrite) -> bool:
    """Whether an overwrite has the shape the lockdown commands write.

    The per-channel `/lockdown` before this module kept a ledger called
    ``set_permissions(everyone, send_messages=False)``, which Discord stores as
    *allow nothing, deny Send Messages*. So the signature is two facts: Send
    Messages is denied, and **nothing is granted** — the old command replaced the
    overwrite rather than editing it, so any allow bit at all means somebody has
    been here since and this is no longer only its lock. The server-wide lockdown
    denies two more permissions and still matches, because the test is about what
    is denied rather than about what is not.

    Matching the shape makes a channel a **candidate**, never a lock: only
    :func:`adopt_legacy_locks` decides, and it asks the audit log first.
    """
    if overwrite is None:
        return False
    deny = getattr(overwrite, "deny", None)
    if not bool(getattr(deny, "send_messages", False)):
        return False
    allow = getattr(overwrite, "allow", None)
    return not int(getattr(allow, "value", 0) or 0)


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
        try:
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
    if adopted:
        ledger = [(int(getattr(channel, "id", 0) or 0), "") for channel in adopted]
        try:
            await db.save_lockdown(
                guild_id, ledger, int(getattr(bot_user, "id", 0) or 0), _now()
            )
        except Exception as exc:  # noqa: BLE001 — no ledger, no lock
            print(f"[CONTROL] could not record the lockdowns I adopted: {exc}", flush=True)
            adopted = []
        else:
            forget_lock_cache(guild_id)
            forget_spam(guild_id)
    names = ", ".join(f"#{getattr(channel, 'name', '?')}" for channel in adopted)
    print(
        f"[CONTROL] locks older than the ledger in guild {guild_id}: "
        f"{len(candidates)} candidate(s), adopted {len(adopted)}"
        + (f" ({names})" if names else "")
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


#: The locked channels of each server, as the last read left them. Read on every
#: message, so it cannot be a query per message: a lock changes rarely and
#: messages arrive in bursts. Every lock and unlock path below forgets what it
#: touched, and the entry expires anyway — a row written by a process this one
#: does not share still takes effect, one window late.
_LOCKED_CACHE: dict[int, tuple[float, frozenset]] = {}
_LOCKED_CACHE_SECONDS = 10.0


def forget_lock_cache(guild_id=None) -> None:
    """Drop the cached locks — one server's, or every server's."""
    if guild_id is None:
        _LOCKED_CACHE.clear()
    else:
        _LOCKED_CACHE.pop(int(guild_id or 0), None)


async def locked_channel_ids(guild_id) -> frozenset:
    """Which channels this server has recorded as locked.

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
        rows = await db.lockdown_rows(key)
    except Exception as exc:  # noqa: BLE001
        print(f"[CONTROL] could not read the lockdown ledger: {exc}", flush=True)
        return cached[1] if cached is not None else frozenset()
    ids = frozenset(int(row[0]) for row in rows)
    _LOCKED_CACHE[key] = (now, ids)
    return ids


async def is_locked(guild_id, channel) -> bool:
    """Whether this channel is held by a lockdown — the listener's question.

    The argument is a channel rather than an id because a **thread** is held by
    the channel it hangs under: the lockdown denies *creating* a public thread,
    but a thread that already existed is still a place to type, and its
    permissions come from its parent. An id in the ledger and a parent id in it
    both answer yes.
    """
    ids = await locked_channel_ids(guild_id)
    if not ids:
        return False
    channel_id = int(getattr(channel, "id", 0) or 0)
    parent_id = int(getattr(channel, "parent_id", None) or 0)
    return channel_id in ids or (parent_id != 0 and parent_id in ids)


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
