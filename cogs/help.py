import discord
from discord import app_commands
from discord.ext import commands
from utils import bot_channels, command_directory as directory, features as feature_registry
from utils import modules as module_state
from utils.style import send_panel, ACCENT, DANGER, accent_for, avatar_url, guild_banner

HELP_MODULES = {
    "moderation": {
        "title": "🛡️ Moderation & Logs",
        "desc": (
            "Complete server protection: disciplinary actions, the audit log, and "
            "the doors a member has when an action lands on them. The automatic "
            "filters are their own system — see 🤖 **Auto Moderation**.\n\n"
            "> `/warn <member> <reason>` — Issue an official warning\n"
            "> `/warnings <member>` — View warning history for a user\n"
            "> `/clearwarnings <member>` — Clear all warnings for a user\n"
            "> `/mute <member> <duration> [reason]` — Timeout/mute a member\n"
            "> `/unmute <member>` — Remove timeout\n"
            "> `/kick <member> [reason]` — Kick a member from the server\n"
            "> `/ban <member> [delete_days] [reason]` — Ban a member\n"
            "> `/unban <user_id>` — Unban a user\n"
            "> `/purge <amount> [member]` — Bulk delete messages\n"
            "> `/appeal <reason>` — Ask a human to review an action taken against you\n"
            "> `/modlog <channel>` — Set the moderation audit log channel\n"
        "> `/modlog` — Show where the log goes, and whether I can write there\n"
        "> `/modlog off` — Stop logging, without losing the cases already recorded\n\n"
            "Every action — manual or automatic — is written to the moderation log, "
            "and the member it hit is told they can run `/appeal`."
        ),
        "color": 0xED4245,
        "emoji": "🛡️"
    },
    "automod": {
        "title": "🤖 Auto Moderation",
        "desc": (
            "The automatic shields, on one switch of their own — so a server can "
            "keep human moderation and still ask the robot to stand down.\n\n"
            "> `/automod invites <enabled>` — Block Discord invite links\n"
            "> `/automod links <enabled>` — Block ordinary links (an allowlist "
            "keeps the ones this server uses)\n"
            "> `/automod antispam <enabled>` — Flood protection\n"
            "> `/automod status` — What the filters are doing here right now\n\n"
            "Every automatic action leaves a case in the moderation log with the "
            "rule that fired, and the member it hit is told about `/appeal`. The "
            "thresholds — how many messages, over how long, and for how long a "
            "flooder is timed out — are set on this server's dashboard."
        ),
        "color": 0x5865F2,
        "emoji": "🤖"
    },
    "reaction_roles": {
        "title": "🎭 Reaction Roles",
        "desc": (
            "A reaction is a role: members take one off and put it back on "
            "themselves, with nothing for them to type.\n\n"
            "> `/reactionrole add <message> <emoji> <role>` — Link an emoji on a "
            "message to a role and let the bot react for you\n"
            "> `/reactionrole remove <message> <emoji>` — Stop a mapping\n"
            "> `/reactionrole list` — Every mapping this server has\n\n"
            "The dashboard's Server page manages the same list, with a picker for "
            "the role so nobody has to type an id."
        ),
        "color": 0xEB459E,
        "emoji": "🎭"
    },
    "starboard": {
        "title": "⭐ Community Starboard",
        "desc": (
            "Pin the funniest and most memorable messages to a dedicated showcase channel!\n\n"
            "> `/starboard set <channel> [threshold]` — Set showcase channel and star count (default: 3)\n"
            "> `/starboard status` — Check active Starboard configuration"
        ),
        "color": 0xFEE75C,
        "emoji": "⭐"
    },
    "social": {
        "title": "Safari Pets & Social Roleplay",
        "desc": (
            "Wild animal hunting, zoo collection, high-stakes crash, anime actions, and marriage!\n\n"
            "> `/hunt` — Catch rare animals in the wild\n"
            "> `/zoo [member]` — View your collected safari animals\n"
            "> `/sellanimals <tier>` — Sell animals for coins\n"
            "> `/action <type> <member>` — Perform roleplay actions (Bonk, Slap, Hug, Kiss, Pat, Punch, Bite, Cuddle, Poke, Feed)\n"
            "> `/marry <member>` — Propose marriage to another member\n"
            "> `/divorce` — End marriage\n"
            "> `/ship <member1> <member2>` — Calculate love compatibility percentage\n"
            "> `/uwu <text>` — Translate any message into cute anime text"
        ),
        "color": 0xE91E63,
        "emoji": "🐱"
    },
    "company": {
        "title": "🫂 Voice Company",
        "desc": (
            "When somebody is alone in a voice channel, the bot joins them — deafened "
            "and muted, so it can neither hear the room nor speak into it — and says "
            "hello. It leaves the moment somebody real arrives, and it never follows "
            "somebody who walked out.\n\n"
            "> `/company` — Auto (default) or Off for this server, and whether it says hi\n"
            "> A bot cannot watch a Go Live stream: Discord has no API for it. What it "
            "*can* see is that somebody is live, which the owner's dashboard shows."
        ),
        "color": 0x5865F2,
        "emoji": "🫂"
    },
    "alonetime": {
        "title": "🌙 Alone Time",
        "desc": (
            "Lock the voice channel you are in to yourself. Nobody else can be "
            "inside it — not another member, not a moderator, and not an "
            "administrator — because Discord enforces the room's **capacity** for "
            "everybody, and anything that still gets past the locked door is moved "
            "back out. The holder's own card carries a picker: whitelist somebody for "
            "this session, rename the room, and it all resets when the room empties. "
            "Say why you are not to be disturbed and it is shown back to you alone — "
            "it is never written to the database or the log, and the notices go to "
            "this server's own channel rather than into the locked room.\n\n"
            "> `/alone start` — Take the room you are standing in, name it, and say why\n"
            "> `/alone stop` — Give it back, exactly the way it was\n"
            "> `/alone status` — Who holds this room, which layers are in force, and the privacy answer\n"
            "> `/alone guests` — Who may still join, for this session only *(the holder)*\n"
            "> `/alone ask` — Ask the holder to let you in, once every five minutes\n"
            "> `/alone list` — Every locked room in this server *(Manage Channels)*\n"
            "> `/alone release` — Lift somebody else's lock *(Manage Channels)*\n"
            "> `/alone server` — Quiet hours: lock every voice channel for a while *(admin)*\n\n"
            "A lock lifts itself when you leave the room, and an administrator can "
            "always release one. What it deliberately does **not** have is a "
            "countdown: nothing expires unless this server asks for a ceiling in the "
            "dashboard. And one thing it cannot do is said plainly on every status "
            "card: whether your voice **activity** shows on your Discord profile is "
            "your own account setting (Activity Sharing) — no bot can change it."
        ),
        "color": 0x5865F2,
        "emoji": "🌙"
    },
    "economy": {
        "title": "💰 Economy & Currency",
        "desc": (
            "Full community banking, daily rewards, work jobs, and trading currency. "
            "Moving coins between members carries a small **DaCat Fee** that lands in "
            "DaCat Bank, and an admin can switch it off.\n\n"
            "> `/balance [member]` — Check wallet & bank balance\n"
            "> `/daily` — Claim 24-hour daily coin reward\n"
            "> `/weekly` — Claim 7-day weekly bonus\n"
            "> `/work` — Work shifts across various jobs for coins\n"
            "> `/pay <member> <amount>` — Transfer coins (a small DaCat Fee goes to DaCat Bank)\n"
            "> `/rob <member>` — Attempt a daring coin heist (the bank takes its cut of the loot)\n"
            "> `/deposit <amount>` — Deposit wallet coins into your bank\n"
            "> `/withdraw <amount>` — Withdraw bank coins into your wallet\n"
            "> `/richlist` — Server wealth leaderboard\n"
            "> `/loan [amount] [plan]` — Borrow 🪙 from DaCat Bank; your credit line grows with your level\n"
            "> `/repay [amount]` — Pay a loan back in full or in part, from wallet or bank\n"
            "> `/beg` — Beg for spare coins (2-minute cooldown, no dignity required)\n"
            "> `/eco-admin add / remove / set / reset` — Admin currency management\n"
            "> `/eco-admin fee` — See the DaCat Fee and DaCat Bank's balance, or turn the fee off (admin)"
        ),
        "color": 0x2ECC71,
        "emoji": "💰"
    },
    "market": {
        "title": "📈 DaCat Markets",
        "desc": (
            "A real market, priced by real exchanges — Binance and TradingView, with "
            "Kraken, Coinbase and CNBC as backups. No invented numbers: if a price "
            "cannot be fetched, trading pauses rather than guessing.\n\n"
            "> `/market board [category]` — Live prices with filters:\n"
            "  • `🌐 All` — Every instrument\n"
            "  • `🪙 Crypto` — Coins only\n"
            "  • `📊 Stocks` — Shares only\n"
            "  • `📈 ETFs` — Funds only\n"
            "  • `🚀 Top Gainers` — Best performers\n"
            "  • `📉 Top Losers` — Worst performers\n"
            "  • `🔥 Most Active` — Popular pairs\n"
            "> `/market quote <symbol>` — One instrument in detail, with a 24h sparkline\n"
            "> `/market chart <symbol> [24 hours|7 days|30 days]` — Real candles, "
            "with your average cost drawn on them\n"
            "> `/market buy <symbol> <coins>` — Buy with coins\n"
            "> `/market sell <symbol> <amount|50%|all>` — Sell back, with real profit/loss\n"
            "> `/market portfolio` — What you hold and what it is worth\n"
            "> `/market history` — Your recent fills\n\n"
            # Mirrors utils.market.COINS_PER_USD and MEOW_TREAT_FEE; kept as
            # literals so the help menu stays free of the market's HTTP
            # dependencies. ``tests/test_market.py`` fails if they drift.
            "*1 USD = 1 coin • fills happen at the observed market price on both "
            "sides, so a real move is the same move here, and the only charge is "
            "the 0.25% DaCat Treat fee — at least 1 coin, itemised on every "
            "receipt, and burned.*"
        ),
        "color": 0x2ECC71,
        "emoji": "📈"
    },
    "games": {
        "title": "🎮 Games & Casino",
        "desc": (
            "Competitive and luck-based casino games with coins.\n\n"
            "> `/crash <bet>` — High-stakes rocket crash game (cash out before it blows!)\n"
            "> `/mines <bet> [mines_count]` — Minefield grid multiplier\n"
            "> `/blackjack <bet>` — Interactive card game with Hit/Stand buttons\n"
            "> `/roulette <bet> <choice>` — Casino roulette wheel (red/black/green/numbers)\n"
            "> `/slots <bet>` — Triple-reel slot machine\n"
            "> `/coinflip <bet> <side>` — Double or nothing coin toss\n"
            "> `/dice <bet> <guess>` — 6-sided dice roll\n"
            "> `/highlow <bet>` — Guess if next card is higher or lower\n"
            "> `/tictactoe <member> [bet]` — Turn-based Tic-Tac-Toe duel with buttons\n"
            "> `/rps <choice> [bet]` — Rock, Paper, Scissors\n"
            "> `/trivia [category] [difficulty]` — Trivia quiz challenge\n"
            "> `/8ball <question>` — Magic 8-Ball oracle"
        ),
        "color": 0x34495E,
        "emoji": "🎮"
    },
    "leveling": {
        "title": "⚡ Levels, Ranks & Badges",
        "desc": (
            "Chat & voice leveling with XP rank cards and automated role rewards.\n\n"
            "> `/rank [member]` — Display ranked status card, XP progress, and badges\n"
            "> `/leaderboard [category]` — Server rank leaderboard (XP, Voice, Messages, Earnings)\n"
            "> `/levelroles view` — View all configured level role milestones\n"
            "> `/levelroles add <level> <role>` — Assign a role reward for hitting a level\n"
            "> `/levelroles remove <level>` — Delete a level role reward\n"
            "> `/levelsettings [channel] [enabled]` — Configure announcement channels\n"
            "> `/setlevel <member> <level>` — Admin level adjustment"
        ),
        "color": 0xF39C12,
        "emoji": "⚡"
    },
    "meetings": {
        "title": "📅 Meetings & Notices",
        "desc": (
            "Staff and team meetings, booked from Discord and announced on a card "
            "people can reply to.\n\n"
            "> `/meeting create <title> <when>` — Book one: when, where, who to "
            "ping, and the notices\n"
            "> `/meeting list` — What this server has coming up\n"
            "> `/meeting view <id>` — One in detail, with replies and notice status\n"
            "> `/meeting reschedule <id> <when>` — Move it and rebuild its notices\n"
            "> `/meeting ping <id>` — Send the notice to people right now\n"
            "> `/meeting cancel <id>` — Cancel it and stand the notices down\n\n"
            "Times you type are UTC (or add an offset like `18:00+2`), and every "
            "card draws them in each reader's own timezone. Notices go to the "
            "meeting's channel — `1h,15m` by default — and the buttons on the "
            "announcement record who is coming."
        ),
        "color": 0x5865F2,
        "emoji": "📅"
    },
    "tickets": {
        "title": "🎫 Support Tickets",
        "desc": (
            "Modern private channel support ticket system with problem modals and transcripts.\n\n"
            "> `/ticket setup` — Deploy the interactive ticket panel in the support channel\n"
            "> `/ticket add <member>` — Grant a user access to the current ticket channel\n"
            "> `/ticket remove <member>` — Remove a user from the ticket channel"
        ),
        "color": 0x3498DB,
        "emoji": "🎫"
    },
    "fun": {
        "title": "😆 Memes & Quotes",
        "desc": (
            "Top-tier viral memes and iconic quotes.\n\n"
            "> `/meme [category]` — Viral Reddit memes with interactive 🔄 Next Meme button\n"
            "> `/quote [category] [mood]` — Iconic anime, cartoon, movie, and motivation quotes\n"
            "> `/customquote add / get / list / delete` — Server custom quote collection\n"
            "> `/meow` & `/meowsound` — Playful cat media\n"
            "> `/kms` — Relatable funny life checks\n"
            "> `/rizz [target]` — Drop a pickup line & get your rizz score 🔥\n"
            "> `/roast <member>` — Savage AI roast generator 💀\n"
            "> `/scam` — Join a fake investment scheme (comedy only 😂)\n"
            "> `/aura [member]` — Scan someone's aura and get an instant verdict 🔮\n"
            "> `/flex [member]` — Flex your net worth; the bank statement settles the argument 💸"
        ),
        "color": 0xE74C3C,
        "emoji": "😆"
    },

    "tracker": {
        "title": "📊 Activity Tracker",
        "desc": (
            "Per-day, per-channel message and voice time tracking with a 365-day rolling window.\n\n"
            "> `/activity me [member] [period]` — Your personal message & voice stats\n"
            "> `/activity top [category] [period]` — Server-wide leaderboards\n"
            "> `/activity channel [channel] [period]` — Per-channel breakdown\n\n"
            "*Periods: Today, This Week, This Month, This Year (365d), All Time*"
        ),
        "color": 0x5865F2,
        "emoji": "📊"
    },
    "system": {
        "title": "⚙️ System & Server Control",
        "desc": (
            "Server utility, announcements, lockdown, and configuration.\n\n"
            "> `/serverinfo` — Server overview, boost level, and statistics\n"
            "> `/userinfo [member]` — Member profile, roles, and join date\n"
            "> `/announce <channel> <message>` — Send formatted community announcements\n"
            "> `/lockdown` & `/unlock` — Emergency channel lockdown; messages sent while locked are removed\n"
            "> `/slowmode <seconds>` — Adjust channel chat cooldown\n"
            "> `/bot sync` — Force re-sync all slash commands directly to this server\n"
            "> `/bot db` — Verify all database tables and default channels\n"
            "> `/bot bug` — Report a bug in the bot itself (administrators)\n"
            "> `/legal privacy` & `/legal tos` — Bot privacy policy and terms of service"
        ),
        "color": 0x95A5A6,
        "emoji": "⚙️"
    },
    "structure": {
        "title": "🧱 Server Structure",
        "desc": (
            "Channels and roles, made and cleared away from here. These are the "
            "commands that can *destroy* something — a deleted channel takes every "
            "message in it, and Discord keeps no copy — so they are built around "
            "two rules rather than one permission bit.\n\n"
            "**Size is checked first.** Deleting the one channel you named is what "
            "you asked for; clearing a category or taking a role off everybody who "
            "has it stops and asks *you*, by a press, with the number said out "
            "loud. `/structure limits` shows what this server asks about.\n"
            "**Everything is written down.** Each one opens a case in this "
            "server's moderation log — what changed, who asked, and why — and a "
            "refusal is written down too, because \"Discord said no\" is the thing "
            "an administrator most needs to know.\n\n"
            "> `/structure channel-create <name> [category] [private]`\n"
            "> `/structure channel-delete <channel>`\n"
            "> `/structure channel-archive <category> [keep]` — confirmed\n"
            "> `/structure role-create <name> [colour]`\n"
            "> `/structure role-delete <role>`\n"
            "> `/structure role-purge <role>` — confirmed\n"
            "> `/structure limits` — the ceilings for this server *(admin)*"
        ),
        "color": 0x2dd4bf,
        "emoji": "🧱"
    },
    "legacy": {
        "title": "📻 Radio & Anonymous Q&A",
        "desc": (
            "Off in every server to begin with: switched **off** until an "
            "administrator asks for them, and one flag turns both on. A server "
            "that relied on either turns it on once and keeps it.\n\n"
            "> `/bot legacy state:on` — the switch, for this server only *(admin)*\n\n"
            "**Radio** (📻) — 24/7 stations by genre, language, mood or country, "
            "played as direct streams that ask no media platform for anything. "
            "There is no song search, and a station that goes quiet is re-asked "
            "rather than skipped, so it never looks like a short track.\n"
            "> `/radio play [station] [language] [country] [ads] [stay] [volume]`\n"
            "> `/radio stop` — end it and give the channel back\n"
            "> `/radio doctor` — what this host can and cannot do for voice *(admin)*\n\n"
            "**Anonymous Q&A** (❓) — a question box with nobody's name on it. What "
            "you ask is posted by the bot's own webhook and the answer comes back "
            "as a DM. Who asked is written down beside the question and shown to "
            "**one** person, the bot's owner, who answers it: the question arrives "
            "as the box, not as a member.\n"
            "> `/qna ask` — ask something anonymously\n"
            "> `/qna inbox` / `/qna reply` — the queue and the reply *(bot's own server)*"
        ),
        "color": 0x5865F2,
        "emoji": "📻"
    },
    "decat": {
        "title": "🛰️ Decat",
        "desc": (
            "A question goes to the operator's own Mac, which reads the live web "
            "and answers with the pages it read. The model runs **on that "
            "machine**, so nothing about a question is sent to a model vendor — "
            "and this bot's host keeps nothing either, in either mode.\n\n"
            "> `/decat <question>` — ask it. Add a picture with `image:`\n"
            "> `mode: Public` — everyone sees the answer, and it is kept on that "
            "machine alone\n"
            "> `mode: Private` — only you see it, and **nothing** is written "
            "anywhere *(the default)*\n"
            "> `/bot decat` — whether the machine is answering *(admin)*\n\n"
            "Every answer names its sources, and says so when it could not reach "
            "any. Anything legal, medical, money-related, safety-related or "
            "personal arrives with a plain note that this is an AI and not a "
            "professional — and a question that reads like somebody in danger is "
            "answered with a helpline rather than an opinion. Ship a question and "
            "find the machine asleep and it says *sry, offline rn*."
        ),
        "color": 0x5865F2,
        "emoji": "🛰️"
    }
}




# Hand-written prose, and this string is exactly why the drift check below is
# not enough: it compares *keys*, and Automod & Shield was merged into
# Moderation & Logs — the registry and `HELP_MODULES` both moved in that pass,
# and this list kept a bullet for a system that no longer exists beside a
# moderation bullet whose name and emoji were already out of date. A category
# named here is the first thing a reader sees on the card, so a stale one is the
# help menu advertising a system the bot does not have.
HELP_HOME_BODY = (
    "Your all-in-one command center. Pick a category below to open its full command list.\n\n"
    "- 🛡️ **Moderation & Logs** — warns, bans, timeouts, and the audit log every action lands in\n"
    "- 🤖 **Auto Moderation** — the invite, link and flood shields, each the server's own to tune\n"
    "- 🎭 **Reaction Roles** — an emoji on a message that hands out a role by itself\n"
    "- ⭐ **Starboard** — community highlights\n"
    "- 💰 **Economy & Games** — bank, casino, minigames\n"
    "- ⚡ **Leveling** — XP, ranks, reward roles\n"
    "- 🐾 **Social & Fun** — pets, roleplay, memes, quotes\n"
    "- 🎫 **Tickets** — private staff support\n"
    "- 📊 **Activity Tracker** — per-channel msg & voice stats\n"
    "- 🧱 **Server Structure** — channels and roles, with a confirmation on anything big\n"
    "- 📻 **Radio & Anonymous Q&A** — stations by language and country, and a question box with no names\n"
    "- 🌙 **Alone Time** — take a voice channel for yourself, and keep everybody else out of it\n"
    "- 🛰️ **Decat** — ask the operator's own Mac a question, answered from the live web"
)

# The category *structure* comes from the feature registry; the prose below
# stays hand-written. This check makes drift loud instead of silent.
_missing_keys = feature_registry.HELP_KEYS - set(HELP_MODULES)
_extra_keys = set(HELP_MODULES) - feature_registry.HELP_KEYS
if _missing_keys or _extra_keys:
    print(
        f"[HELP] ⚠️ Help menu and feature registry disagree — "
        f"missing={sorted(_missing_keys)} extra={sorted(_extra_keys)}",
        flush=True,
    )

TITLE_ID = 1101
BODY_ID = 1102


class HelpSelectMenu(discord.ui.Select):
    def __init__(self):
        options = []
        # Iterate the registry so the menu order and membership always match the
        # rest of the bot (a retired feature disappears from help automatically).
        for feature in feature_registry.FEATURES:
            data = HELP_MODULES.get(feature.help_key)
            if not data:
                continue
            label = data["title"].split(" ", 1)[1] if " " in data["title"] else data["title"]
            options.append(discord.SelectOption(
                label=label[:100],
                value=feature.help_key,
                emoji=data["emoji"],
                description=f"Browse {feature.id} commands"[:100],
            ))
        super().__init__(
            placeholder="Browse a command category…",
            min_values=1,
            max_values=1,
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        data = HELP_MODULES.get(self.values[0], next(iter(HELP_MODULES.values())))
        title = self.view.find_item(TITLE_ID)
        body = self.view.find_item(BODY_ID)
        if title:
            title.content =            f"## {data['title']}"
        if body:
            body.content = data["desc"]
        for child in self.view.children:
            if isinstance(child, discord.ui.Container):
                # Through the palette like every other card: a category may name a
                # colour, and the only thing it may end up as is the accent or a
                # state — twelve decorative hues were twelve accents on one screen.
                child.accent_color = accent_for(data["color"])
        await interaction.response.edit_message(view=self.view)


# Discord refuses a text input whose placeholder runs past 100 characters, and it
# refuses it when the modal is *opened* — so the member never sees the form at
# all, only "An unexpected error occurred". This string sat at 122 and `/bot bug`
# was dead for everyone. `tests/test_static_health.py` now holds every component
# string in the tree to its limit.
BUG_REPORT_HINT = (
    "What happened, and what you expected instead? The steps you took, and any "
    "error text."
)
#: The two things a report needs before anybody can *look*: which command, and
#: when. The card carries the time the report was filed, which is often hours
#: after the thing broke — so "when it happened" is asked for separately.
#: Discord caps a placeholder at 100 characters and a label at 45, and refuses
#: the whole modal when one is over; both are measured by
#: ``tests/test_static_health.py``.
BUG_COMMAND_HINT = "e.g. /balance — leave it blank if it was not a command"
BUG_WHEN_HINT = "e.g. “today around 19:40” or “about ten minutes ago”"


class BugReportModal(discord.ui.Modal, title="Report a bug"):
    """The free-text half of a report; the destination is resolved on submit."""

    def __init__(
        self,
        channel_ids: list[int],
        source: str = "the /help panel",
        command: str = "",
        when: str = "",
    ):
        super().__init__()
        self.channel_ids = list(channel_ids)
        self.source = source
        self.what_happened = discord.ui.TextInput(
            label="What went wrong?",
            placeholder=BUG_REPORT_HINT,
            style=discord.TextStyle.paragraph,
            max_length=1500,
            required=True,
        )
        self.command_used = discord.ui.TextInput(
            label="Command you ran",
            placeholder=BUG_COMMAND_HINT,
            default=_prefill(command, 100),
            max_length=100,
            required=False,
        )
        self.when_happened = discord.ui.TextInput(
            label="When did it happen?",
            placeholder=BUG_WHEN_HINT,
            default=_prefill(when, 100),
            max_length=100,
            required=False,
        )
        self.add_item(self.what_happened)
        self.add_item(self.command_used)
        self.add_item(self.when_happened)

    async def on_submit(self, interaction: discord.Interaction):
        guild = getattr(interaction, "guild", None)
        # ``getattr`` like the rest of this module: a partial stand-in (and a
        # Discord interaction object that is mid-reconnect) must not turn filing
        # a bug report into a second bug.
        here = getattr(interaction, "channel", None)
        report = str(self.what_happened.value or "").strip()
        command = str(self.command_used.value or "").strip()
        when = str(self.when_happened.value or "").strip()
        embed = discord.Embed(
            title="🐞 Bug report",
            description=report[:4000] or "*(no description given)*",
            color=DANGER,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Reported by", value=interaction.user.mention, inline=True)
        embed.add_field(
            name="Server",
            # The id, not just the name: this is what makes the card joinable to
            # the command log and to ``guild_modules`` when the report is checked.
            value=(f"{guild.name} · `{guild.id}`" if guild is not None else "Direct message"),
            inline=True,
        )
        embed.add_field(
            name="Channel",
            value=(f"#{getattr(here, 'name', 'unknown')}" if here is not None else "—"),
            inline=True,
        )
        # Both are always shown, even when blank. A report cannot be checked
        # without the command and the time, so "not given" is the useful thing to
        # see — an absent field reads as a field nobody asked for.
        embed.add_field(name="Command", value=command or "*not given*", inline=True)
        embed.add_field(name="When it happened", value=when or "*not given*", inline=True)
        embed.add_field(name="Filed via", value=self.source, inline=True)
        filed = int(discord.utils.utcnow().timestamp())
        embed.set_footer(
            text=f"User ID {interaction.user.id} · filed <t:{filed}:f>"
        )

        # Targets, best first — and the first one that accepts the post ends the
        # walk, so one report is never posted twice. A server with no bug channel
        # of its own still files: the bot's own server is the next target.
        refusal = ""
        for channel_id in self.channel_ids:
            channel = interaction.client.get_channel(channel_id)
            if channel is None:
                continue
            try:
                message = await channel.send(embed=embed)
            except discord.Forbidden:
                refusal = f"I don't have permission to post in {channel.mention}"
                continue
            except Exception as exc:
                print(f"[BUG] Could not file the report in {channel_id}: {exc}", flush=True)
                refusal = f"posting in {channel.mention} failed"
                continue
            follow_up = await _open_report_thread(message, report, interaction, command)
            where = channel.mention
            if getattr(getattr(channel, "guild", None), "id", None) != getattr(guild, "id", None):
                # Filed in the bot's own server on this server's behalf: say so, or
                # the reporter has no idea why the channel they cannot see is named.
                where = f"{channel.mention} — the bot's own server, **{channel.guild.name}**"
            return await interaction.response.send_message(
                f"✅ Thanks — your report is in {where}.{follow_up}",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"❌ Nothing was posted — {refusal or 'the bug channel is gone'}. "
            "Send the report to an administrator directly.",
            ephemeral=True,
        )


async def _open_report_thread(message, report: str, interaction, command: str = "") -> str:
    """A thread on the report, or ``""`` if this server will not have one.

    The command leads the thread's name when the reporter gave one, because a
    list of threads is scanned by what was being run — "🐞 /balance" says more
    than the first line of a paragraph does.
    """
    try:
        first_line = (report.splitlines() or [""])[0].strip()
        named = f"/{command.lstrip('/')} {first_line}".strip() if command else first_line
        thread = await message.create_thread(
            name=f"🐞 {named[:95] or interaction.user.name}",
            auto_archive_duration=1440,
        )
        return f" Follow it in {thread.mention}."
    except Exception as e:
        print(f"[BUG] Could not open a thread on the report: {e}", flush=True)
        return ""


def _prefill(value: str, limit: int) -> str | None:
    """A modal field's starting text, or ``None`` so it opens empty.

    ``/bot bug command:/balance when:2 minutes ago`` fills the form in rather than
    making somebody type the same sentence twice; a field with nothing to prefill
    is left empty rather than showing a blank string.
    """
    text = str(value or "").strip()
    return text[:limit] or None


async def open_bug_report(
    interaction: discord.Interaction,
    source: str = "the /help panel",
    command: str = "",
    when: str = "",
):
    """Open the report modal, or say plainly why nothing can be filed.

    One entry point for the panel button and for ``/bot bug`` alike: the
    destinations are resolved by the same call either time, which is the only
    reason a report sent from either surface ends up in the same place.

    ``command`` and ``when`` are optional and only *prefill* the form: the report
    still cannot be filed without saying what went wrong, and a reporter who
    gives them at the command line should not have to type them again.
    """
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message(
            "Bug reports need a server to file them in — open `/help` in your "
            "server, or run `/bot bug` there.",
            ephemeral=True,
        )
    targets = await bot_channels.report_targets(guild.id, "bug", client=interaction.client)
    if not targets:
        return await interaction.response.send_message(
            "❌ Nowhere to file that yet: this server has no bug-report channel "
            "and I cannot reach the bot's own. A channel named `#bug` (or "
            "`#bug-report`) is picked up on the next startup, and `/bot db` binds "
            "one without a restart.",
            ephemeral=True,
        )
    await interaction.response.send_modal(
        BugReportModal(targets, source=source, command=command, when=when)
    )


class BugReportButton(discord.ui.Button):
    """The panel's entry point into the same report path ``/bot bug`` uses.

    It predates the command: the bot was at Discord's hundred-command ceiling
    when this was written, so the entry point rode on the panel people already
    open. Both are kept because they are for different people — anyone can report
    a bug from here, while ``/bot bug`` is the administrator's way in.
    """

    def __init__(self):
        super().__init__(
            label="Report a bug", emoji="🐞", style=discord.ButtonStyle.secondary
        )

    async def callback(self, interaction: discord.Interaction):
        await open_bug_report(interaction, source="the /help panel")


def dashboard_link(interaction: discord.Interaction) -> str | None:
    """A one-time dashboard sign-in link, or ``None`` if this member cannot use one.

    ``/dashboard`` was its own command until the bot reached Discord's hundred-
    command ceiling; the entry point now rides on the panel administrators already
    open, which is the same reason the bug button lives here. The link is minted
    for administrators only, and only where the panel is opened.
    """
    try:
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is None or not permissions.administrator or not interaction.guild_id:
            return None
        from keep_alive import dashboard_base_url, generate_magic_token

        token = generate_magic_token(interaction.guild_id, interaction.user.id)
        # The token lives in the memory of the process that made it, so the link
        # has to name that process rather than a fixed host that may be another
        # instance entirely.
        base = dashboard_base_url()
        return f"{base}/login?token={token}" if base else None
    except Exception:
        # A panel that cannot offer a sign-in is still a working help panel.
        return None


class HelpLayout(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        thumbnail: str | None,
        gallery: list[str] | None = None,
        dashboard: str | None = None,
    ):
        super().__init__(timeout=300)
        heading = discord.ui.TextDisplay("## DaCat Command Center", id=TITLE_ID)
        body = discord.ui.TextDisplay(HELP_HOME_BODY, id=BODY_ID)
        thumb = discord.ui.Thumbnail(thumbnail) if thumbnail else None
        parts = []
        if thumb is not None:
            parts.append(discord.ui.Section(heading, accessory=thumb))
        else:
            parts.append(heading)
        parts.append(body)
        if gallery:
            parts.append(discord.ui.MediaGallery(*[discord.MediaGalleryItem(u) for u in gallery[:4]]))
        # The category menu and the bug button are **two rows**, and that is a
        # Discord rule rather than a taste: an ActionRow is either one select
        # (which takes the row's whole width) or up to five buttons, and a row
        # holding both is refused with `50035 COMPONENT_LAYOUT_WIDTH_EXCEEDED`.
        # Discord names the offender as `components.0.components.3.components.1`
        # — the button beside the menu — and the member sees only "An unexpected
        # error occurred", because a refused interaction response is a failed
        # command. That is what happened to every `/help` in production, and it
        # is why the panel is built as separate rows here instead of one strip.
        # `utils.style.legal_rows` now holds the same rule for every other card.
        menu_row = discord.ui.ActionRow()
        menu_row.add_item(HelpSelectMenu())
        parts.append(menu_row)
        bug_row = discord.ui.ActionRow()
        bug_row.add_item(BugReportButton())
        parts.append(bug_row)
        if dashboard:
            link_row = discord.ui.ActionRow()
            link_row.add_item(
                discord.ui.Button(
                    label="Open Dashboard",
                    url=dashboard,
                    style=discord.ButtonStyle.link,
                    emoji="⚡",
                )
            )
            parts.append(link_row)
        parts.append(discord.ui.TextDisplay("-# 🌐 [Website & Pricing](https://dacat.pages.dev)  ·  Tap a category, or `/cmd` for the whole list  ·  DaCat"))
        self.add_item(discord.ui.Container(*parts, accent_color=ACCENT))


# --------------------------------------------------------------------------- #
# /cmd — the whole directory, scoped to whoever asked
# --------------------------------------------------------------------------- #
# `/help` is prose: one card per system, written by hand, telling a reader what a
# system *is*. `/cmd` is the index underneath it, and it exists because the prose
# cannot answer the question a member actually has — "what can I run, right
# here, right now" — nor the one an administrator has, "what is this bot even
# offering". Both answers come from tables rather than from a list written for the
# occasion: the tree says what exists, `utils.features` says what each row belongs
# to, and `utils.permissions` says who may run it.
#
# The panel is always **ephemeral**, and that is the one place it deliberately
# parts company with `/help`. A help panel is the same for everybody and belongs
# in the channel; this card is about the person holding it — their permissions,
# their server's switches — so posting it publicly would be the bot telling a
# channel what one member cannot do.
CMD_TITLE_ID = 1201
CMD_BODY_ID = 1202
#: The select value for the whole directory, and the page key for a search.
CMD_ALL = "all"
CMD_SEARCH_ID = "search"
#: Discord cuts a Components V2 message off at 4,000 characters, and the titles,
#: the headings and the footer are all part of that budget — so a page stops short
#: of it and says what it left out rather than being refused. The whole directory
#: for an administrator with every permission is about 2,700 of these, which is
#: why the number has headroom rather than being the measurement of today.
CMD_PAGE_BUDGET = 3200

CMD_DESCRIPTION = "Every command you can run here, grouped by system, with a search."
CMD_SEARCH_LABEL = "Search commands"
CMD_SEARCH_HINT = "ban, coins, casino — a name, a word from what it does, or a system"
CMD_FOOTER = (
    "-# `/cmd search:<word>` or the 🔍 button searches  ·  `/help` describes "
    "each system  ·  DaCat"
)

#: How one reason list reads on the card. The sentences themselves come from
#: :mod:`utils.command_directory` — it owns every refusal, so this only decides
#: how a *count* of one is described.
REASON_NOUNS = {
    directory.OFF: "switched off on this server",
    directory.PERMISSION: "behind a permission you do not have",
    directory.OWNER: "for the server owner alone",
    directory.OPERATOR: "the operator's own",
    directory.MARKET: "behind the market role",
}

#: The systems a member can ask for by name. Built from the registry, so a system
#: added or retired moves this list with it, and under Discord's 25-choice cap
#: with five to spare.
CMD_SYSTEM_CHOICES = [
    app_commands.Choice(name=f"{feature.icon} {feature.name}", value=feature.id)
    for feature in feature_registry.FEATURES
] + [
    app_commands.Choice(
        name=f"{directory.CORE_ICON} {directory.CORE_NAME}", value=directory.CORE_ID
    )
]


def _switched_off_here(viewer) -> list[str]:
    """The systems this server has off, by name.

    Read from the merged state the caller gathered — the server's own rows, its
    plan and the operator's override — so the line matches the switches on every
    other surface. A system with one switch is off when that switch is off;
    the core group has no switch at all and never appears here.
    """
    return [
        f"{feature.icon} {feature.name}"
        for feature in feature_registry.FEATURES
        if feature.toggleable
        and not bool(viewer.states.get(feature.id, feature.enabled_by_default))
    ]


def _scope_line(scoped, viewer, where: str = "") -> str:
    """The card's opening claim: what is yours, and what is not.

    Three sentences at most, and the third is the one that keeps the first two
    honest: two numbers about the word "command" with no explanation of the gap
    is the exact failure ``utils/features.py`` was written to stop.
    """
    place = f" in **{where}**" if where else " here"
    lines = [f"**{len(scoped.usable)} of {scoped.total}** commands can be run by you{place}."]
    if scoped.withheld:
        counts = " · ".join(
            f"{scoped.count(kind)} {REASON_NOUNS.get(kind, kind)}" for kind in scoped.order
        )
        lines.append(f"-# {counts}")
    else:
        lines.append("-# Nothing is held back from you here.")
    off = _switched_off_here(viewer)
    if off:
        lines.append(
            "-# Switched off here: " + ", ".join(off)
            + ". `/cmd system:<name>` still lists what one would offer."
        )
    return "\n".join(lines)


def _row(command, viewer) -> str:
    """One command as a line: its name, the registry's sentence, and any caveat."""
    line = f"> {command.text}"
    if command.note:
        line += f" — {command.note}"
    cue = directory.caveat(command, viewer)
    if cue:
        line += f" *({cue})*"
    return line


def _rows(commands, viewer, *, budget: int = CMD_PAGE_BUDGET) -> list[str]:
    """Rows for one page, trimmed to the message budget and never silently.

    A directory that quietly stops at row ninety would be worse than one that
    says "and twelve more", because the reader cannot tell the difference between
    a short list and a truncated one.
    """
    lines: list[str] = []
    used = 0
    for index, command in enumerate(commands):
        line = _row(command, viewer)
        if lines and used + len(line) > budget:
            lines.append(f"-# …and {len(commands) - index} more.")
            break
        lines.append(line)
        used += len(line) + 1
    return lines


def _withheld_lines(held, *, budget: int = CMD_PAGE_BUDGET) -> list[str]:
    """What is not offered, grouped by why, in the module's own words.

    Grouped rather than repeated per row: a system that is switched off would
    otherwise print the same sentence once for each of its commands, which buries
    the two rows underneath it that a member could actually do something about.

    The operator's own rows are counted and not named — the rule
    :mod:`utils.permissions` states for its ``DEV`` audience, applied to a listing
    rather than to a picker: a member reading a name they may not have is a leak.
    """
    grouped: dict[str, list[str]] = {}
    private = 0
    for entry in held:
        if entry.reason.private:
            private += 1
            continue
        grouped.setdefault(entry.reason.text, []).append(entry.command.text)
    lines: list[str] = []
    used = 0
    for reason, names in grouped.items():
        block = [f"-# {reason}", " · ".join(names)]
        if lines and used + sum(len(part) for part in block) > budget:
            lines.append("-# …and more that this card has no room for.")
            break
        lines.extend(block)
        used += sum(len(part) for part in block)
    if private:
        lines.append(f"-# …and {private} only the operator can run.")
    return lines


def _home_body(scoped, viewer, *, where: str = "") -> str:
    """The whole directory: every name this member can run, system by system.

    Names only. The card cannot hold a sentence per command and Discord would
    refuse the message if it tried, so the *sentences* live one press away — the
    menu opens a system, and a search shows the note on every match.

    The whole list is printed, and it fits: this deployment's worst case — an
    administrator with every permission, in a server with every switch on, whose
    name is as long as Discord allows — measures **2,461** characters, and
    ``tests/test_cmd.py`` holds it there. What is not trusted is the future: at
    roughly a hundred and forty commands the card is still half empty and at a
    thousand it would be refused outright, and a refused payload is a command that
    answers "An unexpected error occurred". So the groups are added while they fit
    and any that do not are **named** on the card rather than dropped from it.
    """
    parts = [_scope_line(scoped, viewer, where)]
    groups = directory.grouped(scoped.usable)
    used = len(parts[0])
    left_out: list[str] = []
    for group in groups:
        names = "  ·  ".join(command.text for command in group.commands)
        block = f"### {group.icon} {group.name} · {len(group.commands)}\n{names}"
        if used + len(block) > CMD_PAGE_BUDGET:
            left_out.append(f"{group.icon} {group.name}")
            continue
        parts.append(block)
        used += len(block) + 2
    if left_out:
        parts.append(
            "-# No room left on this card for: " + ", ".join(left_out)
            + ". The menu below opens each one."
        )
    if not scoped.usable:
        parts.append(
            "Nothing at all is switched on for you here — an administrator can "
            "open any system at [dacat.pages.dev](https://dacat.pages.dev)."
        )
    return "\n\n".join(parts)


def _group_body(group, held, viewer) -> str:
    """One system: what it gives this member, and what it would not."""
    parts: list[str] = []
    if group.commands:
        parts.extend(_rows(group.commands, viewer))
    else:
        parts.append(f"Nothing in **{group.name}** is yours to run here.")
    if held:
        parts.append("### Not offered to you")
        parts.extend(_withheld_lines(held))
    return "\n".join(parts)


def _search_body(scoped, viewer, query: str) -> str:
    """A search, answered against both halves of the directory.

    The withheld half is searched too, and that is the whole point of doing it
    here rather than at the picker: a member searching for ``ban`` has asked a
    real question, and "nothing found" is a lie when `/ban` exists and needs a
    permission they have not been given.
    """
    usable = directory.search(scoped.usable, query)
    missed = directory.search((held.command for held in scoped.withheld), query)
    reasons = {entry.command.path: entry.reason for entry in scoped.withheld}
    parts: list[str] = []
    if usable:
        parts.append(
            f"**{len(usable)}** command{'' if len(usable) == 1 else 's'} you can run "
            f"match **{query}**."
        )
        parts.extend(_rows(usable, viewer))
    else:
        parts.append(f"No command you can run matches **{query}**.")
    if missed:
        held = [directory.Held(command, reasons[command.path]) for command in missed]
        parts.append("### Not offered to you")
        parts.extend(_withheld_lines(held))
    if not usable and not missed:
        parts.append(
            "-# Try a name (`/balance`), a word from what it does (`coins`), or a "
            "system (`casino`)."
        )
    return "\n".join(parts)


def _system_page(rows_all, scoped, viewer, system: str):
    """One system's page, from its id — for `system:` and for the menu alike."""
    for group in directory.grouped(rows_all):
        if group.id != system:
            continue
        held = [entry for entry in scoped.withheld if entry.command.system_id == system]
        mine = tuple(command for command in scoped.usable if command.system_id == system)
        current = directory.Group(group.id, group.name, group.icon, mine)
        return f"## {group.icon} {group.name}", _group_body(current, held, viewer)
    return None


def _cmd_options(scoped, *, extra: tuple[str, str, str] | None = None) -> list:
    """The menu's options: one per system this member can actually run.

    Systems with nothing for them are left out rather than shown empty — the
    select is a directory of *their* commands, and a row that opens onto "nothing
    here is yours" is a row that wasted a press. `system:` still reaches them by
    name, which is how somebody asks about a system they cannot use.
    """
    options = []
    if extra is not None:
        value, label, description = extra
        options.append(
            discord.SelectOption(label=label[:100], value=value, description=description[:100])
        )
    options.append(
        discord.SelectOption(
            label="📖 Everything",
            value=CMD_ALL,
            description="The whole directory",
        )
    )
    for group in directory.grouped(scoped.usable):
        options.append(
            discord.SelectOption(
                label=f"{group.icon} {group.name}"[:100],
                value=group.id,
                description=f"{len(group.commands)} command"
                + ("" if len(group.commands) == 1 else "s"),
            )
        )
    return options[:25]


class CmdSearchModal(discord.ui.Modal, title="Search commands"):
    """The same search, asked again without retyping the command.

    ``/cmd search:coins`` is the whole feature; this is why a member who has just
    found nothing can try another word, instead of typing the command again and
    again in a channel. It re-renders through the *same* renderer as the command,
    so the two can never answer differently.
    """

    def __init__(self, layout: "CmdLayout | None"):
        super().__init__()
        self.layout = layout
        self.query = discord.ui.TextInput(
            label=CMD_SEARCH_LABEL,
            placeholder=CMD_SEARCH_HINT,
            required=False,
            max_length=100,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        query = str(self.query.value or "").strip()
        layout = self.layout
        if layout is None:
            return await interaction.response.send_message(
                "Search needs the directory it came from — run `/cmd search:"
                f"{query}` instead.",
                ephemeral=True,
            )
        page = (f"## 🔍 {query}", _search_body(layout.scoped, layout.viewer, query))
        options = _cmd_options(
            layout.scoped,
            extra=(CMD_SEARCH_ID, f"🔍 {query}"[:100], "your search"),
        )
        await send_panel(
            interaction,
            CmdLayout(
                pages={**layout.pages, CMD_SEARCH_ID: page},
                start=CMD_SEARCH_ID,
                options=options,
                scoped=layout.scoped,
                viewer=layout.viewer,
            ),
            ephemeral=True,
        )


class CmdSelectMenu(discord.ui.Select):
    """Open one system's page without leaving the card."""

    def __init__(self, options: list):
        super().__init__(
            placeholder="Open a system…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        layout = getattr(self, "view", None)
        pages = getattr(layout, "pages", {})
        title, body = pages.get(self.values[0]) or pages.get(CMD_ALL) or ("", "")
        if layout is None or not body:
            # A menu that has lost its card cannot draw a page; refusing quietly
            # would leave a dead control, so it says what to do instead.
            return await interaction.response.send_message(
                "That menu has no directory behind it any more — run `/cmd` again.",
                ephemeral=True,
            )
        heading = layout.find_item(CMD_TITLE_ID)
        text = layout.find_item(CMD_BODY_ID)
        if heading is not None and title:
            heading.content = title
        if text is not None:
            text.content = body
        await interaction.response.edit_message(view=layout)


class CmdSearchButton(discord.ui.Button):
    """The search modal's door on the card itself."""

    def __init__(self):
        super().__init__(label="Search", emoji="🔍", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            CmdSearchModal(getattr(self, "view", None))
        )


class CmdLayout(discord.ui.LayoutView):
    """The directory card: one page, a system menu, and the search door.

    Every page is built before the card is sent and stored on the view, so the
    menu swaps two strings rather than re-deciding anything — which is the same
    shape the ``/help`` panel uses and the reason both can edit a message in
    place without a second card being built that might disagree with the first.
    """

    def __init__(
        self,
        *,
        pages: dict,
        start: str,
        options: list,
        scoped,
        viewer,
        thumbnail: str | None = None,
    ):
        super().__init__(timeout=600)
        self.pages = pages
        self.scoped = scoped
        self.viewer = viewer
        title, body = pages.get(start, ("## DaCat Command Directory", ""))
        heading = discord.ui.TextDisplay(title, id=CMD_TITLE_ID)
        text = discord.ui.TextDisplay(body, id=CMD_BODY_ID)
        parts = []
        thumb = discord.ui.Thumbnail(thumbnail) if thumbnail else None
        if thumb is not None:
            parts.append(discord.ui.Section(heading, accessory=thumb))
        else:
            parts.append(heading)
        parts.append(text)
        # The menu and the search button are **two rows**, for the reason the help
        # panel learned at Discord's hand: a row holds one select or up to five
        # buttons, and a row holding both is refused with 50035 — which reaches the
        # member as "An unexpected error occurred" and nothing else.
        if options:
            menu_row = discord.ui.ActionRow()
            menu_row.add_item(CmdSelectMenu(options))
            parts.append(menu_row)
        search_row = discord.ui.ActionRow()
        search_row.add_item(CmdSearchButton())
        parts.append(search_row)
        parts.append(discord.ui.TextDisplay(CMD_FOOTER))
        self.add_item(discord.ui.Container(*parts, accent_color=ACCENT))


def build_cmd_card(bot, interaction, *, viewer, rows, scoped, search: str | None = None, system: str | None = None):
    """The card for one request, from the tables and nothing else.

    Split out of the command so the modal, the tests and the command itself build
    the *same* card: three callers that each composed their own would be three
    ways for the directory to say three different things about one server.
    """
    where = getattr(getattr(interaction, "guild", None), "name", "") or ""
    pages: dict[str, tuple[str, str]] = {}
    for group in directory.grouped(rows):
        page = _system_page(rows, scoped, viewer, group.id)
        if page is not None:
            pages[group.id] = page
    home = ("## DaCat Command Directory", _home_body(scoped, viewer, where=where))
    pages[CMD_ALL] = home

    start = CMD_ALL
    extra = None
    options = _cmd_options(scoped)
    if search:
        query = str(search).strip()
        pages[CMD_SEARCH_ID] = (f"## 🔍 {query}", _search_body(scoped, viewer, query))
        start = CMD_SEARCH_ID
        extra = (CMD_SEARCH_ID, f"🔍 {query}"[:100], "your search")
    elif system:
        page = _system_page(rows, scoped, viewer, system)
        if page is not None:
            pages[system] = page
            start = system

    return CmdLayout(
        pages=pages,
        start=start,
        options=_cmd_options(scoped, extra=extra),
        scoped=scoped,
        viewer=viewer,
        thumbnail=avatar_url(getattr(bot, "user", None)),
    )


async def _caller_owns_the_server(interaction) -> bool:
    """Whether this member passes the *own* audience's real check.

    Discord has no "is the owner" permission bit, so ``utils.permissions``
    declares the owner's commands as administrator and relies on the callback to
    refuse everybody else — which means the picker offers them to an administrator
    who is not the owner. The directory says what is true instead of what the
    picker says, because the check is one attribute and one cache lookup away.
    """
    guild = getattr(interaction, "guild", None)
    user = getattr(interaction, "user", None)
    if guild is not None and user is not None and getattr(guild, "owner_id", None) == user.id:
        return True
    try:
        return bool(await interaction.client.is_owner(user))
    except Exception:
        return False


async def cmd_viewer(interaction) -> "directory.Viewer":
    """Everything the two tables need about one member, gathered once."""
    user = getattr(interaction, "user", None)
    guild = getattr(interaction, "guild", None)
    return directory.Viewer(
        states=module_state.get_states(interaction.guild_id),
        permissions=getattr(user, "guild_permissions", None),
        is_owner=await _caller_owns_the_server(interaction),
        role_ids=frozenset(role.id for role in getattr(user, "roles", ()) or ()),
        guild_role_ids=frozenset(role.id for role in getattr(guild, "roles", ()) or ()),
    )


class Help(commands.Cog):
    """Interactive categorized command directory."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Explore all features, commands, and modules in DaCat.")
    @app_commands.describe(search="Search for a specific command, feature, or keyword")
    async def help_cmd(self, interaction: discord.Interaction, search: str | None = None):
        """The prose tour. ``/cmd`` is the index — see ``cmd_cmd`` below."""
        if search:
            query = search.strip().lower().lstrip("/")
            matches = []
            for mod_id, mod in HELP_MODULES.items():
                for line in mod["desc"].split("\n"):
                    line_clean = line.strip().lstrip("> ")
                    if line_clean.startswith("`/"):
                        if query in line_clean.lower():
                            matches.append((mod["title"], line_clean))
            if matches:
                embed = discord.Embed(
                    title=f"🔍 Help Search: '{search}'",
                    description=f"Found **{len(matches)}** matching command(s):\n\n" + "\n".join(
                        f"**{title}**\n> {cmd}" for title, cmd in matches[:10]
                    ),
                    color=ACCENT,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            else:
                embed = discord.Embed(
                    title="🔍 No Commands Found",
                    # The prose half answers nothing for a *name*, and `/cmd` is
                    # the half that does: it knows every command, including the
                    # ones this server has switched off or that the asker cannot
                    # run, and it says which of those it is (see ``cmd_cmd``).
                    description=(
                        f"No commands matching **{search}** were found in these "
                        f"categories. Try `/cmd search:{str(search).strip()[:80]}` "
                        "for every command this server can run."
                    ),
                    color=DANGER,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        thumb = avatar_url(interaction.client.user)
        banner = guild_banner(interaction.guild)
        link = dashboard_link(interaction)
        view = HelpLayout(
            thumbnail=thumb,
            gallery=[banner] if banner else None,
            dashboard=link,
        )
        # A panel carrying a sign-in link goes to the person who asked, in
        # private. That is a security property rather than a preference: the link
        # *is* a dashboard session for this server, and redeeming it proves only
        # that somebody holds the link — so a public message would hand a live
        # credential for this server's dashboard to everyone who can read the
        # channel, for the fifteen minutes the token lives, and the session it
        # buys lasts eight hours and acts with the administrator's own live
        # permissions (the dashboard resolves the actor from the token's
        # ``user_id``). Members who get no link still get the shared panel: they
        # have nothing to leak.
        await send_panel(interaction, view, ephemeral=bool(link))

    @app_commands.command(name="cmd", description=CMD_DESCRIPTION)
    @app_commands.describe(
        search="A command name, a word from what it does, or a system",
        system="Open one system in full",
    )
    @app_commands.choices(system=CMD_SYSTEM_CHOICES)
    async def cmd_cmd(
        self,
        interaction: discord.Interaction,
        search: str | None = None,
        system: str | None = None,
    ):
        """Every command this bot offers, scoped to the member asking.

        The request is answered in three readings of one card: the whole
        directory grouped by system, one system in full, or the matches for a
        word.
        All three are built by :func:`build_cmd_card` from the live command tree
        and the two tables in ``utils``, so a command retired tomorrow is gone
        from here tomorrow with no list to update — and a command added tomorrow
        is on the card the moment its cog loads.
        """
        if not interaction.guild_id:
            # Defence in depth rather than the rule: ``bot.global_module_check``
            # already refuses every command that is not DM-allowed, and this one
            # reads a guild's switches, a guild's roles and a member's permissions
            # *in* a guild — there is nothing to scope outside one.
            return await interaction.response.send_message(
                "The command directory is per server — run `/cmd` in your server.",
                ephemeral=True,
            )
        viewer = await cmd_viewer(interaction)
        rows = directory.commands(interaction.client.tree)
        scoped = directory.scope(rows, viewer)
        await send_panel(
            interaction,
            build_cmd_card(
                interaction.client,
                interaction,
                viewer=viewer,
                rows=rows,
                scoped=scoped,
                search=search,
                system=system,
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
