"""Where each server wants the bot to talk.

The bot lives in more than one server now, and every server keeps its own house
channels: where release notes land, where the bot reports its own failures, where
a reported bug is filed, and where moderation cases are written. Only the last of
those had a per-server home. The update channel was a literal in ``bot.py``
naming one server's channel, the error path knew only a process-wide webhook, and
a server that had never run ``/updatedb`` had nowhere to put its moderation
cases — which is why ``_send_log`` dropped them without a word.

Binding follows the convention ``/updatedb`` already used for ``#logs``: a server
that has never been configured is matched by channel *name*, once, at boot. After
that the stored setting is authoritative. That asymmetry is deliberate — a server
that renames ``#bot-error`` next month should not quietly have the bot's failures
land wherever the new name happens to point, and an administrator who moves a
channel on purpose can say so.

Nothing here talks to Discord beyond the objects it is handed, so the whole
module is exercised with stand-in guilds.
"""

from utils import db

#: The house channels every server may configure. The names are also the setting
#: keys' prefixes: ``"error"`` is stored as ``error_channel_id``.
ROLES: tuple[str, ...] = ("update", "error", "bug", "commands", "log")

#: The roles whose setting predates this module and does not follow the naming.
_KEYS: dict[str, str] = {"commands": "bot_cmd_channel_id"}

#: Roles a release note may ping. Matched by name against the server's own roles
#: (decorations ignored), most specific first, and only after the role id this bot
#: has always pinged. A server with none of them gets a note with no mention in it
#: rather than an @everyone nobody asked for.
NOTIFY_ROLE_NAMES: tuple[str, ...] = (
    "bot-dev",
    "dev",
    "developer",
    "admin",
    "administrator",
    "owner",
)

#: Matched in order, most specific first, so ``#bug-report`` wins over ``#bug``
#: and a server that names its channel exactly gets it. No name appears under two
#: roles: an ambiguous name can only ever bind one thing, and guessing which is
#: how a log channel ends up full of release notes.
_NAMES: dict[str, tuple[str, ...]] = {
    "update": ("meow-bot-update", "bot-update", "bot-updates", "updates", "update"),
    "error": ("bot-error", "bot-errors", "error-log", "error-logs"),
    "bug": ("bug-report", "bug-reports", "report-a-bug", "bugs", "bug"),
    # Where level-up cards are allowed to appear. Leveling already reads this
    # setting; what was missing was anything that ever wrote it, so every server
    # but the one whose channel was hardcoded into the cog posted nothing.
    "commands": ("bot-cmd", "bot-commands", "level-up", "levelups", "commands"),
    "log": ("mod-logs", "mod-log", "modlogs", "server-logs", "logs"),
}


def setting_key(role: str) -> str:
    """The ``guild_settings`` key holding this role's channel."""
    if role not in ROLES:
        raise KeyError(f"unknown channel role: {role!r}")
    return _KEYS.get(role, f"{role}_channel_id")


def candidates(role: str) -> tuple[str, ...]:
    """The channel names that may bind this role, most specific first."""
    return _NAMES.get(role, ())


def normalize(name: str) -> str:
    """A channel or role name reduced to its letters and digits.

    Servers decorate names — ``💬・bot-cmd``, ``🤖 bot-logs`` — and an exact
    match against ``bot-cmd`` then finds nothing at all, so the bot's own
    channels stayed unbound in exactly the servers that had renamed them.
    """
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def pick(channels, role: str) -> int | None:
    """The id of the channel named for ``role``, or ``None``.

    Order comes from :func:`candidates` rather than from the guild, so the result
    does not depend on where Discord happens to put a channel in its cache. An
    exact name is preferred; a name that only matches once its decoration is
    stripped is the fallback for that same candidate.
    """
    by_name: dict[str, int] = {}
    by_normal: dict[str, int] = {}
    for channel in channels or ():
        name = (getattr(channel, "name", "") or "").lower()
        channel_id = getattr(channel, "id", None)
        if not name or not channel_id:
            continue
        by_name.setdefault(name, int(channel_id))
        normal = normalize(name)
        if normal:
            by_normal.setdefault(normal, int(channel_id))
    for name in candidates(role):
        found = by_name.get(name)
        if found is None:
            found = by_normal.get(normalize(name))
        if found:
            return found
    return None


async def bound(guild_id, role: str) -> int | None:
    """The channel this server has bound for ``role``, or ``None``.

    A missing, empty or unreadable setting all mean "not configured" — the
    callers fall back to their old behaviour rather than to a guess.
    """
    if guild_id is None:
        return None
    try:
        value = await db.get_setting(int(guild_id), setting_key(role))
    except Exception:
        return None
    if value is None or isinstance(value, bool):
        return None
    try:
        channel_id = int(value)
    except (TypeError, ValueError):
        return None
    # 0 is how an empty setting usually comes back, and it is nobody's channel.
    return channel_id if channel_id > 0 else None


async def all_bound(guild_id) -> dict[str, int | None]:
    """Every role's binding for a server, configured or not."""
    return {role: await bound(guild_id, role) for role in ROLES}


async def ensure_bound(guild) -> dict[str, int]:
    """Bind this server's house channels by name — only the ones it has not set.

    Returns the roles bound *now*, which is empty on every boot after the first,
    so calling it for every guild on every start costs four reads and nothing
    else. A server whose channels are named none of the candidates simply stays
    unconfigured.
    """
    bound_now: dict[str, int] = {}
    channels = list(getattr(guild, "text_channels", []) or [])
    for role in ROLES:
        if await bound(guild.id, role) is not None:
            continue
        found = pick(channels, role)
        if found is None:
            continue
        await db.set_setting(guild.id, setting_key(role), found)
        bound_now[role] = found
    return bound_now


def notify_role(guild, legacy_role_id: int | None = None):
    """The role to ping when a release note lands in this server, or ``None``.

    The id this bot has always pinged wins where that server has it, so the
    server that was already being tagged keeps exactly the ping it had. Every
    other server is matched by role name instead — which is how a server added
    later gets tagged at all, rather than the note arriving with no mention.
    """
    if legacy_role_id is not None:
        role = getattr(guild, "get_role", lambda _id: None)(legacy_role_id)
        if role is not None:
            return role
    by_name: dict[str, object] = {}
    # Highest role first, so a server with two roles sharing a name pings the one
    # that actually carries the permissions.
    for role in reversed(list(getattr(guild, "roles", []) or [])):
        name = (getattr(role, "name", "") or "").strip().lower()
        if name:
            by_name.setdefault(name, role)
    for name in NOTIFY_ROLE_NAMES:
        role = by_name.get(name)
        if role is not None and not getattr(role, "is_default", lambda: False)():
            return role
    return None


def describe(guild, bound_now: dict[str, int]) -> str:
    """A one-line summary of what was just bound, for the boot log."""
    parts = []
    for role, channel_id in bound_now.items():
        channel = guild.get_channel(channel_id) if hasattr(guild, "get_channel") else None
        parts.append(f"{role}=#{channel.name}" if channel is not None else f"{role}={channel_id}")
    return ", ".join(parts)
