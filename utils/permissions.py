"""Who may run what, declared once for every command.

A Discord bot has two separate audiences for a command, and they are easy to
confuse:

* **Enforcement** — what actually stops a member. For most commands that is an
  ``@app_commands.checks.has_permissions(...)`` check, which discord.py evaluates
  before the callback runs. For a few (the server owner's commands, the
  market's role) it is a check inside the callback instead.
* **Visibility** — whether Discord *shows* the command in a member's picker at
  all. That is ``default_member_permissions``, and it is the difference between
  a command being hidden from someone who cannot use it and being offered to
  them and then refused.

Both are declared here, in one table, for one reason: a permission model that
lives in forty decorators across thirteen files cannot be reviewed, and the way
it fails is silent — a new command ships with no gate at all, or an admin-only
command quietly loses its check. ``COMMAND_ACCESS`` is the whole model, and
``undeclared()`` plus the test-suite make a command without an entry a build
failure rather than a discovery.

This module does not replace the decorators; it must agree with them.
``apply_declared_permissions`` stamps the tree before it is published so the
picker matches, and ``tests/test_permissions.py`` asserts the stamped visibility
and the real runtime check agree for every single command — including by
*calling* each command's own checks with a fake interaction that has only the
declared permission.
"""
from __future__ import annotations

from typing import Iterator

import discord

#: The VIP bot application's id. The main bot and the VIP bot run this same
#: tree, but the VIP commands belong to the VIP picker only — the main bot
#: leaves them out of every scope it publishes, which takes them out of its
#: pickers and hands the ceiling slots back.
VIP_BOT_ID = 1558691012975534222
#: Root commands reserved to the VIP instance (see above).
VIP_ONLY_COMMANDS = frozenset({"premium", "bond", "music"})


def is_vip_app_id(app_id) -> bool:
    """Whether an application id is the VIP bot's."""
    try:
        return int(app_id) == VIP_BOT_ID
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Audiences
# --------------------------------------------------------------------------- #
EVERYONE = "everyone"
MODERATE = "moderate_members"
BAN = "ban_members"
KICK = "kick_members"
MESSAGES = "manage_messages"
CHANNELS = "manage_channels"
ROLES = "manage_roles"
GUILD = "manage_guild"
ADMIN = "administrator"
#: The **server owner**, checked inside the callback because Discord has no
#: "owner" permission bit. Declared as administrator so the picker hides it from
#: everyone who cannot pass it — a guild owner is always an administrator.
OWNER = "owner"
#: The **operator's own surfaces that a second person may reach**: the bot's owner,
#: or somebody holding the deployment's Dev role in the bot's own server. Discord
#: cannot express "this role, in that server" — a role is per guild and a
#: permission bit is not a membership — so this is a body check like OWNER, and the
#: one thing that separates it from OWNER is *who comes back true*: a Dev is not
#: necessarily an administrator, and an administrator elsewhere is not an operator
#: here. Both commands that declare it also refuse to run outside that server, which
#: is a second body check and is stated at the call site rather than modelled here,
#: because "which server" is not an audience.
DEV = "dev"
#: Whoever submitted the record (a custom quote), or staff. Also a body check.
SUBMITTER = "submitter"
#: The market's own role gate, checked in the cog's ``interaction_check``.
#: Deliberately declared as *visible*: Discord's permission bits cannot express
#: "the members holding role X", and declaring a permission instead would hide
#: the market from the very role allowed to trade.
MARKET = "market_role"

#: What Discord should show the picker, per audience. ``None`` means "visible to
#: everyone" — it says nothing about enforcement, which the table's tests pin.
PICKER_VISIBILITY: dict[str, discord.Permissions | None] = {
    EVERYONE: None,
    MODERATE: discord.Permissions(moderate_members=True),
    BAN: discord.Permissions(ban_members=True),
    KICK: discord.Permissions(kick_members=True),
    MESSAGES: discord.Permissions(manage_messages=True),
    CHANNELS: discord.Permissions(manage_channels=True),
    ROLES: discord.Permissions(manage_roles=True),
    GUILD: discord.Permissions(manage_guild=True),
    ADMIN: discord.Permissions(administrator=True),
    OWNER: discord.Permissions(administrator=True),
    # Deliberately OWNER's visibility, and deliberately not a bit of its own: an
    # operator is usually an administrator in the server the inbox lives in, and
    # showing the command to *everybody* so that a Dev who is not one can see it
    # would advertise the operator's inbox in every server the bot is in. A Dev who
    # cannot see it can be given the role that shows it, which is a decision; a
    # member reading a name they may not have is a leak.
    DEV: discord.Permissions(administrator=True),
    SUBMITTER: None,
    MARKET: None,
}

#: The runtime check each audience must have, as a ``Permissions`` instance the
#: test-suite builds a fake interaction from. The three body-checked audiences
#: (OWNER, SUBMITTER, MARKET) have no permission bit and are pinned by a marker
#: in the callback's own source instead.
PERMISSION_CHECK: dict[str, discord.Permissions] = {
    MODERATE: discord.Permissions(moderate_members=True),
    BAN: discord.Permissions(ban_members=True),
    KICK: discord.Permissions(kick_members=True),
    MESSAGES: discord.Permissions(manage_messages=True),
    CHANNELS: discord.Permissions(manage_channels=True),
    ROLES: discord.Permissions(manage_roles=True),
    GUILD: discord.Permissions(manage_guild=True),
    ADMIN: discord.Permissions(administrator=True),
}

#: The body check each non-permission audience must show in its callback source.
SOURCE_MARKER: dict[str, str] = {
    OWNER: "owner",
    DEV: "_is_operator",
    SUBMITTER: "creator_id",
    MARKET: "interaction_check",
}

# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #
#: Every command in the tree, and who it is for. Keys are the command's path:
#: ``"name"`` for a top-level command, ``"group sub"`` for a subcommand. A
#: command missing from this table is a failure in the test-suite, on purpose —
#: the audience is a decision, and an undeclared one is an accident.
COMMAND_ACCESS: dict[str, str] = {
    # -- Economy: everyone, on their own coins ----------------------------- #
    "balance": EVERYONE,
    "daily": EVERYONE,
    "weekly": EVERYONE,
    "work": EVERYONE,
    "beg": EVERYONE,
    "deposit": EVERYONE,
    "withdraw": EVERYONE,
    "pay": EVERYONE,
    "rob": EVERYONE,
    "richlist": EVERYONE,
    "shop": EVERYONE,
    "buy": EVERYONE,
    "inventory": EVERYONE,
    "loan": EVERYONE,
    "repay": EVERYONE,
    # `/claim` pays a member their own coins and gives back their own level, and
    # it reads and writes nothing else. It is not gated on the economy or the
    # leveling switch (see `features.ALWAYS_ALLOWED_COMMANDS`), and it can only
    # ever pay once per member, so there is no audience narrower than the member
    # it is for.
    "claim": EVERYONE,
    # -- Admin economy: the coins of others ------------------------------- #
    "eco-admin add": ADMIN,
    "eco-admin remove": ADMIN,
    "eco-admin set": OWNER,
    "eco-admin reset": OWNER,
    # The DaCat Fee switch is the server's, not a member's: one administrator
    # flips it for everyone.
    "eco-admin fee": ADMIN,
    "eco-admin bank": ADMIN,
    # -- Levels ------------------------------------------------------------ #
    "rank": EVERYONE,
    "leaderboard": EVERYONE,
    # Three verbs, one slot: viewing the rewards is for everyone, changing them
    # is the same audience a one-off role grant needs.
    "levelroles view": EVERYONE,
    "levelroles add": ROLES,
    "levelroles remove": ROLES,
    "levelsettings": GUILD,
    # `/add` binds which channel a system reports into — a decision about the
    # server's own furniture, so the audience that may change its settings.
    "add": GUILD,
    "setlevel": ADMIN,
    "givexp": ADMIN,
    "resetlevels": OWNER,
    # -- Games and social ------------------------------------------------- #
    "8ball": EVERYONE,
    "action": EVERYONE,
    "aura": EVERYONE,
    "blackjack": EVERYONE,
    "coinflip": EVERYONE,
    "crash": EVERYONE,
    "dice": EVERYONE,
    "divorce": EVERYONE,
    "bond propose": EVERYONE,
    "bond break": EVERYONE,
    "bond list": EVERYONE,
    # -- Music: sharing is everybody's ----------------------------------- #
    "music add": EVERYONE,
    "music my": EVERYONE,
    "music top": EVERYONE,
    "music jam": EVERYONE,
    "music jamset": EVERYONE,
    "music jamremove": EVERYONE,
    "flavor": GUILD,
    "flex": EVERYONE,
    "gamble": EVERYONE,
    "highlow": EVERYONE,
    "hunt": EVERYONE,
    "kms": EVERYONE,
    "marry": EVERYONE,
    "meme": EVERYONE,
    "meow": EVERYONE,
    "meowsound": EVERYONE,
    "mines": EVERYONE,
    "quote": EVERYONE,
    "rizz": EVERYONE,
    "roast": EVERYONE,
    "roulette": EVERYONE,
    "rps": EVERYONE,
    "scam": EVERYONE,
    "sellanimals": EVERYONE,
    "ship": EVERYONE,
    "slots": EVERYONE,
    "tan": EVERYONE,
    "tictactoe": EVERYONE,
    "trivia": EVERYONE,
    "uwu": EVERYONE,
    "zoo": EVERYONE,
    "customquote add": EVERYONE,
    "customquote get": EVERYONE,
    "customquote list": EVERYONE,
    "customquote delete": SUBMITTER,
    # -- Moderation -------------------------------------------------------- #
    "warn": MODERATE,
    "mute": MODERATE,
    "unmute": MODERATE,
    "warnings": MODERATE,
    "ban": BAN,
    "unban": BAN,
    "kick": KICK,
    "purge": MESSAGES,
    "clearwarnings": ADMIN,
    "modlog": ADMIN,
    # Deliberately EVERYONE, and the one command in this section that is: the
    # whole point is that the member an automatic action happened to can reach a
    # human without asking a moderator for permission to ask. A permission here
    # would be exactly the dead end `/appeal` exists to remove. What keeps it
    # from being a flood is the cooldown — one appeal per member per five
    # minutes — and the fact that it only ever *writes to the log*.
    "appeal": EVERYONE,
    "automod invites": GUILD,
    "automod links": GUILD,
    "automod antispam": GUILD,
    "automod native": GUILD,
    # The reading is for everybody, like `/modlog`'s bare form: an administrator
    # checking the filters is the common case, but "why did the bot delete my
    # link" is a question a member may ask, and it is a read of this server's own
    # settings — the same facts the dashboard prints.
    "automod status": EVERYONE,
    # -- Server control ---------------------------------------------------- #
    "lockdown": CHANNELS,
    "unlock": CHANNELS,
    "slowmode": CHANNELS,
    # The audit reads every channel's overwrites — who can see a private room,
    # where a lock can be typed through — and its `fix` may rewrite any channel the
    # ledger holds, not only the one it was typed in. `ADMIN` was the first answer
    # and is one rank too wide for it: an administrator is precisely who a security
    # reading of the server might be *about*, and the report belongs to the one
    # person Discord calls the owner. `OWNER` is a body check against
    # `guild.owner_id`; the picker still declares Administrator, because the owner
    # always holds it and everybody else has no business seeing the name.
    "check": OWNER,
    # `/hide` takes a room off every role's list and writes the ledger that puts it
    # back, so it answers to the same person `/check` does and for the same reason:
    # it rewrites the overwrites of a room the report is about.
    "hide": OWNER,
    # `/hidden` is the same audience as `/hide` for the same reason: it reports which
    # rooms are off every list and which ranks were stripped to keep them there, and
    # that describes the server's privacy rather than acting on a member.
    "hidden": OWNER,
    "addrole": ROLES,
    "removerole": ROLES,
    "defaultrole set": ADMIN,
    "defaultrole view": EVERYONE,
    "defaultrole clear": ADMIN,
    "announce": GUILD,
    "serverinfo": EVERYONE,
    "userinfo": EVERYONE,
    "ticket setup": CHANNELS,
    "ticket add": CHANNELS,
    "ticket remove": CHANNELS,
    # Reaction roles. Handing out a role is a role decision, so the two writes
    # are ROLES — the same audience `/addrole` has, and the same one the
    # dashboard's own add checks. The listing is a read of this server's own
    # mappings and is offered to everybody.
    "reactionrole add": ROLES,
    "reactionrole remove": ROLES,
    "reactionrole list": EVERYONE,
    # -- Meetings ---------------------------------------------------------- #
    # Booking, moving, cancelling and telling people now are the same audience
    # as `/announce`: Manage Server — the bit a moderator role usually holds,
    # and the one a staff meeting's organiser already has. Reading is
    # everybody's, for the same reason `/modlog`'s bare form is: a member
    # asking "when is the meeting?" is the whole point of posting the card
    # where they can see it, and a read of this server's own schedule changes
    # nothing.
    "meeting create": GUILD,
    "meeting reschedule": GUILD,
    "meeting ping": GUILD,
    "meeting cancel": GUILD,
    "meeting list": EVERYONE,
    "meeting view": EVERYONE,
    # -- Bot itself -------------------------------------------------------- #
    "help": EVERYONE,
    # The directory is everybody's, and it is the one command whose *content* is
    # per-member: `/cmd` lists what the asker may run, so declaring an audience
    # above EVERYONE here would hide the part of the tree a moderator most needs
    # to read from the moderator it is about. Nothing in this module can express
    # that, which is why the command carries no permission check at all and the
    # scoping lives in ``utils.command_directory``.
    "cmd": EVERYONE,
    "legal privacy": EVERYONE,
    "legal tos": EVERYONE,
    "bot sync": ADMIN,
    "bot db": ADMIN,
    "bot avatar": OWNER,
    "bot banner": OWNER,
    # Owner-only for a reason the other profile commands do not have: the activity
    # is **global**. Discord has no per-server presence, so a server administrator
    # who could set it would be setting it for every server — the last one to
    # speak winning for everyone. What *is* per server (name, picture, banner,
    # bio) lives in the dashboard's identity panel instead.
    "bot status": OWNER,
    "bot push": OWNER,
    # Reporting a fault in the bot is an operator's job, not a member's: the
    # command is the administrator's way in, while the /help panel's 🐞 button
    # stays open to everyone who finds something broken.
    "bot bug": ADMIN,
    # A server's own licence: the administrator of *that* server spends the
    # activation on it, which is why this is not the operator's OWNER tier. The
    # key is the proof of purchase, so there is nothing here to guess at.
    "bot plan": ADMIN,
    # `/dbstatus` moved here when the tree hit Discord's hundred-command ceiling.
    # The audience did not move with it — asking whether the database is healthy
    # is still an administrator's — and neither did anything the card says: it is
    # the same report, built by ``utils.dbstats`` rather than by the cog that
    # happened to host it.
    "bot dbstatus": ADMIN,
    # -- Activity Tracker: admin-only for management ---------------------- #
    "activity me": ADMIN,
    "activity top": ADMIN,
    "activity channel": ADMIN,
    # -- The market: one role, nothing else -------------------------------- #
    "market board": MARKET,
    "market quote": MARKET,
    "market chart": MARKET,
    "market buy": MARKET,
    "market sell": MARKET,
    "market portfolio": MARKET,
    "market history": MARKET,
    # -- Premium / VIP: everyone, on status, perks and invite ------------- #
    "premium status": EVERYONE,
    "premium perks": EVERYONE,
    "premium invite": EVERYONE,
}


def walk(tree) -> Iterator[tuple[str, object]]:
    """Yield ``(path, command)`` for every command in a command tree.

    A group is not itself a runnable command, so only its subcommands are
    yielded — the paths match the keys in :data:`COMMAND_ACCESS`.
    """
    for command in tree.get_commands():
        children = getattr(command, "commands", None)
        if children:
            for child in children:
                yield f"{command.name} {child.name}", child
        else:
            yield command.name, command


def undeclared(tree) -> list[str]:
    """Commands in ``tree`` with no declared audience — must always be empty."""
    return sorted(path for path, _ in walk(tree) if path not in COMMAND_ACCESS)


def unknown_declarations(tree) -> list[str]:
    """Declared audiences that no longer match a command (a retired command)."""
    present = {path for path, _ in walk(tree)}
    return sorted(path for path in COMMAND_ACCESS if path not in present)


def apply_declared_permissions(tree) -> list[str]:
    """Set each command's picker visibility from its declared audience.

    Discord hides a command from a member who lacks its
    ``default_member_permissions``, which is the only mechanism it offers for
    this. Called before the tree is published, so what members see and what the
    checks enforce agree. Returns the paths whose visibility changed, so a
    deploy log shows exactly what moved.
    """
    changed: list[str] = []
    for path, command in walk(tree):
        requirement = COMMAND_ACCESS.get(path)
        if requirement is None:
            continue
        wanted = PICKER_VISIBILITY[requirement]
        if command.default_permissions != wanted:
            command.default_permissions = wanted
            changed.append(path)
    return changed


# --------------------------------------------------------------------------- #
# Role grants
# --------------------------------------------------------------------------- #
#: Permissions that make a role too powerful to hand out (or automate) unless
#: the person doing it is an administrator. Discord's own role hierarchy stops a
#: member from editing a *role above their own*, but it happily lets a
#: moderator with Manage Roles grant a role that *contains* Administrator —
#: which is an escalation, not a delegation.
#: (What is deliberately *not* here: Manage Messages, Moderate Members, Manage
#: Nicknames and Manage Expressions. Those are ordinary moderation powers, and a
#: moderator handing a Staff role to another moderator is delegation, not an
#: escalation — refusing it would break the normal workflow this guard exists to
#: protect.)
DANGEROUS_ROLE_PERMISSIONS: tuple[str, ...] = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "ban_members",
    "kick_members",
    "mention_everyone",
)


def _describe(permission: str) -> str:
    return permission.replace("_", " ").title()


# --------------------------------------------------------------------------- #
# Admin gate + public denial
# --------------------------------------------------------------------------- #
# ``/eco-admin set`` once had no administrator gate at all — only an owner
# check the bot owner passes in *every* server — so a non-admin could rewrite
# any server's balances. These are the shared enforcement for every
# currency/levels command that must refuse non-admins: a pure predicate, and a
# denial posted visibly in the channel where the attempt happened (never
# ephemeral — a silent refusal reads as success to everyone watching). No
# mention in the text: the name must not ping anyone.


def has_admin(interaction) -> bool:
    perms = getattr(getattr(interaction, "user", None), "guild_permissions", None)
    return bool(perms is not None and perms.administrator)


async def deny_admin(interaction, what: str) -> None:
    name = getattr(getattr(interaction, "user", None), "display_name", "That member")
    content = (
        f"❌ **Permission Denied:** {name} lacks the **Administrator** "
        f"permission, which is required to {what}."
    )
    try:
        if interaction.response.is_done():
            channel = getattr(interaction, "channel", None)
            if channel is not None:
                await channel.send(content)
            else:
                await interaction.followup.send(content)
        else:
            await interaction.response.send_message(content, ephemeral=False)
    except (discord.Forbidden, discord.HTTPException):
        pass


def role_grant_problem(role, actor, guild) -> str | None:
    """Why ``actor`` may not give or take ``role``, or ``None`` if they may.

    Four refusals, and each one is a real escalation path:

    * ``@everyone`` is not a role that can be assigned.
    * an integration-managed role cannot be assigned by hand at all.
    * a role **at or above the actor's own highest role** — Discord would let a
      moderator do this for roles below it, and this is the line that stops a
      moderator promoting themselves or an alt.
    * a role carrying any permission from
      :data:`DANGEROUS_ROLE_PERMISSIONS`, unless the actor is an administrator.

    Pure on purpose: no discord client, no I/O, so the awkward cases are testable.
    """
    if role is None:
        return "That role no longer exists."
    default_role = getattr(guild, "default_role", None)
    if (default_role is not None and role.id == default_role.id) or role.is_default():
        return "**@everyone** cannot be handed out — it is not an assignable role."
    if role.managed:
        return (
            f"**{role.name}** is managed by an integration (a bot or a booster role), "
            "so it cannot be assigned by hand."
        )

    permissions = getattr(actor, "guild_permissions", None)
    if permissions is None:
        return "I could not read your permissions in this server."
    if permissions.administrator:
        return None

    top_role = getattr(actor, "top_role", None)
    if top_role is not None and role >= top_role:
        return (
            f"**{role.name}** sits at or above your highest role (**{top_role.name}**), "
            "so you cannot manage it."
        )

    for permission in DANGEROUS_ROLE_PERMISSIONS:
        if getattr(role.permissions, permission, False):
            return (
                f"**{role.name}** grants **{_describe(permission)}**, which only an "
                "administrator can hand out."
            )
    return None
