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
            "resetlevels", "levelroles",
        ),
        commands=(
            "/rank", "/leaderboard", "/levelroles view", "/levelroles add",
            "/levelroles remove", "/levelsettings", "/setlevel", "/givexp",
            "/resetlevels",
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
            "inventory items, and the Meow Fee on transfers into Meow Bank."
        ),
        gate_commands=(
            "balance", "daily", "weekly", "work", "rob", "pay", "deposit",
            "withdraw", "shop", "buy", "inventory", "richlist", "eco-admin",
            "addmoney", "removemoney", "setmoney", "addbank", "loan", "repay",
            "beg",
        ),
        commands=(
            "/balance", "/daily", "/weekly", "/work", "/rob", "/pay", "/deposit",
            "/withdraw", "/shop", "/buy", "/inventory", "/richlist", "/eco-admin",
            "/loan", "/repay", "/beg", "/eco-admin fee",
        ),
        cog="cogs.economy",
        help_key="economy",
    ),
    Feature(
        id="market",
        name="Market",
        icon="📈",
        category="Economy",
        description=(
            "A live market priced by real exchanges: buy and sell crypto and "
            "stocks with coins, and track your portfolio. Fills match the real "
            "price 1:1, and trading is limited to a configured role."
        ),
        gate_commands=("market",),
        commands=(
            "/market board — Live prices for everything tradable",
            "/market chart — Real candles with your average cost drawn on them",
            "/market buy — Buy an instrument with coins",
            "/market sell — Sell a position (quantity, %, or all)",
            "/market portfolio — Your positions and their profit/loss",
            "/market quote — One instrument in detail",
            "/market history — Your recent fills",
        ),
        cog="cogs.market",
        # Temporarily shipped OFF (owner's request, 2026-09-25). A one-time
        # migration in utils/db.py flips saved rows too; the registry default
        # covers servers that arrive afterwards. Delete these two lines to
        # re-enable everywhere.
        enabled_by_default=False,
        help_key="market",
    ),
    Feature(
        id="moderation",
        name="Moderation & Logs",
        icon="🛡️",
        category="Management",
        description=(
            "Administrative bans, kicks, timeouts, warnings, message purges, and "
            "automated audit logs in #logs — plus the automatic shields: real-time "
            "anti-spam flood protection, Discord invite filter and unauthorized "
            "link blocking. One switch runs all of it."
        ),
        gate_commands=(
            "ban", "kick", "mute", "unmute", "unban", "warn", "warnings",
            "clearwarnings", "purge", "modlog", "appeal", "lockdown",
            "lockdown-server",
            "unlock", "unlock-server", "slowmode", "addrole", "removerole",
            "announce",
            "automod",
        ),
        commands=(
            "/ban", "/kick", "/mute", "/unmute", "/warn", "/warnings",
            "/clearwarnings", "/purge", "/modlog", "/appeal", "/lockdown",
            "/unlock",
            "/slowmode", "/addrole", "/removerole", "/announce",
            "/automod invites", "/automod antispam",
        ),
        cog="cogs.moderation",
        help_key="moderation",
    ),
    # Automod & Shield was merged into Moderation & Logs above: one switch runs
    # the manual actions and the automatic filters. `cogs.automod` still loads —
    # via CORE_COGS below, for the same single-path reason radio and qna are
    # there — and gates itself on "moderation".
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
        enabled_by_default=False,  # temporarily off — see the market entry
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
            "customquote", "rizz", "roast", "scam", "aura", "flavor",
        ),
        commands=(
            "/meme", "/8ball", "/meow", "/meowsound", "/tan", "/kms", "/quote",
            "/customquote", "/rizz", "/roast", "/scam", "/aura", "/flex",
            "/flavor — cat, dog or neutral, per server",
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
    Feature(
        id="company",
        name="Voice Company",
        icon="🫂",
        category="Community",
        description=(
            "When somebody is alone in a voice channel the bot joins, deafened and "
            "muted, and says hello — and it shows on the owner's dashboard who is "
            "alone and who is live."
        ),
        # The command *is* the setting, so it is the whole gate: /company is how a
        # server decides whether the bot does this at all.
        gate_commands=("company",),
        commands=("/company — auto or off, and whether I say hello",),
        cog="cogs.company",
        enabled_by_default=False,  # temporarily off — see the market entry
        help_key="company",
    ),
    Feature(
        id="structure",
        name="Server Structure",
        icon="🧱",
        category="Management",
        description=(
            "Channels and roles, built and cleared away from Discord: make a "
            "channel or a role, delete one, or clear a whole category out. Every "
            "action over this server's ceiling stops and asks whoever typed the "
            "command to confirm it, and every one of them — refused ones included "
            "— opens a case in the server's own moderation log."
        ),
        gate_commands=("structure",),
        commands=(
            "/structure channel-create — a text channel, optionally private",
            "/structure channel-delete — delete one channel and its messages",
            "/structure channel-archive — clear a category out, confirmed",
            "/structure role-create — make a role, with a colour",
            "/structure role-delete — delete a role",
            "/structure role-purge — take a role off everybody who has it, confirmed",
            "/structure limits — what this server asks you to confirm",
        ),
        cog="cogs.structure",
        help_key="structure",
    ),
    # Radio and the anonymous question box: **off by default**, one flag per
    # server.
    #
    # "Off by default, and one flag per server" is not a new mechanism: it is
    # what this registry already does. `enabled_by_default=False` is the default
    # state, `command_scope.is_available` already removes a disabled command from
    # *that server's* picker rather than merely refusing it, and
    # `modules.set_enabled` already writes the per-server flag. So offering them
    # is a registry edit — and because the commands keep their own names, nothing
    # goes into `commands_sync.RETIRED_COMMANDS` and no server's muscle memory is
    # broken. A `/legacy` group would have been the louder change and the bigger
    # lie: the commands would still work, nobody would know where they went.
    #
    # **The name is a decision, not a leftover.** It read *Legacy — Radio &
    # Anonymous Q&A* under a `Legacy` category, and `/systems` prints
    # "{name} arrives switched off" — so the page told a visitor Radio was legacy
    # and switched off, which reads as a service being retired. Nothing is: off is
    # the default the switch beside it declares. The **id** stays `legacy` — a
    # command choice value, a per-server row and a `/systems#cmd-legacy` anchor —
    # so renaming it is a migration with no reader-visible reward.
    Feature(
        id="legacy",
        name="Radio & Anonymous Q&A",
        icon="📻",
        category="Community",
        description=(
            "Off in every server to begin with: switched off until an "
            "administrator asks for them, and one flag offers both. Radio plays "
            "any station the table holds, and the question box relays a member's "
            "question with nobody's name on it."
        ),
        # Two roots, one switch: the brief is explicit that this is one flag a
        # server turns on, not two more things to configure.
        gate_commands=("qna", "radio"),
        commands=(
            "/radio play — a station by name, genre, language or country",
            "/radio stop — end it and give the channel back",
            "/radio doctor — what this host can and cannot do for voice",
            "/qna ask — Ask something with nobody's name on it, in any server",
            "/qna inbox — The questions waiting for an answer (bot's own server)",
            "/qna reply — Answer one, by DM (bot's own server)",
        ),
        # No cog of its own: `Feature.cog` holds one path and this feature is two
        # files, so both are loaded as core cogs and gate themselves. See
        # `CORE_COGS` below.
        cog=None,
        enabled_by_default=False,
        help_key="legacy",
    ),
    Feature(
        id="tracker",
        name="Activity Tracker",
        icon="📊",
        category="Community",
        description=(
            "Per-day, per-channel message and voice time tracking with a "
            "365-day rolling window. View your activity, leaderboards, and "
            "channel breakdowns."
        ),
        # The gate names the *root* command, because that is what Discord reports
        # for a subcommand; the paths below are what a member sees.
        gate_commands=("activity",),
        commands=(
            "/activity me", "/activity top", "/activity channel",
        ),
        cog="cogs.tracker",
        help_key="tracker",
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
            "/serverinfo", "/userinfo", "/announce", "/bot sync", "/bot db",
            "/legal privacy", "/legal tos", "/dbstatus",
            "/defaultrole set", "/defaultrole view", "/defaultrole clear",
        ),
        toggleable=False,
        help_key="system",
    ),
)

# Commands that require *several* features at once to be enabled. These are the
# casino pair (economy + games) and selling animals (social + economy).
SHARED_GATES: Mapping[str, tuple[str, ...]] = {
    "gamble": ("economy", "games"),
    # /flex reads wallet, bank and loan debt, so it needs both features on.
    "flex": ("fun", "economy"),
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
#
# `cogs.radio` and `cogs.qna` are here rather than on the legacy feature for one
# mechanical reason — `Feature.cog` holds a single path and the legacy module is
# two files — and they are safe here because "always loaded" and "always runnable"
# stopped being the same thing when the registry gained a gate: every command in
# both cogs checks `is_module_enabled(guild, "legacy")` before it does anything,
# and `command_scope` keeps them out of a picker that has not opted in. Loading a
# cog registers its commands; it does not offer them.
#
# `cogs.automod` is here for the same mechanical reason: Automod & Shield was
# merged into Moderation & Logs, `Feature.cog` holds one path, so the filters'
# cog loads as a core cog and gates itself on `"moderation"` — one switch runs
# the manual actions and the automatic ones.
CORE_COGS: tuple[str, ...] = (
    "cogs.server_control",
    "cogs.help",
    "cogs.legal",
    "cogs.bot_config",
    "cogs.radio",
    "cogs.qna",
    "cogs.automod",
)

# Commands that are always reachable regardless of module state. (The old
# hand-written list in bot.py still named a "/ping" command that no longer
# exists; tests/test_commands.py now fails if this list and the real command
# tree ever drift apart again.)
ALWAYS_ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "help", "bot", "serverinfo", "userinfo", "legal",
    "dbstatus",
    "defaultrole",
})

# Commands that are safe to run in a DM (or that handle their own guild check).
# Everything else needs a guild: it reads `interaction.guild.id` for the wallet,
# the level row or the module toggle, and without this guard a DM invocation
# raised AttributeError and answered with "An unexpected error occurred".
DM_ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "help", "legal", "uwu", "8ball",
    # The owner answers questions from a DM — the card's button is answered in
    # one, and ``/qna reply`` has to be reachable from the same place or the
    # feature is discoverable only by whoever remembers it. ``/qna ask`` checks
    # for a server itself and says so, since a question is posted *somewhere*.
    "qna",
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


# ---------------------------------------------------------------------------
# How many of a thing there are
#
# Two numbers that describe this bot to somebody who has not used it: how many
# systems it has, and how many commands. They are facts about *the code*, they
# appear on the landing page, in the console's cards, in the hub's rail and in
# the reference's own headline — and every one of those places used to work them
# out separately, which is how the same bot came to advertise **11 systems and
# 100 commands** on its front page and **12 and 90** one click away.
#
# Not one of those differences was a bug in a count. `systems` was the dashboard
# module list on one surface and the registry on the next; `commands` was every
# declared string including `/market buy` on one and the distinct names Discord
# actually publishes on the other. Each was defensible alone and together they
# were a page arguing with itself, which is the failure this whole module exists
# to prevent. So the definitions live here, once, and a caller that wants a
# number reads it rather than deciding what it means.
# ---------------------------------------------------------------------------


def system_count() -> int:
    """How many systems this bot declares.

    The *declared* set, not the switchable one: this is the answer to "what does
    it do", and a server that cannot switch something off still gets it. The
    switchable subset is ``len(TOGGLEABLE_IDS)`` and the hub's systems page draws
    both, labelled — which is the only honest way to show two numbers about one
    word.
    """
    return len(FEATURES)


#: Every name Discord's picker would offer, reduced to its root: `/market buy`
#: is one command called `market`. Derived from the registry rather than typed,
#: so retiring a feature retires its commands from the count.
ROOT_COMMANDS: frozenset[str] = frozenset(
    {command.lstrip("/").split()[0] for feature in FEATURES for command in feature.commands}
    | set(MODULE_COMMAND_MAP)
    | set(ALWAYS_ALLOWED_COMMANDS)
)


def command_count() -> int:
    """How many commands this bot publishes, counted the way a picker counts.

    The live command tree is deliberately *not* consulted, and the reason is the
    same one that made the old front page wrong: a tree measured inside a running
    process is a fact about that process — which servers it has synced, what a
    server switched off — while a marketing figure is a claim about the product.
    Reading the tree here would also make the published site's number depend on
    whether a gateway happened to be awake, which is exactly how the front page
    came to print two different totals from one visit to the next.
    """
    return len(ROOT_COMMANDS)


def feature_entries(feature: "Feature") -> tuple[tuple[str, str], ...]:
    """One system's commands, split into the two halves a reader is shown.

    A declared command is one string — ``"/market buy — Buy an instrument with
    coins"`` — and every surface that shows it wants the same two pieces: the
    name a person types and, where the registry wrote one, the sentence after
    the em dash. Splitting it in each caller is how two of them come to disagree
    about how many rows there are, so the split lives here and callers read it.
    """
    entries = []
    for raw in feature.commands:
        text = str(raw).strip()
        if not text:
            continue
        name, _, note = text.partition(" — ")
        name = name.strip()
        if name:
            entries.append((name, note.strip()))
    return tuple(entries)


#: Every command as a person types it, for the whole registry.
COMMAND_ENTRIES: tuple[tuple[str, str], ...] = tuple(
    entry for feature in FEATURES for entry in feature_entries(feature)
)


def command_entries() -> tuple[tuple[str, str], ...]:
    """Every command **as a person types it**, with the note the registry wrote.

    ``command_count()`` counts the way Discord's picker counts — `/market buy`
    and `/market sell` are one command called ``market`` — while this counts the
    rows somebody can actually type. Both are true and the difference is real
    (``/market`` alone is seven of them), so a page that prints one must not
    print it over a list of the other: that is how the front door came to say
    *95 commands* above a hundred and ten of them, which reads as a page
    arguing with itself.
    """
    return COMMAND_ENTRIES


def entry_count() -> int:
    """How many rows :func:`command_entries` holds — what a visitor can type."""
    return len(COMMAND_ENTRIES)


def always_on_names() -> tuple[str, ...]:
    """The systems a server cannot switch off, by name.

    These are the *difference* between the two numbers every surface now prints —
    ``system_count()`` against ``len(dashboard_modules())`` — and printing two
    numbers about one word without ever naming the gap is the failure this module
    was written to stop. A reader who counts the switchable list under a total
    heading should not be left to work out which one is missing.

    Returned as names rather than as cards because that is the whole claim: a
    page says *these are always on*, and it says it in the registry's words.
    """
    return tuple(f.name for f in FEATURES if not f.toggleable)


def get(feature_id: str) -> Feature | None:
    for feature in FEATURES:
        if feature.id == feature_id:
            return feature
    return None


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


def ships_off() -> list[dict]:
    """The systems a server has to ask for, in the registry's own sentence.

    The third answer to *which of these is not like the others*: most systems
    arrive on and can be switched off, :func:`always_on_names` names the ones
    that cannot be switched off at all, and these arrive **off**. A visitor who
    installs the bot and is refused by `/radio` has been told nothing by a page
    that described the system only as *switchable like the rest*, so the page
    that lists every system prints this — on the page the reader is counting
    systems on, which is where a total over a shorter list gets explained.

    The sentence is the feature's own ``description`` rather than a summary of
    it, for the reason every other reader here exists: a registry edit moves the
    page with it and the two cannot drift.
    """
    return [module for module in dashboard_modules() if module["id"] in DEFAULT_DISABLED_MODULES]


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
