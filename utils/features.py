"""Single source of truth for every bot feature/module.

Before this module existed, one feature was described in six places:

    * ``bot.MODULE_COMMAND_MAP``        — slash command -> module gate
    * ``keep_alive.AVAILABLE_MODULES``  — dashboard card metadata
    * ``keep_alive.ALLOWED_MODULE_IDS`` — dashboard allow-list
    * ``utils.db.DEFAULT_DISABLED_MODULES`` — default on/off state
    * ``cogs.help.HELP_MODULES``        — help menu categories
    * every cog's ``is_module_enabled(..., "literal")`` calls

Removing a feature meant editing all six and *any* drift produced the exact
symptoms that were reported ("the card is green but the command says it's off",
"I turned it off and it still runs"). Everything mechanical now derives from
``FEATURES`` below, so a feature is added or retired in one place.

This module is intentionally dependency-free (stdlib only) so it can be
imported by the bot process, the Flask thread and the test-suite alike.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Feature:
    """A toggleable (or always-on) area of the bot."""

    id: str
    name: str
    icon: str
    category: str
    description: str
    # Root slash-command names this feature gates exclusively.
    gate_commands: tuple[str, ...] = ()
    # Human-facing command list shown on the dashboard card.
    commands: tuple[str, ...] = ()
    # Cog extension loaded for this feature (None for non-cog features).
    cog: str | None = None
    # False keeps the feature OFF until an admin explicitly enables it.
    enabled_by_default: bool = True
    # False means the feature has no dashboard switch (core utilities).
    toggleable: bool = True
    # Help-menu category key, if it has one.
    help_key: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Feature.id is required")
        if self.toggleable and self.help_key is None:
            # Default the help key to the id; validated by validate().
            object.__setattr__(self, "help_key", self.id)


FEATURES: tuple[Feature, ...] = (
    Feature(
        id="leveling",
        name="Leveling & XP",
        icon="🏆",
        category="Progression",
        description=(
            "Message length & voice XP rewards, level-up cards routed to #bot-cmd, "
            "rank titles & /rank."
        ),
        gate_commands=(
            "rank", "leaderboard", "levelsettings", "setlevel", "givexp",
            "resetlevels", "addlevelrole", "removelevelrole", "levelroles",
        ),
        commands=(
            "/rank", "/leaderboard", "/levelsettings", "/setlevel", "/givexp",
            "/addlevelrole", "/removelevelrole", "/levelroles", "/resetlevels",
        ),
        cog="cogs.leveling",
        help_key="leveling",
    ),
    Feature(
        id="economy",
        name="Economy & Casino",
        icon="💰",
        category="Economy",
        description=(
            "Daily streak rewards, gambling, slots, blackjack, coinflip, shop, "
            "and inventory items."
        ),
        gate_commands=(
            "balance", "daily", "weekly", "work", "rob", "pay", "deposit",
            "withdraw", "shop", "buy", "inventory", "richlist", "eco-admin",
        ),
        commands=(
            "/balance", "/daily", "/weekly", "/work", "/rob", "/pay", "/deposit",
            "/withdraw", "/shop", "/buy", "/inventory", "/richlist", "/eco-admin",
        ),
        cog="cogs.economy",
        help_key="economy",
    ),
    Feature(
        id="moderation",
        name="Moderation & Logs",
        icon="🛡️",
        category="Management",
        description=(
            "Administrative bans, kicks, timeouts, warnings, message purges, and "
            "automated audit logs in #logs."
        ),
        gate_commands=(
            "ban", "kick", "mute", "unmute", "unban", "warn", "warnings",
            "clearwarnings", "purge", "modlog", "lockdown", "lockdown-server",
            "unlock", "unlock-server", "slowmode", "addrole", "removerole",
            "announce",
        ),
        commands=(
            "/ban", "/kick", "/mute", "/unmute", "/warn", "/warnings",
            "/clearwarnings", "/purge", "/modlog", "/lockdown", "/unlock",
            "/slowmode", "/addrole", "/removerole", "/announce",
        ),
        cog="cogs.moderation",
        help_key="moderation",
    ),
    Feature(
        id="automod",
        name="Automod & Shield",
        icon="🤖",
        category="Security",
        description=(
            "Real-time anti-spam flood protection, Discord invite filter, "
            "unauthorized link blocking."
        ),
        gate_commands=("automod",),
        commands=("/automod invites", "/automod antispam"),
        cog="cogs.automod",
        help_key="automod",
    ),
    Feature(
        id="starboard",
        name="Starboard",
        icon="⭐",
        category="Community",
        description=(
            "Automated star reaction tracking, community showcase cards, "
            "attachment previews."
        ),
        gate_commands=("starboard",),
        commands=("/starboard set", "/starboard limit", "/starboard status"),
        cog="cogs.starboard",
        help_key="starboard",
    ),
    Feature(
        id="tickets",
        name="Support Tickets",
        icon="🎟️",
        category="Support",
        description=(
            "Interactive dropdown ticket panels (General, Reports, Technical, "
            "Trading), private staff channels."
        ),
        gate_commands=("ticket",),
        commands=("/ticket setup",),
        cog="cogs.tickets",
        help_key="tickets",
    ),
    Feature(
        id="social",
        name="Social Roleplay",
        icon="🐾",
        category="Community",
        description=(
            "Expressive action roleplay commands (hug, pat, cuddle, kiss, slap), "
            "/uwu text styler."
        ),
        gate_commands=(
            "action", "uwu", "marry", "divorce", "ship", "hunt", "zoo",
        ),
        commands=(
            "/action", "/uwu", "/marry", "/divorce", "/ship", "/hunt", "/zoo",
            "/sellanimals",
        ),
        cog="cogs.social",
        help_key="social",
    ),
    Feature(
        id="fun",
        name="Fun & Memes",
        icon="🎭",
        category="Entertainment",
        description=(
            "Real-time viral memes scraped from Reddit top feeds, 8ball, "
            "inspirational quotes."
        ),
        gate_commands=(
            "meme", "8ball", "meow", "meowsound", "tan", "kms", "quote",
            "customquote",
        ),
        commands=(
            "/meme", "/8ball", "/meow", "/meowsound", "/tan", "/kms", "/quote",
            "/customquote",
        ),
        cog="cogs.fun",
        help_key="fun",
    ),
    Feature(
        id="games",
        name="Mini-Games Hub",
        icon="🎮",
        category="Games",
        description=(
            "Interactive Discord game sessions: TicTacToe, Rock-Paper-Scissors, "
            "Trivia, Mines, Crash, HighLow."
        ),
        gate_commands=("tictactoe", "rps", "trivia"),
        commands=(
            "/gamble", "/slots", "/blackjack", "/roulette", "/coinflip", "/dice",
            "/mines", "/crash", "/highlow", "/tictactoe", "/rps", "/trivia",
        ),
        cog="cogs.games",
        help_key="games",
    ),
    # Not toggleable: core utilities always stay available. It has no dashboard
    # switch and no default state, but it still owns a help category.
    Feature(
        id="system",
        name="System & Server Control",
        icon="⚙️",
        category="Core",
        description=(
            "Server utility, announcements, lockdown, and configuration."
        ),
        commands=(
            "/serverinfo", "/userinfo", "/announce", "/sync", "/updatedb",
            "/privacy", "/tos",
        ),
        toggleable=False,
        help_key="system",
    ),
)

# Commands that require *several* features at once to be enabled. These are the
# casino pair (economy + games) and selling animals (social + economy).
SHARED_GATES: Mapping[str, tuple[str, ...]] = {
    "gamble": ("economy", "games"),
    "slots": ("economy", "games"),
    "blackjack": ("economy", "games"),
    "roulette": ("economy", "games"),
    "coinflip": ("economy", "games"),
    "dice": ("economy", "games"),
    "mines": ("economy", "games"),
    "crash": ("economy", "games"),
    "highlow": ("economy", "games"),
    "sellanimals": ("social", "economy"),
}

# Cogs that are not a feature of their own (core utilities / docs / config).
CORE_COGS: tuple[str, ...] = (
    "cogs.server_control",
    "cogs.help",
    "cogs.legal",
    "cogs.bot_config",
)

# Commands that are always reachable regardless of module state. (The old
# hand-written list in bot.py still named a "/ping" command that no longer
# exists; tests/test_commands.py now fails if this list and the real command
# tree ever drift apart again.)
ALWAYS_ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "help", "dashboard", "sync", "serverinfo", "userinfo", "privacy",
    "tos", "setavatar", "setbanner", "updatedb",
})

# Commands that are safe to run in a DM (or that handle their own guild check).
# Everything else needs a guild: it reads `interaction.guild.id` for the wallet,
# the level row or the module toggle, and without this guard a DM invocation
# raised AttributeError and answered with "An unexpected error occurred".
DM_ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "help", "privacy", "tos", "uwu", "8ball", "dashboard",
})


def command_gates() -> dict[str, tuple[str, ...]]:
    """Root command name -> feature ids that must all be enabled.

    Exclusively-gated commands come from each feature; multi-feature commands
    come from ``SHARED_GATES``. Raises if two features claim the same command,
    which would silently make the gate depend on dict ordering.
    """
    gates: dict[str, tuple[str, ...]] = {}
    for feature in FEATURES:
        for cmd in feature.gate_commands:
            if cmd in gates:
                raise ValueError(
                    f"Command '{cmd}' is gated by both "
                    f"{gates[cmd]} and '{feature.id}'"
                )
            gates[cmd] = (feature.id,)
    for cmd, ids in SHARED_GATES.items():
        for fid in ids:
            if fid not in FEATURE_IDS:
                raise ValueError(f"SHARED_GATES['{cmd}'] references unknown feature '{fid}'")
        if cmd in gates:
            raise ValueError(f"Command '{cmd}' defined in both gate_commands and SHARED_GATES")
        gates[cmd] = tuple(ids)
    return gates


def cog_paths() -> list[str]:
    """Every cog extension the bot should load, features first."""
    paths = [f.cog for f in FEATURES if f.cog]
    paths.extend(CORE_COGS)
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def default_disabled() -> frozenset[str]:
    """Feature ids that start OFF until an admin turns them on."""
    return frozenset(f.id for f in FEATURES if f.toggleable and not f.enabled_by_default)


DEFAULT_DISABLED_MODULES: frozenset[str] = default_disabled()

FEATURE_IDS: frozenset[str] = frozenset(f.id for f in FEATURES)
TOGGLEABLE_IDS: frozenset[str] = frozenset(f.id for f in FEATURES if f.toggleable)
HELP_KEYS: frozenset[str] = frozenset(f.help_key for f in FEATURES if f.help_key)
MODULE_COMMAND_MAP: dict[str, tuple[str, ...]] = command_gates()


def get(feature_id: str) -> Feature | None:
    for feature in FEATURES:
        if feature.id == feature_id:
            return feature
    return None


def is_toggleable(module_id: str) -> bool:
    feature = get(module_id)
    return bool(feature and feature.toggleable)


def default_enabled(module_id: str) -> bool:
    """Default on/off state for a module with no saved value."""
    feature = get(module_id)
    if feature is None:
        # Unknown ids stay off — failing closed is safer than starting something.
        return False
    return feature.enabled_by_default


def dashboard_modules() -> list[dict]:
    """Card metadata consumed by ``templates/dashboard.html``."""
    return [
        {
            "id": f.id,
            "name": f.name,
            "icon": f.icon,
            "category": f.category,
            "desc": f.description,
            "commands": list(f.commands),
        }
        for f in FEATURES
        if f.toggleable
    ]


def validate() -> list[str]:
    """Return a list of registry problems (empty means healthy).

    Called at startup and asserted by the test-suite so a malformed manifest
    fails loudly at boot instead of silently at runtime.
    """
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_help: set[str] = set()
    for f in FEATURES:
        if f.id in seen_ids:
            problems.append(f"duplicate feature id: {f.id}")
        seen_ids.add(f.id)
        if f.help_key:
            if f.help_key in seen_help:
                problems.append(f"duplicate help key: {f.help_key}")
            seen_help.add(f.help_key)
        if f.toggleable and not f.commands and not f.gate_commands:
            problems.append(f"toggleable feature '{f.id}' gates no commands")
    try:
        command_gates()
    except ValueError as exc:
        problems.append(str(exc))
    for cmd, ids in MODULE_COMMAND_MAP.items():
        for fid in ids:
            if fid not in seen_ids:
                problems.append(f"command '{cmd}' gated by unknown feature '{fid}'")
    return problems
