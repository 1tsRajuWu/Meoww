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
class Setting:
    """One per-server setting a system declares, so its card can carry a control.

    This is the same move ``FEATURES`` made for systems, one level down: a
    setting used to be a bare string key read in one place and written in
    another, with the dashboard unable to offer it because nothing knew its
    label, its type or its bounds. A declared setting carries all four, so the
    page, the endpoint and the reader cannot disagree about what an
    administrator is being asked for — and a new control is a registry edit
    rather than a new form, a new branch and a new validator.

    ``key`` overrides the derived ``f"{feature_id}_{id}"`` name for settings
    that were stored under another namespace before the registry existed (the
    structure ceilings live under ``confirmation_ceiling.<action>``).
    """

    id: str
    label: str
    help: str
    #: "bool", "int", "choice" or "text" — the four controls a card can draw.
    kind: str
    default: object = ""
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[tuple[str, str], ...] = ()
    #: Explicit ``guild_settings`` key, when the derived name is not the one.
    key: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Setting.id is required")
        if self.kind not in SETTING_KINDS:
            raise ValueError(f"Setting '{self.id}' has unknown kind '{self.kind}'")
        if self.kind == "choice" and not self.choices:
            raise ValueError(f"Setting '{self.id}' is a choice with nothing to choose")
        if self.kind == "int" and self.default != "" and not isinstance(self.default, int):
            raise ValueError(f"Setting '{self.id}' is an int with a non-int default")


def setting_key(feature_id: str, setting_id: str) -> str:
    """Where one declared setting lives in ``guild_settings``.

    The derived name is ``<feature>_<setting>`` — the shape the automod filters
    were already stored under — and a declaration that needs a different one
    says so itself (see :attr:`Setting.key`).
    """
    feature = get(feature_id)
    for spec in feature.settings if feature else ():
        if spec.id == setting_id:
            return spec.key or f"{feature_id}_{setting_id}"
    return f"{feature_id}_{setting_id}"


def settings_for(feature_id: str) -> tuple[Setting, ...]:
    """Every setting a system declares, in the order its card draws them."""
    feature = get(feature_id)
    return feature.settings if feature else ()


def get_setting(feature_id: str, setting_id: str) -> Setting | None:
    for spec in settings_for(feature_id):
        if spec.id == setting_id:
            return spec
    return None


def setting_value(spec: Setting, raw):
    """The value in force for a stored raw one, or the declared default.

    Anything unreadable, unparsable or out of range falls back to the default
    rather    than being clamped *upwards*: the only direction of corruption that
    matters is a stored number silently becoming a larger allowance than the
    default.

    """
    if raw is None or str(raw).strip() == "":
        return spec.default
    if spec.kind == "bool":
        text = str(raw).strip().lower()
        if text in BOOL_TRUE:
            return True
        if text in BOOL_FALSE:
            return False
        return spec.default
    if spec.kind == "int":
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return spec.default
        low = spec.minimum if spec.minimum is not None else value
        high = spec.maximum if spec.maximum is not None else value
        if value < low or value > high:
            return spec.default
        return value
    if spec.kind == "choice":
        text = str(raw).strip()
        for value, _label in spec.choices:
            if text == value:
                return value
        return spec.default
    return str(raw)


def read_setting_values(feature_id: str, read_raw) -> dict:
    """Every declared setting's value in force, from a raw reader callable.

    ``read_raw`` takes the storage key and answers the stored value or ``None``;
    the registry decides what that means. Kept here rather than in each caller
    because the fallback rules (:func:`setting_value`) are the part a second
    implementation would get wrong — and a page that drew a different default
    from the one the bot reads is the exact drift this module exists to stop.
    """
    values = {}
    for spec in settings_for(feature_id):
        try:
            raw = read_raw(setting_key(feature_id, spec.id))
        except Exception:  # noqa: BLE001 — a failed read is the default, said plainly
            raw = None
        values[spec.id] = setting_value(spec, raw)
    return values


#: The controls a settings card knows how to draw, and the spellings a form or
#: a script may use for a two-state one.
SETTING_KINDS: frozenset[str] = frozenset({"bool", "int", "choice", "text"})
BOOL_TRUE: frozenset[str] = frozenset({"1", "true", "on", "yes", "enabled"})
BOOL_FALSE: frozenset[str] = frozenset({"0", "false", "off", "no", "disabled"})


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
    # Per-server settings this system declares, drawn on its dashboard card.
    settings: tuple[Setting, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Feature.id is required")
        if self.toggleable and self.help_key is None:
            # Default the help key to the id; validated by validate().
            object.__setattr__(self, "help_key", self.id)
        seen: set[str] = set()
        for spec in self.settings:
            if spec.id in seen:
                raise ValueError(f"Feature '{self.id}' declares setting '{spec.id}' twice")
            seen.add(spec.id)


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
            "inventory items, and the DaCat Fee on transfers into DaCat Bank."
        ),
        gate_commands=(
            "balance", "daily", "weekly", "work", "rob", "pay", "deposit",
            "withdraw", "shop", "buy", "inventory", "richlist", "eco-admin",
            "addmoney", "removemoney", "setmoney", "addbank", "loan", "repay",
            "beg",
        ),
        # The admin group is spelled out by its real subcommands, not as
        # "/eco-admin": the group name is not a command a member can type, and
        # every count on every surface reads this list (`test_features` holds it
        # to the command tree), so a row here that nobody can type is a row the
        # front page advertises anyway.
        commands=(
            "/balance", "/daily", "/weekly", "/work", "/rob", "/pay", "/deposit",
            "/withdraw", "/shop", "/buy", "/inventory", "/richlist",
            "/eco-admin add", "/eco-admin remove", "/eco-admin set",
            "/eco-admin reset", "/eco-admin fee",
            "/loan", "/repay", "/beg",
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
            "automated audit logs in #logs. The automatic shields — invites, "
            "links and floods — are their own system: see *Auto Moderation*."
        ),
        gate_commands=(
            "ban", "kick", "mute", "unmute", "unban", "warn", "warnings",
            "clearwarnings", "purge", "modlog", "appeal", "lockdown",
            "lockdown-server",
            "unlock", "unlock-server", "slowmode", "addrole", "removerole",
            "announce",
        ),
        commands=(
            "/ban", "/kick", "/mute", "/unmute", "/warn", "/warnings",
            "/clearwarnings", "/purge", "/modlog", "/appeal", "/lockdown",
            "/unlock",
            "/slowmode", "/addrole", "/removerole", "/announce",
        ),
        cog="cogs.moderation",
        help_key="moderation",
    ),
    # Automod — its own system, and its own switch.
    #
    # It was merged into Moderation & Logs once ("one switch runs the manual
    # actions and the automatic filters"), and the merge is what made the
    # complaint this entry answers possible: the only way to stop the automatic
    # filters was to switch off bans, kicks, warnings and logging with them.
    # A server that wants human moderators but no robot deleting its members'
    # messages had no switch, and the dashboard had nothing to draw.
    #
    # `cogs.automod` still loads through CORE_COGS because one `Feature.cog` path
    # cannot hold a feature that is one file plus a registry row — and it gates
    # itself on "automod" rather than on moderation. The settings below are the whole of its card: the two
    # filters that already existed, the flood rule the cog used to hardcode, and
    # the link filter the old copy advertised before any code implemented it.
    Feature(
        id="automod",
        name="Auto Moderation",
        icon="🤖",
        category="Management",
        description=(
            "The automatic shields, on their own switch: invite links, ordinary "
            "links (with an allowlist) and message floods are handled the moment "
            "they happen, each one leaving a case in the moderation log and a "
            "notice naming `/appeal`. Discord's native AutoMod can block invites "
            "and floods before they reach the bot, while the local log keeps the "
            "server's detailed audit trail. Every threshold and length below is "
            "this server's to set."
        ),
        gate_commands=("automod",),
        commands=(
            "/automod invites", "/automod links", "/automod antispam",
            "/automod status",
        ),
        # No cog of its own: `cogs.automod` is a core cog that gates itself, so
        # that a server with this system off still loads the listener (a disabled
        # listener is how "turned off and still deleting" happens) and the gate
        # does the deciding. See CORE_COGS.
        cog=None,
        help_key="automod",
        settings=(
            Setting(
                id="block_invites",
                label="Block Discord invite links",
                help=(
                    "Deletes a message carrying a discord.gg invite, records the case "
                    "and points the member at `/appeal`."
                ),
                kind="bool",
                default=False,
            ),
            Setting(
                id="block_links",
                label="Block links",
                help=(
                    "Deletes a message carrying any http(s) link that is not on the "
                    "allowlist below."
                ),
                kind="bool",
                default=False,
            ),
            Setting(
                id="link_allowlist",
                label="Allowed domains",
                help=(
                    "Domains that stay allowed while link blocking is on, separated "
                    "by commas. Empty means every link is blocked."
                ),
                kind="text",
                default="",
            ),
            Setting(
                id="anti_spam",
                label="Flood protection",
                help="Watches for members sending many messages in a short window.",
                kind="bool",
                default=True,
            ),
            Setting(
                id="spam_messages",
                label="Messages before it acts",
                help="How many messages inside the window trigger the flood rule.",
                kind="int",
                default=6,
                minimum=2,
                maximum=50,
            ),
            Setting(
                id="spam_window_seconds",
                label="Seconds those messages may arrive in",
                help="The length of the window the message count is counted over.",
                kind="int",
                default=5,
                minimum=1,
                maximum=60,
            ),
            Setting(
                id="spam_timeout_minutes",
                label="Timeout length (minutes)",
                help=(
                    "How long a flooding member is timed out for. Zero records the "
                    "flood in the log without timing anybody out."
                ),
                kind="int",
                default=5,
                minimum=0,
                maximum=1440,
            ),
        ),
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
            "/customquote add", "/customquote list", "/customquote get",
            "/customquote delete",
            "/rizz", "/roast", "/scam", "/aura", "/flex",
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
        id="reaction_roles",
        name="Reaction Roles",
        icon="🎭",
        category="Community",
        description=(
            "A reaction on a message is a role: pick an emoji, point it at a role, "
            "and members take it off and put it back on themselves — with no "
            "command for them to learn and no moderator involved. Mappings are "
            "managed from this server's dashboard as well as from `/reactionrole`."
        ),
        gate_commands=("reactionrole",),
        commands=(
            "/reactionrole add", "/reactionrole remove", "/reactionrole list",
        ),
        cog="cogs.reaction_roles",
        help_key="reaction_roles",
    ),
    Feature(
        id="meetings",
        name="Meetings & Notices",
        icon="📅",
        category="Management",
        description=(
            "Staff and team meetings from Discord: a time, a place, who to tell, "
            "and notices that go out to a channel before it starts — with reply "
            "buttons on the card so you can see who is coming."
        ),
        gate_commands=("meeting",),
        # Spelled out by the subcommand a member types, because every surface
        # counts these rows and a group name nobody can run is a row the front
        # page advertises anyway.
        commands=(
            "/meeting create — Book one on a private card: when, where, who to ping, and the notices",
            "/meeting list — What this server has coming up",
            "/meeting view — One in detail, with replies and notice status",
            "/meeting reschedule — Move it and rebuild its notices",
            "/meeting ping — Send the notice to people now",
            "/meeting cancel — Cancel it and stand the notices down",
        ),
        cog="cogs.meetings",
        help_key="meetings",
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
    Feature(
        id="system",
        name="System & Server Control",
        icon="⚙️",
        category="Core",
        description=(
            "Server utility, announcements, lockdown, and configuration."
        ),
        # `/announce` is not here because *Moderation & Logs* gates it, and this
        # card cannot be switched off: listing it made the same command appear
        # under two systems, counted twice, and still promised by a card whose
        # system stays on when moderation is turned off.
        commands=(
            "/serverinfo", "/userinfo", "/bot sync", "/bot db",
            "/legal privacy", "/legal tos", "/bot dbstatus",
            "/defaultrole set", "/defaultrole view", "/defaultrole clear",
            "/check — Audit every channel for permission holes: the allow that "
            "beats a deny, the private channel a role can still see, and the lock "
            "whose deny is gone",
            "/claim — One chance per member to claim a floor of coins and level "
            "after a restore, up to a published ceiling",
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
# `cogs.automod` is here for the same mechanical reason: Auto Moderation is one
# file plus a registry row, so the cog loads as a core cog and gates itself on
# "automod" — one switch runs the invite, link and flood filters together.
#
# `cogs.support` is here because it is not a server's feature at all: the support
# channel is a deployment setting, the conversation belongs to the operator, and
# the cog registers no command — it is one listener over one channel, and a
# server that never asked for anything gets nothing from it.
CORE_COGS: tuple[str, ...] = (
    "cogs.server_control",
    "cogs.help",
    "cogs.legal",
    "cogs.bot_config",
    "cogs.automod",
    "cogs.support",
    # `cogs.claims` is here for `/claim`'s own reason: it is a core command, so
    # it must not load inside a feature cog whose `interaction_check` would
    # refuse it on a server that switched that system off.
    "cogs.claims",
)

# Commands that are always reachable regardless of module state. (The old
# hand-written list in bot.py still named a "/ping" command that no longer
# exists; tests/test_commands.py now fails if this list and the real command
# tree ever drift apart again.)
ALWAYS_ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "help", "bot", "serverinfo", "userinfo", "legal",
    # `/dbstatus` used to be here and is now `/bot dbstatus`. It left this list
    # with its name: what was ever meant by listing it is that asking whether the
    # database is healthy does not depend on the *moderation* switch, and the
    # group it now lives under is core by this same list — `/bot` is always
    # reachable, and so is everything under it.
    "defaultrole",
    # `/check` reads the server whether or not anything is switched on, and the
    # server whose systems are all off is the one whose administrator most needs
    # the reading — `fix` writes only what the lockdown ledger already holds, so
    # the audit cannot be the thing that changes a room nobody asked to shut.
    "check",
    # `/hidden` is `/check`'s question asked about the hides specifically — what is
    # off every list here, and which ranks can still see it — and it reads the ledger
    # rather than changing anything, so it belongs beside `/check` for the same
    # reason: the server whose systems are all switched off is the one whose owner
    # most needs the reading, and a reading a switch can hide is a reading that lies.
    "hidden",
    # `/cmd` is the index under `/help`, and it has to work in a server where
    # *every* system is switched off: that is exactly the server whose
    # administrator needs to read what there is to switch on. It reads the module
    # states rather than being governed by them, and it does its own scoping.
    "cmd",
    # `/claim` restores two figures that belong to the *member* — a wallet and a
    # level — and it is the answer to a restore that lost them. Gating it on the
    # economy or the leveling switch would make a server that switched either off
    # unable to give its members back what they had earned, which is the one
    # server where the command matters most. Its own latch makes it once ever, so
    # nothing is lost by leaving the door open.
    "claim",
    # `/add` and `/hide` are the server's own furniture rather than features:
    # binding which channel a system reports into, and taking a room off
    # everybody's list, are both things an owner may need in a server where every
    # switch is off — and neither is governed by a switch, which is what this list
    # says. `/hide` answers to the server's owner, exactly as `/check` does.
    "add",
    "hide",
})

# Commands that are safe to run in a DM (or that handle their own guild check).
# Everything else needs a guild: it reads `interaction.guild.id` for the wallet,
# the level row or the module toggle, and without this guard a DM invocation
# raised AttributeError and answered with "An unexpected error occurred".
DM_ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "help", "legal", "uwu", "8ball",
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


def setting_meta(spec: Setting) -> dict:
    """One declared setting as the dashboard template reads it.

    A plain mapping rather than the dataclass so the page cannot call a reader
    the endpoint would never have used: the two sides of a control post the same
    ids, the same kinds and the same bounds, because both were handed this.
    """
    return {
        "id": spec.id,
        "label": spec.label,
        "help": spec.help,
        "kind": spec.kind,
        "default": spec.default,
        "minimum": spec.minimum,
        "maximum": spec.maximum,
        "choices": list(spec.choices),
    }


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
            # The system's own settings, declared beside the system. Empty for
            # most; a card with settings draws a form under its switch.
            "settings": [setting_meta(spec) for spec in f.settings],
        }
        for f in FEATURES
        if f.toggleable
    ]


def ships_off() -> list[dict]:
    """The systems a server has to ask for, in the registry's own sentence.

    The third answer to *which of these is not like the others*: most systems
    arrive on and can be switched off, :func:`always_on_names` names the ones
    that cannot be switched off at all, and these arrive **off**. A visitor who
    installs the bot and is refused by `/market board` has been told nothing by
    a page that described the system only as *switchable like the rest*, so the
    page that lists every system prints this — on the page the reader is counting
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
    seen_keys: dict[str, str] = {}
    for f in FEATURES:
        if f.id in seen_ids:
            problems.append(f"duplicate feature id: {f.id}")
        seen_ids.add(f.id)
        if f.help_key:
            if f.help_key in seen_help:
                problems.append(f"duplicate help key: {f.help_key}")
            seen_help.add(f.help_key)
        if f.toggleable and not f.commands and not f.gate_commands and not f.settings:
            problems.append(f"toggleable feature '{f.id}' gates no commands")
        for spec in f.settings:
            key = setting_key(f.id, spec.id)
            owner = seen_keys.get(key)
            if owner:
                problems.append(
                    f"setting key '{key}' is claimed by both {owner} and {f.id}"
                )
            seen_keys[key] = f.id
            if spec.kind == "int" and spec.minimum is None and spec.maximum is None:
                problems.append(f"setting '{key}' is an int with no bounds")
    try:
        command_gates()
    except ValueError as exc:
        problems.append(str(exc))
    for cmd, ids in MODULE_COMMAND_MAP.items():
        for fid in ids:
            if fid not in seen_ids:
                problems.append(f"command '{cmd}' gated by unknown feature '{fid}'")
    return problems
