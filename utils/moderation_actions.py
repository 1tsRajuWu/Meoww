"""Member actions as *facts*, so the website and the slash commands cannot disagree.

The dashboard grew a members page, and that made a second caller of powers that
until now only existed behind ``/kick``, ``/ban``, ``/mute`` and ``/warn``. Two
callers of one power is how a product ends up with two answers to "what happened
when somebody was banned", and this project already fixed that shape once: the
bot's own automod timed people out with no record at all until one door
(:mod:`utils.modlog`) was written for every writer.

So this module is the door for member actions, and it holds the three things that
must be identical whichever surface asked:

* **Who may be touched** — :func:`target_problem`. It used to live in the cog as
  ``_mod_target_problem`` and the cog still imports it from here, because the
  guard is not a UI opinion: a web panel that let an administrator kick the server
  owner, or somebody with a role above theirs, would be a worse bug than a
  missing feature.
* **What actually happened** — :func:`apply`. The Discord call, whether it was
  accepted or refused, the case it becomes and the card the server's own
  moderation log receives. A refusal is recorded as a refusal (``not muted``),
  exactly as automod's record does, because a log that only holds successes reads
  like a filter that works.
* **Who asked** — :class:`Actor`, which carries the difference between a person
  in Discord and a person in a browser. It matters for two reasons: the reason
  line needs to say *where* the order came from, and the mod case's
  ``moderator_id`` is the only place a web-signed-in administrator's identity is
  written down — Discord's own audit log credits the bot, because the bot is what
  performed the action.

The web path is intentionally *not* a re-implementation of the commands: the
commands do their own cards and DMs, this module does the action and the record,
and they share both. ``tests/test_member_actions.py`` pins that with the
technique the automod record uses — a source-reading guard that fails if a
moderation command performs a Discord write without going through
:func:`apply`.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from . import db, modlog

#: The actions this module performs, and what each one is called where a person
#: reads it. One table, because the web panel, the record and the tests all need
#: the verb and three hand-written copies is how they drift.
#:
#: The structure actions at the end belong to ``utils.structure``, the second door
#: onto server state. They live here because a *case* is one list: a server's
#: moderation log drawing "Ban", "Kick" and "Channel deleted" from three tables
#: would number them in three places, and `Case #12` has to mean one thing.
ACTIONS: dict[str, str] = {
    "warn": "Warn",
    "timeout": "Mute",
    "timeout_remove": "Unmute",
    "kick": "Kick",
    "ban": "Ban",
    "unban": "Unban",
    "clear_warnings": "Warnings cleared",
    # --- structure (utils/structure.py) ---
    "channel_create": "Channel created",
    "channel_delete": "Channel deleted",
    "channel_archive": "Channels cleared from a category",
    "role_create": "Role created",
    "role_delete": "Role deleted",
    "role_purge": "Role removed from members",
}

#: How long a timeout may be, in minutes. Discord's own ceiling is 28 days; the
#: floor exists because "time out for 0 minutes" is a command that does nothing.
TIMEOUT_MINUTES = (1, 40320)
#: How many days of a banned member's messages may go with them.
BAN_DELETE_DAYS = (0, 7)
#: What a reason is when nobody gave one. The same sentence the commands use, so
#: the case list cannot tell which door an entry came through by its wording.
NO_REASON = "No reason provided"
#: Discord's ceiling on a case reason, and the length this module trims to. A web
#: panel accepts whatever somebody pastes; a log entry is not a place to keep a
#: 40,000-character essay, and the field has a limit that would refuse the card.
MAX_REASON = 400


@dataclass(frozen=True)
class Actor:
    """Who is asking, and from where.

    ``member`` is the actor's own :class:`discord.Member` when one could be read —
    the hierarchy guard needs it — and ``None`` when the panel could not fetch it.
    A ``None`` here is not permission to skip the guard: :func:`apply` refuses
    instead, because "I could not check who you are in that server" must not
    degrade into "so I will act anyway".
    """

    id: int
    name: str
    member: object = None
    source: str = "discord"

    @property
    def from_website(self) -> bool:
        return self.source == "dashboard"

    @property
    def label(self) -> str:
        """How the actor is named in a Discord audit-log reason."""
        return f"{self.name} (website)" if self.from_website else self.name

    def reason_line(self, reason: str) -> str:
        """The ``reason`` Discord records against its own audit entry."""
        return f"{self.label}: {reason}"


@dataclass
class Outcome:
    """What one action did, or why it did not.

    A refusal is a *result* here rather than an exception, because every caller
    has something to say about it: the web panel answers with the sentence, the
    record notes it, and neither should have to guess between "Discord said no"
    and "this code crashed".
    """

    action: str
    ok: bool
    target_id: int = 0
    target_name: str = ""
    detail: str = ""
    case_id: int | None = None
    reason: str = NO_REASON
    refused_by: str = ""
    #: The clamped duration a timeout actually used, so a panel can say *30
    #: minutes* rather than echoing what somebody typed.
    minutes: int = 0

    @property
    def verb(self) -> str:
        return ACTIONS.get(self.action, self.action.title())

    def to_dict(self) -> dict:
        return {
            "success": self.ok,
            "action": self.action,
            "verb": self.verb,
            "target_id": str(self.target_id or ""),
            "target_name": self.target_name,
            "detail": self.detail,
            "case_id": self.case_id,
            "reason": self.reason,
            "refused_by": self.refused_by,
        }


def target_problem(actor, target) -> str | None:
    """Refuse self / owner / bot / equal-or-higher role moderation targets.

    Moved here from the cog when the website became a second caller: this is the
    one rule that decides whether an action is allowed to touch somebody at all,
    and a second implementation of it is how a panel ends up able to do something
    a command cannot.
    """
    if target is None:
        return "That member could not be read in this server."
    if getattr(target, "id", None) == getattr(actor, "id", None):
        return "You cannot moderate yourself."
    guild = getattr(actor, "guild", None) or getattr(target, "guild", None)
    owner_id = getattr(guild, "owner_id", None)
    if owner_id is not None and getattr(target, "id", None) == owner_id:
        return "You cannot moderate the server owner."
    me = getattr(guild, "me", None)
    if me is not None and getattr(target, "id", None) == getattr(me, "id", None):
        return "You cannot moderate me."
    actor_perms = getattr(actor, "guild_permissions", None)
    target_perms = getattr(target, "guild_permissions", None)
    if getattr(target_perms, "administrator", False) and not getattr(actor_perms, "administrator", False):
        return "You cannot moderate an administrator."
    actor_top = getattr(actor, "top_role", None)
    target_top = getattr(target, "top_role", None)
    if (
        actor_top is not None
        and target_top is not None
        and getattr(actor, "id", None) != owner_id
        and target_top >= actor_top
    ):
        return (
            f"**{getattr(target, 'display_name', 'That member')}** has a role at or "
            f"above yours (**{getattr(target_top, 'name', 'their role')}**)."
        )
    return None


def body_embed(action: str, guild, target, actor: Actor, reason: str, case_id: int | None):
    """The card a server's moderation log receives, for either door.

    One builder, because the log is the record: a card from the website that
    looked different from a card from ``/ban`` would make the two paths visible in
    exactly the list people read to find out what happened.
    """
    import discord  # local: this module is imported by the web app and by tests

    from .style import ACCENT

    emoji = {
        "warn": "⚠️", "timeout": "🔇", "timeout_remove": "🔊",
        "kick": "👢", "ban": "🔨", "unban": "✅", "clear_warnings": "🧹",
        "channel_create": "🏗️", "channel_delete": "🗑️", "channel_archive": "🗄️",
        "role_create": "🏷️", "role_delete": "🗑️", "role_purge": "🧨",
    }.get(action, "🛡️")
    title = f"{emoji} {ACTIONS.get(action, action.title())}"
    if case_id:
        title += f" — Case #{case_id}"
    embed = discord.Embed(
        title=title,
        color=ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    if getattr(target, "display_avatar", None) is not None:
        embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Target", value=f"`{getattr(target, 'name', 'unknown')}`\n`{getattr(target, 'id', 0)}`", inline=True)
    embed.add_field(name="By", value=f"{actor.label}\n`{actor.id}`", inline=True)
    embed.add_field(name="Reason", value=(reason or NO_REASON)[:1024], inline=False)
    embed.set_footer(text="via the website" if actor.from_website else "via a command")
    return embed


async def record(guild, action: str, target, actor: Actor, reason: str, *, case_id: int | None = None):
    """Open a case for an action that happened, and write it to the server's log.

    Called only for actions Discord **accepted**. The refusal path records a
    different sentence (:func:`failure_note`) rather than a case, because a case
    list is read as a list of things that were done.
    """
    if case_id is None:
        try:
            case_id = await db.add_mod_case(
                int(getattr(guild, "id", 0)), ACTIONS.get(action, action.title()),
                int(getattr(target, "id", 0)), actor.id, reason or NO_REASON,
            )
        except Exception as exc:  # noqa: BLE001 — a record that raises loses the action too
            print(f"[MOD] could not open a case for '{action}': {exc}", flush=True)
            case_id = None
    await modlog.post(
        guild,
        body_embed(action, guild, target, actor, reason, case_id),
        what=f"the '{action}' case" + (f" #{case_id}" if case_id else ""),
    )
    return case_id


#: What the member is told, per action. One table rather than seven hand-written
#: embeds: the person on the receiving end should not be able to tell which door
#: the order came through, and a web panel that quietly stopped sending these
#: would be the version of this feature nobody trusts.
NOTICES: dict[str, tuple[str, str]] = {
    "warn": ("⚠️ You have been warned", "Warning"),
    "timeout": ("🔇 You have been timed out", "Timeout"),
    "kick": ("👢 You have been kicked", "Kick"),
    "ban": ("🔨 You have been banned", "Ban"),
}


async def _notify(action: str, guild, target, reason: str) -> None:
    """DM the member, best-effort. A closed inbox is not a reason to refuse."""
    title = NOTICES.get(action)
    if title is None:
        return
    import discord

    from .style import send_card

    try:
        embed = discord.Embed(title=title[0], color=ACCENT_FALLBACK, timestamp=discord.utils.utcnow())
        embed.add_field(name="Server", value=str(getattr(guild, "name", "this server")), inline=True)
        embed.add_field(name=title[1], value=(reason or NO_REASON)[:1024], inline=False)
        await send_card(target, embed=embed)
    except Exception:  # noqa: BLE001 — DMs closed, or no shared server
        pass


def failure_note_card(action: str, guild, target, actor: Actor, detail: str):
    """The card a *refused* action leaves in the log, so a refusal is not silent.

    Synchronous on purpose: it builds an embed, and a coroutine here was a trap
    the first version of this module fell into (the card was created and never
    awaited, so the log stayed empty and the only evidence was a runtime warning
    nobody reads).
    """
    import discord

    embed = discord.Embed(
        title=f"🛡️ {ACTIONS.get(action, action.title())} was not applied",
        description=detail,
        color=0xE67E22,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Target", value=f"`{getattr(target, 'id', 0)}`", inline=True)
    embed.add_field(name="Asked by", value=f"{actor.label}\n`{actor.id}`", inline=True)
    embed.set_footer(text="via the website" if actor.from_website else "via a command")
    return embed


#: Discord's own red, for the DM embeds. Spelled once.
ACCENT_FALLBACK = 0xE74C3C


def failure_note(action: str, detail: str) -> str:
    """What a refused action says in the operator's log.

    The same rule automod's record follows: a timeout Discord refused is written
    as *not muted*, because a log where a refusal is silent reads like a filter
    that worked.
    """
    verb = ACTIONS.get(action, action.title())
    return f"{verb} was **not** applied — {detail}"


def _trim(reason: str) -> str:
    text = (reason or "").strip() or NO_REASON
    return text[:MAX_REASON]


async def _discord_call(action: str, call):
    """``(ok, detail)`` for one Discord write, with the refusals in words.

    Never raises: a web route that 500s on a permission problem tells the person
    nothing, and this project's whole register is that a refusal says what to do.
    """
    import discord

    try:
        await call()
    except discord.Forbidden:
        return False, _forbidden(action)
    except discord.NotFound:
        return False, "Discord says that member is not there any more."
    except discord.HTTPException as exc:
        return False, f"Discord answered with an error ({getattr(exc, 'status', 'unknown')}): {exc}"
    except Exception as exc:  # noqa: BLE001 — a transport failure is a refusal too
        return False, f"That did not reach Discord ({type(exc).__name__}: {exc})"
    return True, ""


def _forbidden(action: str) -> str:
    """The fix, named, because "Forbidden" sends nobody anywhere useful."""
    if action in ("ban", "unban", "kick"):
        return (
            "I do not have that permission — I need **Ban Members** / **Kick "
            "Members**, and my own role has to sit above that member's."
        )
    if action in ("timeout", "timeout_remove"):
        return (
            "I need the **Moderate Members** permission, and my role has to sit "
            "above that member's."
        )
    return "I do not have the permission to do that here."


@dataclass
class Request:
    """One member action, as the website or a command fills it in."""

    guild: object
    action: str
    target: object
    actor: Actor
    reason: str = NO_REASON
    minutes: int = 0
    delete_days: int = 0
    extra: dict = field(default_factory=dict)


async def apply(request: Request) -> Outcome:
    """Run one member action, record it, and report it. Never raises.

    The order is deliberate and it is the order the commands already used:
    **guard → Discord → case → log**. Recording before Discord would leave the
    server's log describing something that did not happen, which is worse than a
    missing entry, because the log is what somebody reads to find out.
    """
    guild = request.guild
    action = request.action
    target = request.target
    actor = request.actor
    reason = _trim(request.reason)
    outcome = Outcome(
        action=action,
        ok=False,
        target_id=int(getattr(target, "id", 0) or 0),
        target_name=str(getattr(target, "display_name", "") or getattr(target, "name", "")),
        reason=reason,
    )
    if action not in ACTIONS:
        outcome.detail = f"`{action}` is not an action this panel performs."
        return outcome

    # The hierarchy guard needs the *actor's* member object. A web session that
    # could not be resolved to one is refused rather than waved through: the
    # alternative is a panel that can do what a command cannot.
    actor_member = actor.member
    if actor_member is None:
        outcome.detail = (
            "I could not read your own member in that server, so I did not do "
            "this — try again in a moment."
        )
        outcome.refused_by = "actor-unknown"
        return outcome
    if action != "unban":
        problem = target_problem(actor_member, target)
        if problem:
            outcome.detail = problem
            outcome.refused_by = "guard"
            return outcome

    import discord

    if action == "warn":
        async def call():
            await db.add_warning(int(guild.id), outcome.target_id, reason)
    elif action == "timeout":
        minutes = max(TIMEOUT_MINUTES[0], min(TIMEOUT_MINUTES[1], int(request.minutes or 0)))
        outcome.minutes = minutes
        outcome.reason = _trim(f"{reason} ({minutes}m)")
        delta = datetime.timedelta(minutes=minutes)

        async def call():
            await target.timeout(delta, reason=actor.reason_line(outcome.reason))
    elif action == "timeout_remove":
        outcome.reason = "Timeout removed"

        async def call():
            await target.timeout(None, reason=actor.reason_line("Timeout removed"))
    elif action == "kick":
        async def call():
            await target.kick(reason=actor.reason_line(reason))
    elif action == "ban":
        days = max(BAN_DELETE_DAYS[0], min(BAN_DELETE_DAYS[1], int(request.delete_days or 0)))

        async def call():
            await target.ban(reason=actor.reason_line(reason), delete_message_days=days)
    elif action == "unban":
        user = target
        if not isinstance(user, discord.User):
            try:
                user = await guild.fetch_user(int(getattr(target, "id", 0)))
            except Exception:
                user = discord.Object(id=int(getattr(target, "id", 0)))

        async def call():
            await guild.unban(user, reason=actor.reason_line(reason))
        outcome.target_name = str(getattr(user, "name", "") or outcome.target_name)
        target = user
    else:  # clear_warnings
        async def call():
            await db.clear_warnings(int(guild.id), outcome.target_id)
        outcome.reason = "All warnings cleared"

    await _notify(action, guild, target, outcome.reason)
    ok, detail = await _discord_call(action, call)
    outcome.ok = ok
    outcome.detail = detail
    if not ok:
        outcome.refused_by = "discord"
        # A refusal reaches the server's log as a refusal. The commands used to
        # answer only the moderator who ran them, which left the log looking as
        # if the action had not been attempted at all.
        await modlog.post(
            guild,
            failure_note_card(action, guild, target, actor, detail),
            what=f"the refused '{action}'",
        )
        return outcome

    outcome.case_id = await record(guild, action, target, actor, outcome.reason)
    return outcome


# --------------------------------------------------------------------------- #
# The roster the panel draws
# --------------------------------------------------------------------------- #
#: How many members one search answers with, and why it is not "all of them":
#: a server with 40,000 members would render a list nobody scrolls, and the web
#: request that carried it would be the biggest thing this process does all day.
ROSTER_LIMIT = 40
#: Discord's own cap on a member search, used as the fetch size when the local
#: cache cannot answer.
ROSTER_FETCH = 1000


def _match(member, needle: str) -> bool:
    """Whether one member matches a search — id, name, nickname or tag."""
    if not needle:
        return True
    haystacks = (
        str(getattr(member, "id", "")),
        str(getattr(member, "name", "")),
        str(getattr(member, "display_name", "")),
        str(getattr(member, "global_name", "") or ""),
    )
    return any(needle in value.lower() for value in haystacks)


def member_row(member) -> dict:
    """One member as the panel needs it: ids as text, nothing invented."""
    timed_out_until = getattr(member, "timed_out_until", None)
    return {
        "id": str(getattr(member, "id", "")),
        "name": str(getattr(member, "name", "")),
        "display_name": str(getattr(member, "display_name", "") or getattr(member, "name", "")),
        "bot": bool(getattr(member, "bot", False)),
        "avatar": str(getattr(getattr(member, "display_avatar", None), "url", "") or ""),
        "joined_at": int(member.joined_at.timestamp()) if getattr(member, "joined_at", None) else None,
        "created_at": int(member.created_at.timestamp()) if getattr(member, "created_at", None) else None,
        "top_role": str(getattr(getattr(member, "top_role", None), "name", "") or ""),
        "roles": [
            {"id": str(getattr(role, "id", "")), "name": str(getattr(role, "name", ""))}
            for role in list(getattr(member, "roles", []) or [])[1:]
        ][:12],
        "timeout_seconds": (
            max(0, int((timed_out_until - _now()).total_seconds())) if timed_out_until else 0
        ),
    }


def _now():
    import discord

    return discord.utils.utcnow()


def _roster_payload(members, needle: str, limit: int, *, complete: bool, queried: bool) -> dict:
    """Rows and counts out of a list of members. Shared by both readers."""
    found = [
        member for member in members
        if _match(member, needle)
    ]
    found.sort(key=lambda m: str(getattr(m, "display_name", "")).lower())
    total = len(found)
    return {
        "members": [member_row(member) for member in found[:limit]],
        "matched": total,
        "shown": min(limit, total),
        "cache": len(members),
        "queried": queried,
        "complete": complete,
    }


def roster_cached(guild, needle: str = "", *, limit: int = ROSTER_LIMIT, with_bots: bool = True):
    """The roster out of the gateway's own cache — or ``None`` when it is empty.

    This exists so a *page render* can draw a member list without hopping to the
    bot's event loop, which matters because the dashboard is Flask on the main
    thread and a page cannot await. The cache is complete with the Server Members
    intent on, which is the deployment this is written for; when it is empty the
    caller asks the asynchronous :func:`roster`, which queries Discord once.
    """
    members = list(getattr(guild, "members", []) or [])
    if not members:
        return None
    if not with_bots:
        members = [member for member in members if not getattr(member, "bot", False)]
    return _roster_payload(
        members, (needle or "").strip().lower(), limit,
        complete=bool(getattr(guild, "chunked", False)), queried=False,
    )


async def roster(guild, needle: str = "", *, limit: int = ROSTER_LIMIT, with_bots: bool = True) -> dict:
    """A page of members matching a search, read from the cache and never from a fetch storm.

    ``guild.members`` is only complete with the Server Members intent; when the
    cache is empty this asks Discord once (a member query) rather than reporting a
    server with no members in it, because those two states look identical on a
    page and only one of them is true.
    """
    needle = (needle or "").strip().lower()
    members = list(getattr(guild, "members", []) or [])
    queried = False
    answered = False
    if not members and hasattr(guild, "query_members"):
        queried = True
        try:
            members = list(await guild.query_members(limit=ROSTER_FETCH) or [])
            # *Answered* is the load-bearing half: a query that was attempted and
            # failed is not a reading either, and reporting an empty list as
            # complete would turn "I could not ask" into "there is nobody" — the
            # exact confusion the Live page and the media watch both exist to
            # avoid.
            answered = True
        except Exception as exc:  # noqa: BLE001 — a query that fails is a reading we do not have
            print(f"[PANEL] member search failed: {exc}", flush=True)
            members = []
    if not with_bots:
        members = [member for member in members if not getattr(member, "bot", False)]
    return _roster_payload(
        members, needle, limit,
        complete=bool(getattr(guild, "chunked", False)) or answered, queried=queried,
    )


async def banned(guild, *, limit: int = ROSTER_LIMIT) -> list[dict]:
    """The ban list, as rows: a panel that cannot show one cannot offer an unban."""
    rows: list[dict] = []
    try:
        async for entry in guild.bans(limit=limit):
            if entry is None:
                continue
            user = getattr(entry, "user", None)
            rows.append({
                "id": str(getattr(user, "id", "")),
                "name": str(getattr(user, "name", "")),
                "display_name": str(getattr(user, "global_name", "") or getattr(user, "name", "")),
                "reason": str(getattr(entry, "reason", "") or ""),
            })
    except Exception as exc:  # noqa: BLE001 — no Ban Members permission is a reading, not a crash
        print(f"[PANEL] could not read the ban list: {exc}", flush=True)
        return []
    return rows


async def warning_rows(guild_id, user_id) -> list[dict]:
    """One member's warnings, newest first, for the panel's own list."""
    try:
        return await db.get_warnings(int(guild_id), int(user_id))
    except Exception as exc:  # noqa: BLE001
        print(f"[PANEL] could not read warnings: {exc}", flush=True)
        return []
