"""Changing a server's *shape* — channels and roles — as facts, with a record.

The management tier adds powers that can destroy things: deleting a channel
destroys every message in it, and Discord keeps no copy to restore from. So the
same discipline ``utils/moderation_actions`` applies to members applies here, and
for the same reason — two callers of one power is how a product ends up with two
answers to "what happened to that channel".

* **One door.** Every Discord write this file exists for happens inside it, so the
  record and the action cannot come apart. A command that called
  ``channel.delete()`` itself would be a write with no case, which is the failure
  the brief calls out by name: *no shipped command without an audit-log entry if
  it modifies server state.*
* **A refusal is a fact.** ``@everyone`` is not a role that can be deleted,
  Discord will not let the bot touch anything above its own top role, and a
  managed role belongs to an integration rather than to the server. Each of those
  is answered in words and written to the log, because a log that only holds
  successes reads like a bot that never refused anything.
* **The guard is not a UI opinion.** :func:`deletable_channels` and
  :func:`deletable_role` decide what *may* be touched, and the command, the
  dashboard panel and the tests all ask them rather than re-deciding. A panel
  that could delete a role the command refuses would be a worse bug than a
  missing feature.

What this module deliberately does **not** do is ask for confirmation. A door has
no interaction to prompt with; the ceiling and the prompt live in
``utils/confirmations``, and the command wires the two together so the *size* of
an action is checked before the door is opened.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import discord

from . import modlog
from .moderation_actions import Actor, failure_note_card, record

#: Discord's own ceiling on a channel or role name. Declared rather than typed
#: into each check, because two copies of a limit is how they come to differ.
MAX_NAME = 100

#: The actions here that **destroy** something, as opposed to making it. Declared
#: rather than inferred because the confirmation ceilings are keyed by action, and
#: a destructive action with no ceiling is one that would run unconfirmed: a test
#: holds this list and ``utils.confirmations.CEILINGS`` together.
DESTRUCTIVE: tuple[str, ...] = (
    "channel_delete",
    "channel_archive",
    "role_delete",
    "role_purge",
)

#: How many members :func:`purge_role` will report by name in its card. The
#: count is the truth; a list of 12,000 ids is not a card anybody reads.
MAX_LISTED = 20


@dataclass
class Result:
    """What one structure action did, or why it did not.

    Shaped like ``moderation_actions.Outcome`` on purpose — same fields where the
    meaning is the same — but its own class, because a channel has no
    ``display_avatar`` and a role has no ``timed_out_until``, and a shared type
    carrying fields neither of them uses is how one of them grows a lie.
    """

    action: str
    ok: bool
    detail: str = ""
    count: int = 0
    names: list[str] = field(default_factory=list)
    case_id: int | None = None
    reason: str = ""
    refused_by: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.ok,
            "action": self.action,
            "detail": self.detail,
            "count": int(self.count),
            "names": list(self.names),
            "case_id": self.case_id,
            "reason": self.reason,
            "refused_by": self.refused_by,
        }


def clean_name(raw: str) -> str:
    """A channel or role name as Discord takes it, or ``""`` when unusable.

    Discord lower-cases a channel's name for its own purposes and allows spaces
    (it renders them as dashes), so this does not rewrite what somebody typed —
    it trims, collapses whitespace and refuses the two things Discord refuses:
    nothing at all, and more than a hundred characters.
    """
    name = " ".join(str(raw or "").split())
    return name[:MAX_NAME]


def name_problem(name: str, *, what: str = "channel") -> str:
    """Why this name cannot be used, or ``""``."""
    if not name:
        return f"A {what} needs a name."
    if str(name).startswith("@"):
        # Discord renders a leading @ as a mention, so a role called
        # "@everyone" is a role that pings the server every time it is shown.
        return "A name that starts with `@` reads as a mention — drop the `@`."
    return ""


def deletable_channels(guild, channels) -> tuple[list, list[tuple]]:
    """``(may_be_deleted, refused)`` for a list of channels.

    Refused pairs are ``(channel, why)``. A channel the bot cannot see, cannot
    manage, or that is not in this server is refused *by name*, because "0 of 4
    channels deleted" is a sentence an administrator cannot act on.
    """
    allowed: list = []
    refused: list[tuple] = []
    for channel in channels or ():
        if channel is None:
            continue
        if getattr(channel, "guild", guild) is not guild:
            refused.append((channel, "that channel is in another server"))
            continue
        me = getattr(guild, "me", None)
        permissions = None
        if me is not None and hasattr(channel, "permissions_for"):
            try:
                permissions = channel.permissions_for(me)
            except Exception:  # noqa: BLE001 — an unreadable channel is not a deletable one
                permissions = None
        if permissions is not None and not getattr(permissions, "manage_channels", True):
            refused.append((channel, "I do not have Manage Channels on it"))
            continue
        allowed.append(channel)
    return allowed, refused


def deletable_role(guild, role) -> str:
    """Why this role cannot be deleted or purged, or ``""``.

    Four refusals, each a real server and not a hypothetical: ``@everyone`` is not
    a role, a managed role belongs to an integration, a role above the bot's own
    cannot be touched however the permission bits read, and a bot role is the
    bot's own membership of the server.
    """
    if role is None:
        return "That role does not exist in this server."
    if getattr(role, "guild", guild) is not guild:
        return "That role is in another server."
    if getattr(role, "is_default", lambda: False)():
        return "`@everyone` is the server itself, not a role — it cannot be removed."
    if getattr(role, "managed", False):
        return (
            f"**{getattr(role, 'name', 'That role')}** belongs to an integration "
            "(a bot or a booster role), so only it can give the role up."
        )
    me = getattr(guild, "me", None)
    top = getattr(me, "top_role", None)
    if top is not None and role >= top:
        return (
            f"**{getattr(role, 'name', 'That role')}** sits at or above my own top "
            "role, and Discord does not let a bot touch those."
        )
    return ""


async def _write(action: str, guild, target, actor: Actor, reason: str, call):
    """Run one Discord write and answer ``(ok, detail)``. Never raises.

    The same contract as ``moderation_actions._discord_call``, kept separate
    because the refusals are different words: a channel that refuses is almost
    always a permission bit somebody can grant, and the sentence has to name it.
    """
    try:
        await call()
    except discord.Forbidden:
        return False, (
            "Discord refused it — I need **Manage Channels** (for channels) or "
            "**Manage Roles** (for roles), and my own role has to sit above the "
            "thing I am changing."
        )
    except discord.NotFound:
        return False, "Discord says that is not there any more — nothing was changed."
    except discord.HTTPException as exc:
        return False, f"Discord answered with an error ({getattr(exc, 'status', 'unknown')})."
    except Exception as exc:  # noqa: BLE001 — a transport failure is a refusal too
        return False, f"That did not reach Discord ({type(exc).__name__})."
    return True, ""


async def _finish(
    result: Result, guild, target, actor: Actor, reason: str, fallback_target=None
) -> Result:
    """Close a result: a case and a card when it worked, a card when it did not.

    One place, so the two halves cannot drift — and the mod-log call is the
    server's own channel, resolved by ``utils.modlog``, which is why an
    unconfigured server loses the card but never the command.
    """
    # A channel has no member to name, so the card's target is the guild itself:
    # the alternative is a card that cannot be built at all, which is how a
    # refusal ends up in nobody's log.
    subject = target or fallback_target or guild
    if not result.ok:
        await modlog.post(
            guild,
            failure_note_card(result.action, guild, subject, actor, result.detail),
            what=f"the refused '{result.action}'",
        )
        result.refused_by = "discord"
        return result
    result.case_id = await record(
        guild, result.action, subject, actor, result.reason or reason
    )
    return result


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #

async def create_channel(
    guild, actor: Actor, name: str, *, category=None, private: bool = False, reason: str = ""
) -> Result:
    """A text channel with the invoker's own name on the creation."""
    action = "channel_create"
    cleaned = clean_name(name)
    problem = name_problem(cleaned)
    if problem:
        return Result(action=action, ok=False, detail=problem, refused_by="input")
    if category is not None and getattr(category, "guild", guild) is not guild:
        return Result(
            action=action, ok=False, detail="That category is in another server.",
            refused_by="input",
        )

    overwrites = None
    if private:
        # Private means one thing and it is not "hidden from the bot": the
        # command's invoker is the only one let in, and the bot has to stay able
        # to look after a channel it created.
        me = getattr(guild, "me", None)
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        if getattr(actor, "member", None) is not None:
            overwrites[actor.member] = discord.PermissionOverwrite(view_channel=True)
        if me is not None:
            overwrites[me] = discord.PermissionOverwrite(view_channel=True)

    result = Result(action=action, ok=False, count=1, names=[cleaned], reason=reason)

    async def call():
        channel = await guild.create_text_channel(
            cleaned, overwrites=overwrites, category=category,
            reason=actor.reason_line(reason),
        )
        result.names = [str(getattr(channel, "name", cleaned))]

    ok, detail = await _write(action, guild, None, actor, reason, call)
    result.ok, result.detail = ok, detail
    return await _finish(result, guild, None, actor, reason, fallback_target=guild)


async def delete_channels(guild, actor: Actor, channels, *, reason: str = "") -> Result:
    """Delete every channel this bot is allowed to, and report the rest by name."""
    action = "channel_delete"
    allowed, refused = deletable_channels(guild, channels)
    names = [str(getattr(channel, "name", "?")) for channel in allowed]
    if not allowed:
        why = (
            "; ".join(f"#{getattr(c, 'name', '?')}: {reason_}" for c, reason_ in refused)
            or "no channels were given"
        )
        return Result(action=action, ok=False, detail=f"Nothing to delete — {why}.", refused_by="guard")

    result = Result(action=action, ok=False, count=len(allowed), names=names, reason=reason)
    failed: list[str] = []

    async def call():
        for channel in allowed:
            try:
                await channel.delete(reason=actor.reason_line(reason))
            except Exception as exc:  # noqa: BLE001 — one refusal does not stop the rest
                failed.append(f"#{getattr(channel, 'name', '?')} ({type(exc).__name__})")

    ok, detail = await _write(action, guild, None, actor, reason, call)
    result.ok = ok and not failed
    result.detail = detail
    if failed:
        # Partial is reported as partial. A card that said "deleted 4 channels"
        # over three deletions is the kind of log entry somebody later trusts.
        result.detail = (
            f"{len(allowed) - len(failed)} of {len(allowed)} deleted; "
            f"Discord refused {', '.join(failed)}."
        )
        result.names = [name for name in names if f"#{name}" not in " ".join(failed)]
    if refused:
        note = "; ".join(f"#{getattr(c, 'name', '?')} — {why}" for c, why in refused)
        result.detail = (result.detail + " " if result.detail else "") + f"Left alone: {note}."
    return await _finish(result, guild, None, actor, reason, fallback_target=guild)


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #

async def create_role(
    guild, actor: Actor, name: str, *, colour: int | None = None, reason: str = ""
) -> Result:
    action = "role_create"
    cleaned = clean_name(name)
    problem = name_problem(cleaned, what="role")
    if problem:
        return Result(action=action, ok=False, detail=problem, refused_by="input")
    if cleaned.lower() in ("everyone", "here"):
        return Result(
            action=action, ok=False,
            detail=f"`{cleaned}` is a mention Discord already owns — pick another name.",
            refused_by="input",
        )

    result = Result(action=action, ok=False, count=1, names=[cleaned], reason=reason)

    async def call():
        role = await guild.create_role(
            name=cleaned,
            colour=discord.Colour(colour) if colour is not None else discord.Colour.default(),
            reason=actor.reason_line(reason),
        )
        result.names = [str(getattr(role, "name", cleaned))]

    ok, detail = await _write(action, guild, None, actor, reason, call)
    result.ok, result.detail = ok, detail
    return await _finish(result, guild, None, actor, reason, fallback_target=guild)


async def delete_role(guild, actor: Actor, role, *, reason: str = "") -> Result:
    action = "role_delete"
    problem = deletable_role(guild, role)
    if problem:
        return Result(action=action, ok=False, detail=problem, refused_by="guard")
    result = Result(
        action=action, ok=False, count=1, names=[str(getattr(role, "name", "?"))], reason=reason
    )

    async def call():
        await role.delete(reason=actor.reason_line(reason))

    ok, detail = await _write(action, guild, role, actor, reason, call)
    result.ok, result.detail = ok, detail
    return await _finish(result, guild, role, actor, reason)


async def purge_role(guild, actor: Actor, role, *, reason: str = "") -> Result:
    """Take a role off every member who has it, without deleting the role.

    The count is the *members*, not the role: this is the action whose blast
    radius is invisible in its own command line, which is why the door reports how
    many people it touched and the caller is expected to have confirmed the size.
    """
    action = "role_purge"
    problem = deletable_role(guild, role)
    if problem:
        return Result(action=action, ok=False, detail=problem, refused_by="guard")
    members = list(getattr(role, "members", []) or [])
    if not members:
        return Result(
            action=action, ok=False,
            detail=f"Nobody has **{getattr(role, 'name', 'that role')}**, so there is nothing to take away.",
            refused_by="guard",
        )

    result = Result(
        action=action, ok=False, count=len(members),
        names=[str(getattr(member, "display_name", member)) for member in members[:MAX_LISTED]],
        reason=reason,
    )
    failed: list[str] = []

    async def call():
        for member in members:
            try:
                await member.remove_roles(role, reason=actor.reason_line(reason))
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{getattr(member, 'name', '?')} ({type(exc).__name__})")

    ok, detail = await _write(action, guild, role, actor, reason, call)
    result.ok = ok and not failed
    result.detail = detail
    if failed:
        result.detail = (
            f"{len(members) - len(failed)} of {len(members)} members had it removed; "
            f"Discord refused {len(failed)}."
        )
        print(f"[STRUCTURE] role purge in guild {getattr(guild, 'id', 0)}: {', '.join(failed)}", flush=True)
    return await _finish(result, guild, role, actor, reason)


# --------------------------------------------------------------------------- #
# The card a confirmation prompt carries
# --------------------------------------------------------------------------- #

def subject_for(action: str, items) -> str:
    """A short noun phrase for a prompt: ``"3 channels"``, ``"12 members"``.

    ``confirmations.guard`` takes the *count* separately because the ceiling is
    about size; this is the sentence that tells somebody what the number counts.
    """
    items = list(items or [])
    if action.startswith("channel"):
        return "channel" if len(items) == 1 else "channels"
    if action == "role_purge":
        return "member holding that role" if len(items) == 1 else "members holding that role"
    return "role" if len(items) == 1 else "roles"
