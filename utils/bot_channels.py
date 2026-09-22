"""Where each server wants the bot to talk.

The bot lives in more than one server now, and every server keeps its own house
channels: where release notes land, where the bot reports its own failures, where
a reported bug is filed, and where moderation cases are written. Only the last of
those had a per-server home. The update channel was a literal in ``bot.py``
naming one server's channel, the error path knew only a process-wide webhook, and
a server that had never run ``/bot db`` had nowhere to put its moderation
cases — which is why ``_send_log`` dropped them without a word.

Binding follows the convention ``/bot db`` already used for ``#logs``: a server
that has never been configured is matched by channel *name*, once, at boot. After
that the stored setting is authoritative. That asymmetry is deliberate — a server
that renames ``#bot-error`` next month should not quietly have the bot's failures
land wherever the new name happens to point, and an administrator who moves a
channel on purpose can say so.

Nothing here talks to Discord beyond the objects it is handed, so the whole
module is exercised with stand-in guilds.
"""

from utils import config_audit, db

#: The house channels every server may configure. The names are also the setting
#: keys' prefixes: ``"error"`` is stored as ``error_channel_id``.
ROLES: tuple[str, ...] = ("update", "error", "bug", "commands", "log", "test", "guilds", "cmdlog")

#: The roles that only mean something on the bot's own server: where it records
#: the servers it is added to and taken out of, and every command anyone runs.
#: No other server has anything to bind them for, so they are not bound there —
#: otherwise every server's settings would fill up with rows naming channels the
#: bot never reads.
OPERATOR_ROLES: frozenset[str] = frozenset({"guilds", "cmdlog"})

#: The server the bot calls home: the one whose ``#update``, ``#bot-error`` and
#: ``#bug`` exist to receive what the bot has to say about itself. It is the
#: fallback destination for a report, never the first choice.
#:
#: Declared as a default rather than hardcoded because *which* server this is is a
#: deployment fact — the fallback for a report has to be somewhere a report can
#: actually be read, and only the operator knows where that is.
_DEFAULT_BOT_SERVER_GUILD_ID = 1550227368889684058


def bot_server_guild_id() -> int:
    """The id of the bot's own server, from ``BOT_SERVER_GUILD_ID`` when set.

    A malformed value falls back to the default rather than raising: this is read
    on the path that files a bug report, and a typo in an environment variable
    must not be the reason a report cannot be filed.
    """
    raw = config_audit.get("BOT_SERVER_GUILD_ID")
    if not raw:
        return _DEFAULT_BOT_SERVER_GUILD_ID
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_BOT_SERVER_GUILD_ID

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

#: The role that marks somebody who **operates this bot**, as opposed to somebody
#: who should be told about a release. Deliberately a shorter list than
#: :data:`NOTIFY_ROLE_NAMES`: a ping is a courtesy and falling back to *admin* on a
#: server with a different role naming is helpful, while a gate that fell back the
#: same way would make every administrator of every server an operator of this one.
DEV_ROLE_NAMES: tuple[str, ...] = ("bot-dev", "dev", "developer")

#: The Dev role in the bot's own server, by id. This is the role ``bot.py`` has
#: always pinged when a release note lands, so the id is what the server that has
#: it keeps working through a rename — the same reason ``notify_role`` takes one.
#: It lives here rather than in ``bot.py`` because this module is what owns "who is
#: an operator", and two copies of an id is how the gate and the ping drift apart.
DEV_ROLE_ID = 1548948461301534791

#: The update channel this bot's release notes were hardcoded to before a server
#: could say where it wanted them. It is still honoured for the server that owns it,
#: so nothing changes for the one that has been reading them all along — and its
#: *server* is the second answer to "which server is this bot's own", which is why
#: the id lives here rather than in ``bot.py``: two readers now ask.
LEGACY_UPDATE_CHANNEL_ID = 1548948620600942632


def is_own_server(guild) -> bool:
    """Whether this guild is a server the bot considers **its own**.

    There are two, and that is the fact this function exists to fix. The one
    ``BOT_SERVER_GUILD_ID`` names is where the bot's own output lands today —
    ``#bot-error``, ``#bug-report``, ``#active-servers``, ``#real-time-cmd-data`` —
    and the one that holds :data:`LEGACY_UPDATE_CHANNEL_ID` is the server the notes
    were hardcoded to *first*, which is also where the Dev role the release-note
    ping has always tagged was created. Naming one of them and not the other is a
    gate that refuses in the server the person answering is sitting in, which is
    the wrong answer to the only question it is asked.

    Both are ids a member cannot forge: neither ``BOT_SERVER_GUILD_ID`` nor the
    legacy channel id is anything a server can acquire by naming a channel. A guild
    that holds neither is a server the bot is a **visitor** in, whatever its
    channels are called.
    """
    if guild is None:
        return False
    guild_id = getattr(guild, "id", None)
    try:
        if guild_id is not None and int(guild_id) == bot_server_guild_id():
            return True
    except (TypeError, ValueError):
        pass
    holder = getattr(guild, "get_channel", None)
    if holder is None:
        return False
    return holder(LEGACY_UPDATE_CHANNEL_ID) is not None


def dev_role(guild):
    """The role that marks an operator *in this server*, or ``None``.

    Resolved the way the release-note ping resolves its role — the known id first,
    then the server's own roles by name, highest first — so a server that renamed
    ``Dev`` still gets an operator and a server with two roles called ``Dev``
    answers with the one that carries the permissions. What it deliberately does
    **not** do is invent one: a server with no such role has nobody who may answer
    an anonymous question, and that is the correct answer rather than a fallback.
    """
    if guild is None:
        return None
    by_id = getattr(guild, "get_role", lambda _id: None)(DEV_ROLE_ID)
    if by_id is not None:
        return by_id
    by_name: dict[str, object] = {}
    for role in reversed(list(getattr(guild, "roles", []) or [])):
        name = (getattr(role, "name", "") or "").strip().lower()
        if name:
            by_name.setdefault(name, role)
    for name in DEV_ROLE_NAMES:
        role = by_name.get(name)
        if role is not None and not getattr(role, "is_default", lambda: False)():
            return role
    return None

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
    # Where an operator tries things out. `/bot push` reports there when it can,
    # because that is the channel they are watching while they test.
    "test": ("test-ground", "testground", "beta-testing", "testing", "test", "beta"),
    # The operator's own two logs, in the operator's own server: who adds the bot
    # and who takes it away, and every command anyone runs.
    "guilds": ("active-servers", "bot-servers", "guild-log", "guild-logs", "server-log"),
    "cmdlog": (
        "real-time-cmd-data", "command-log", "command-logs", "command-data",
        "cmd-log", "cmdlog",
    ),
}


def roles_for(guild_id) -> tuple[str, ...]:
    """The channel roles that mean something for *this* server.

    Everything but :data:`OPERATOR_ROLES` — those two belong to the bot's own
    server, so they are only bound there.
    """
    try:
        is_operator = guild_id is not None and int(guild_id) == bot_server_guild_id()
    except (TypeError, ValueError):
        is_operator = False
    if is_operator:
        return ROLES
    return tuple(role for role in ROLES if role not in OPERATOR_ROLES)


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


async def report_targets(guild_id, role: str = "bug", client=None) -> list[int]:
    """Where a report filed in ``guild_id`` may be posted, best target first.

    A report filed in a server belongs in *that* server — it is the server's
    conversation, and the people who can act on it are there. But a server that
    never made itself a ``#bug`` channel used to be a dead end: the report was
    refused with "this server has no bug-report channel", which reads to the
    person filing it as the bot losing their report. So the bot's own server is
    the fallback, and neither being available is the only case that refuses.

    Ids, not channels: the caller holds the client and knows which guilds it can
    see. No duplicates, and order is deterministic — the same report never lands
    in two places.

    ``client`` is what makes the fallback survive a server that was never bound:
    with it, the bot's own server is *looked* at for a bug channel by name when no
    binding exists there. That is the binder's own rule reused at the moment it
    matters, and it is deliberately limited to the last target — a report may not
    be refused because a boot has not happened yet, but nothing is created, and a
    server that has renamed its own channel still gets the channel it bound.
    """
    fallback_guild = bot_server_guild_id()
    targets: list[int] = []
    for candidate in (guild_id, fallback_guild):
        if candidate is None:
            continue
        channel_id = await bound(candidate, role)
        if channel_id is None and candidate == fallback_guild and client is not None:
            get_guild = getattr(client, "get_guild", None)
            guild = get_guild(candidate) if get_guild is not None else None
            if guild is not None:
                channel_id = pick(getattr(guild, "text_channels", []), role)
        if channel_id is not None and channel_id not in targets:
            targets.append(channel_id)
    return targets


async def ensure_bound(guild) -> dict[str, int]:
    """Bind this server's house channels by name — only the ones it has not set.

    Returns the roles bound *now*, which is empty on every boot after the first,
    so calling it for every guild on every start costs four reads and nothing
    else. A server whose channels are named none of the candidates simply stays
    unconfigured.
    """
    bound_now: dict[str, int] = {}
    channels = list(getattr(guild, "text_channels", []) or [])
    for role in roles_for(getattr(guild, "id", None)):
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
