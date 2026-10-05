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
    rather than being clamped *upwards*: the only direction of corruption that
    matters is a stored number silently becoming a larger allowance than the
    default (the same rule ``utils.confirmations`` applies to its ceilings).
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
    # `cogs.automod` still loads through CORE_COGS for the same mechanical
    # reason radio and qna do — one `Feature.cog` path cannot hold a feature that
    # is one file plus a registry row — and it gates itself on "automod" rather
    # than on moderation. The settings below are the whole of its card: the two
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
            "notice naming `/appeal`. Every threshold and length below is this "
            "server's to set."
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
    # The one line above the starboard entry used to say Automod & Shield was
    # merged into Moderation & Logs. It is not merged any more — see the entry
    # above and `cogs/automod.py`'s own note about which switch it answers to.
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
        # Two subcommands, and the list names exactly those: there has never
        # been a `/starboard limit` (the reaction threshold is an argument of
        # `set`), and listing it made the page offer a command that answers
        # "unknown command" when somebody typed it.
        commands=("/starboard set", "/starboard status"),
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
    # Alone Time — one voice channel, locked to the one person holding it.
    #
    # **The one thing this cannot do the way it reads is keep an administrator
    # out with a permission.** Discord's Administrator permission and the guild
    # owner bypass channel overwrites by design, so a `Connect: false` overwrite
    # is a locked-looking door an admin walks through. What actually holds is the
    # room's **capacity cap** — `user_limit` set to the people allowed inside,
    # which Discord enforces at the voice gateway for everybody and which no
    # permission bit raises — with the bot moving anybody who still gets in back
    # out as the backstop. The card says which of the two is in force, and the
    # sweep says what Discord refused rather than claiming a lock it does not
    # hold. This paragraph is here rather than in the cog because the *promise*
    # is what a server reads on this card, and a promise is a registry fact.
    #
    # It ships **off**, and that is this release's whole shape: the system is
    # being run as a beta in one server, reached through the ordinary per-guild
    # switch rather than a guild id anywhere in the code. `enabled_by_default
    # =False` is what makes that mechanical — every other server's picker simply
    # never lists `/alone`, and `utils.modules.set_enabled` is the one flag that
    # changes it — and it is the same move the market, the starboard, Voice
    # Company and the legacy pair already make. Unlike those, nothing here is
    # being retired: the flag is on for the server running the beta, and the
    # plan is for the default to come back on once the beta has been through
    # its paces. Nothing else in this file, in the cog or in the ledger changes
    # with the flag — a server that turns it on gets exactly what the
    # description below promises.
    Feature(
        id="alonetime",
        name="Alone Time",
        icon="🌙",
        category="Community",
        description=(
            "Lock a voice channel to yourself. Nobody else can be inside it — not "
            "another member, not a moderator, and not an administrator — because "
            "the room's capacity is capped to the people allowed in, and anybody "
            "who still gets past the locked door is moved back out. Say why you "
            "are not to be disturbed — the reason is yours alone and is never "
            "written down — name the few people who may still join, let it lift "
            "itself when you leave, or quieten every voice channel in the server "
            "at once. A member who was turned away can ask you to let them in, "
            "once every five minutes."
        ),
        # One root command. Discord allows 100 top-level names per scope and the
        # subcommands live inside this one, which is why the whole feature is a
        # single slot in the picker rather than seven.
        gate_commands=("alone",),
        commands=(
            "/alone start — Lock the voice channel you are in to yourself",
            "/alone stop — Release the room you are holding",
            "/alone status — Who holds this room, and which layers are in force",
            "/alone guests — The few people who may still join",
            "/alone ask — Ask the holder to let you in (once every five minutes)",
            "/alone list — Every room locked in this server",
            "/alone release — Lift a lock you do not hold",
            "/alone server — Lock every voice channel for a while",
        ),
        cog="cogs.alonetime",
        # The beta flag, and the only thing standing between every other server
        # and a system none of them has asked for yet. Turning it back on for
        # everybody is deleting this line — see the paragraph above.
        enabled_by_default=False,
        help_key="alonetime",
        settings=(
            Setting(
                id="who",
                label="Who may lock a room",
                help=(
                    "Anyone in this server may lock the room they are standing in; "
                    "or only a member holding the role named below; or only "
                    "administrators. An administrator can always release any lock."
                ),
                kind="choice",
                default="everyone",
                choices=(
                    ("everyone", "Everyone in this server"),
                    ("role", "Only a role I name"),
                    ("admins", "Only administrators"),
                ),
            ),
            Setting(
                id="role_name",
                label="The role that may lock a room",
                help=(
                    "The name (or id) of the role, used only while *Who may lock a "
                    "room* is set to a role. A role always beats the name: matching "
                    "is by name or id, case-insensitively."
                ),
                kind="text",
                default="",
            ),
            Setting(
                id="guard",
                label="Move out anybody who still gets in",
                help=(
                    "The layer that makes the lock hold against an administrator: "
                    "the bot puts anyone who is not the holder or a guest back where "
                    "they came from. Needs Move Members, and says so when it is "
                    "missing or when Discord refuses the move."
                ),
                kind="bool",
                default=True,
            ),
            Setting(
                id="keep",
                label="Stay locked after the holder leaves",
                help=(
                    "Off (the default) means the lock lifts the moment the holder "
                    "steps out of the room, so nothing is left locked with nobody "
                    "in it. On means the room stays theirs until they release it."
                ),
                kind="bool",
                default=False,
            ),
            Setting(
                id="announce",
                label="Say it in the room's chat when somebody is turned away",
                help=(
                    "Posts a short card in the locked room's own text chat naming the "
                    "person and the reason, so being moved out is not a mystery."
                ),
                kind="bool",
                default=True,
            ),
            Setting(
                id="log_cases",
                label="Record locks in the moderation log",
                help=(
                    "Every lock, release, quiet-hours window and turned-away member "
                    "opens a card in this server's moderation log — the same rule "
                    "the automatic filters follow."
                ),
                kind="bool",
                default=True,
            ),
            Setting(
                id="evict",
                label="Where a turned-away member goes",
                help=(
                    "Put them back in the room they came from, disconnect them from "
                    "voice, or send them to this server's AFK channel. Putting them "
                    "back is the kindest and, for a move between two rooms, the "
                    "only one that leaves them connected."
                ),
                kind="choice",
                default="origin",
                choices=(
                    ("origin", "Back where they came from"),
                    ("disconnect", "Disconnected from voice"),
                    ("afk", "Sent to the AFK channel"),
                ),
            ),
            Setting(
                id="cooldown_seconds",
                label="Seconds between one lock and the next",
                help=(
                    "How long a member has to wait before locking another room, so a "
                    "room cannot be locked, released and locked again in a loop. "
                    "Zero removes the wait."
                ),
                kind="int",
                default=60,
                minimum=0,
                maximum=86400,
            ),
            Setting(
                id="max_hours",
                label="Longest a room may stay locked (hours)",
                help=(
                    "Zero — the default — means no ceiling: a lock ends when its "
                    "holder leaves, when somebody releases it, or when the system is "
                    "switched off. A number is the safety net a server asks for when "
                    "it wants nothing left locked by accident."
                ),
                kind="int",
                default=0,
                minimum=0,
                maximum=168,
            ),
            Setting(
                id="server_minutes",
                label="Length of a quiet-hours window (minutes)",
                help=(
                    "What `/alone server` locks every voice channel for when the "
                    "administrator does not say a length themselves."
                ),
                kind="int",
                default=60,
                minimum=5,
                maximum=1440,
            ),
            # Where the automated notices go — the room's lock card, somebody
            # being turned away, a lock ending. Empty is the resolved default:
            # this server's own #bot-commands (the house channel `utils/bot_channels`
            # already binds for automated output), then #general, and only if the
            # server has neither does a notice fall back to the locked room's own
            # chat. A server that wants them somewhere else says so here, by
            # channel id or by name, and that answer wins over the search.
            Setting(
                id="notice_channel",
                label="Where the notices go",
                help=(
                    "A channel id or name for Alone Time's notices — who locked "
                    "what, who was turned away, and when a room opened again. "
                    "Leave it empty and the bot uses #bot-commands, then "
                    "#general, then the room's own chat. Notices never go into "
                    "another member's room unless there is nowhere else."
                ),
                kind="text",
                default="",
            ),
        ),
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
        settings=(
            Setting(
                id="channel_delete",
                label="Channels one command may delete",
                help="Above this, `/structure channel-delete` stops and asks.",
                kind="int",
                default=1,
                minimum=0,
                maximum=200,
                key="confirmation_ceiling.channel_delete",
            ),
            Setting(
                id="channel_archive",
                label="Channels one archive may clear out",
                help="Above this, `/structure channel-archive` stops and asks.",
                kind="int",
                default=3,
                minimum=0,
                maximum=200,
                key="confirmation_ceiling.channel_archive",
            ),
            Setting(
                id="role_delete",
                label="Roles one command may delete",
                help="Above this, `/structure role-delete` stops and asks.",
                kind="int",
                default=1,
                minimum=0,
                maximum=200,
                key="confirmation_ceiling.role_delete",
            ),
            Setting(
                id="role_purge",
                label="Members one role-purge may touch",
                help="Above this, `/structure role-purge` stops and asks.",
                kind="int",
                default=5,
                minimum=0,
                maximum=200,
                key="confirmation_ceiling.role_purge",
            ),
        ),
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
            "/meeting create — Book one: when, where, who to ping, and the notices",
            "/meeting list — What this server has coming up",
            "/meeting view — One in detail, with replies and notice status",
            "/meeting reschedule — Move it and rebuild its notices",
            "/meeting ping — Send the notice to people now",
            "/meeting cancel — Cancel it and stand the notices down",
        ),
        cog="cogs.meetings",
        help_key="meetings",
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
    # Decat — ask the operator's own Mac a question, from Discord.
    #
    # Two halves on two machines, and it is worth being clear about which half
    # this registry describes. The *bot's* half is one command and a queue that
    # lives in memory; the half that does the work is a model on the operator's
    # own laptop, run locally through Ollama so that nothing about a question is
    # sent to any model vendor at all. So the switch below decides whether
    # `/decat` is offered in a server, and the machine behind it is the operator's
    # to keep awake — the command says plainly which of those two it is missing.
    #
    # It ships **off**, like every beta here, and the scope is a deployment fact
    # rather than a literal in the code: `DECAT_GUILDS` names the servers it may
    # run in, and a blank value leaves the per-server switch as the only gate —
    # the same arrangement Alone Time uses one entry above. The hosting Space
    # keeps **nothing**: no question, no answer, no transcript, in either mode,
    # which is why there are no tables for this feature and nothing here to
    # retain. A `public` question is written to the laptop's own history; a
    # `private` one is written nowhere at all.
    Feature(
        id="decat",
        name="Decat",
        icon="🛰️",
        category="Assistant",
        description=(
            "Ask a question and have it answered by the operator's own machine, "
            "which reads the live web, cites what it read, and says so when it "
            "could not reach it. Pictures can be asked about too. A question is "
            "`Private` — nothing anywhere is written down — or `Public`, which is "
            "kept on that machine alone. Anything legal, medical, money-related, "
            "safety-related or personal arrives with a plain note that this is an "
            "AI and not a professional, and a question that reads like somebody "
            "in danger is answered with a helpline rather than an opinion."
        ),
        gate_commands=("decat",),
        commands=(
            "/decat — Ask the operator's Mac, with sources, an optional picture, and Private|Public",
            "/bot decat — Whether that machine is answering, and how the last question went",
        ),
        cog="cogs.decat",
        # The beta flag: off everywhere until an administrator asks for it, and
        # the only lever that changes is `utils.modules.set_enabled`.
        enabled_by_default=False,
        help_key="decat",
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
    "cogs.radio",
    "cogs.qna",
    "cogs.automod",
    "cogs.support",
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
    # `/cmd` is the index under `/help`, and it has to work in a server where
    # *every* system is switched off: that is exactly the server whose
    # administrator needs to read what there is to switch on. It reads the module
    # states rather than being governed by them, and it does its own scoping.
    "cmd",
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
