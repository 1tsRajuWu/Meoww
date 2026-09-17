import discord
from discord import app_commands
from discord.ext import commands
from utils import bot_channels, features as feature_registry
from utils.style import send_panel, ACCENT, avatar_url, guild_banner

HELP_MODULES = {
    "moderation": {
        "title": "🔨 Moderation & Security",
        "desc": (
            "Complete server protection, disciplinary actions, and logging tools.\n\n"
            "> `/warn <member> <reason>` — Issue an official warning\n"
            "> `/warnings <member>` — View warning history for a user\n"
            "> `/clearwarnings <member>` — Clear all warnings for a user\n"
            "> `/mute <member> <duration> [reason]` — Timeout/mute a member\n"
            "> `/unmute <member>` — Remove timeout\n"
            "> `/kick <member> [reason]` — Kick a member from the server\n"
            "> `/ban <member> [delete_days] [reason]` — Ban a member\n"
            "> `/unban <user_id>` — Unban a user\n"
            "> `/purge <amount> [member]` — Bulk delete messages\n"
            "> `/modlog <channel>` — Set the moderation audit log channel"
        ),
        "color": 0xED4245,
        "emoji": "🔨"
    },
    "automod": {
        "title": "🛡️ Auto-Moderation",
        "desc": (
            "Automatic server defenses against spam and unauthorized invites.\n\n"
            "> `/automod invites <enabled>` — Toggle blocking of Discord invite links\n"
            "> `/automod antispam <enabled>` — Toggle rapid-message flood protection"
        ),
        "color": 0xE67E22,
        "emoji": "🛡️"
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
    "economy": {
        "title": "💰 Economy & Currency",
        "desc": (
            "Full community banking, daily rewards, work jobs, and trading currency. "
            "Moving coins between members carries a small **Meow Fee** that lands in "
            "Meow Bank, and an admin can switch it off.\n\n"
            "> `/balance [member]` — Check wallet & bank balance\n"
            "> `/daily` — Claim 24-hour daily coin reward\n"
            "> `/weekly` — Claim 7-day weekly bonus\n"
            "> `/work` — Work shifts across various jobs for coins\n"
            "> `/pay <member> <amount>` — Transfer coins (a small Meow Fee goes to Meow Bank)\n"
            "> `/rob <member>` — Attempt a daring coin heist (the bank takes its cut of the loot)\n"
            "> `/deposit <amount>` — Deposit wallet coins into your bank\n"
            "> `/withdraw <amount>` — Withdraw bank coins into your wallet\n"
            "> `/richlist` — Server wealth leaderboard\n"
            "> `/loan [amount] [plan]` — Borrow 🪙 from Meow Bank; your credit line grows with your level\n"
            "> `/repay [amount]` — Pay a loan back in full or in part, from wallet or bank\n"
            "> `/beg` — Beg for spare coins (2-minute cooldown, no dignity required)\n"
            "> `/eco-admin add / remove / set / reset` — Admin currency management\n"
            "> `/eco-admin fee` — See the Meow Fee and Meow Bank's balance, or turn the fee off (admin)"
        ),
        "color": 0x2ECC71,
        "emoji": "💰"
    },
    "market": {
        "title": "📈 Meow Markets",
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
            "the 0.25% Meow Treat fee — at least 1 coin, itemised on every "
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
            "> `/lockdown` & `/unlock` — Emergency channel lockdown controls\n"
            "> `/slowmode <seconds>` — Adjust channel chat cooldown\n"
            "> `/bot sync` — Force re-sync all slash commands directly to this server\n"
            "> `/bot db` — Verify all database tables and default channels\n"
            "> `/bot bug` — Report a bug in the bot itself (administrators)\n"
            "> `/legal privacy` & `/legal tos` — Bot privacy policy and terms of service"
        ),
        "color": 0x95A5A6,
        "emoji": "⚙️"
    }
}




HELP_HOME_BODY = (
    "Your all-in-one command center. Pick a category below to open its full command list.\n\n"
    "- 🔨 **Moderation** — warns, bans, timeouts, logs\n"
    "- 🛡️ **Automod** — invite & flood protection\n"
    "- ⭐ **Starboard** — community highlights\n"
    "- 💰 **Economy & Games** — bank, casino, minigames\n"
    "- ⚡ **Leveling** — XP, ranks, reward roles\n"
    "- 🐾 **Social & Fun** — pets, roleplay, memes, quotes\n"
    "- 🎵 **Music & Radio** — playback, 24/7 radio, live filters\n"
    "- 🎫 **Tickets** — private staff support\n"
    "- 📊 **Activity Tracker** — per-channel msg & voice stats"
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
            title.content = f"## {data['title']}"
        if body:
            body.content = data["desc"]
        for child in self.view.children:
            if isinstance(child, discord.ui.Container):
                child.accent_color = data["color"]
        await interaction.response.edit_message(view=self.view)


BUG_REPORT_HINT = (
    "What happened, and what you expected instead? The command you ran, the "
    "steps to reproduce it, and any error text all help."
)


class BugReportModal(discord.ui.Modal, title="Report a bug"):
    """The free-text half of a report; the destination is resolved on submit."""

    def __init__(self, channel_ids: list[int], source: str = "the /help panel"):
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
        self.add_item(self.what_happened)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        report = str(self.what_happened.value or "").strip()
        embed = discord.Embed(
            title="🐞 Bug report",
            description=report[:4000] or "*(no description given)*",
            color=0xE74C3C,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Reported by", value=interaction.user.mention, inline=True)
        embed.add_field(
            name="Server",
            value=guild.name if guild is not None else "Direct message",
            inline=True,
        )
        embed.add_field(name="Filed via", value=self.source, inline=True)
        embed.set_footer(text=f"User ID {interaction.user.id}")

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
            follow_up = await _open_report_thread(message, report, interaction)
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


async def _open_report_thread(message, report: str, interaction) -> str:
    """A thread on the report, or ``""`` if this server will not have one."""
    try:
        first_line = (report.splitlines() or [""])[0].strip()
        thread = await message.create_thread(
            name=f"🐞 {first_line[:80] or interaction.user.name}",
            auto_archive_duration=1440,
        )
        return f" Follow it in {thread.mention}."
    except Exception as e:
        print(f"[BUG] Could not open a thread on the report: {e}", flush=True)
        return ""


async def open_bug_report(interaction: discord.Interaction, source: str = "the /help panel"):
    """Open the report modal, or say plainly why nothing can be filed.

    One entry point for the panel button and for ``/bot bug`` alike: the
    destinations are resolved by the same call either time, which is the only
    reason a report sent from either surface ends up in the same place.
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
    await interaction.response.send_modal(BugReportModal(targets, source=source))


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
        heading = discord.ui.TextDisplay("## Meow Command Center", id=TITLE_ID)
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
        row = discord.ui.ActionRow()
        row.add_item(HelpSelectMenu())
        row.add_item(BugReportButton())
        parts.append(row)
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
        parts.append(discord.ui.TextDisplay("-# Tap a category anytime  ·  Meow"))
        self.add_item(discord.ui.Container(*parts, accent_color=ACCENT))


class Help(commands.Cog):
    """Interactive categorized command directory."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Explore all features, commands, and modules in Meow.")
    async def help_cmd(self, interaction: discord.Interaction):
        thumb = avatar_url(interaction.client.user)
        banner = guild_banner(interaction.guild)
        view = HelpLayout(
            thumbnail=thumb,
            gallery=[banner] if banner else None,
            dashboard=dashboard_link(interaction),
        )
        await send_panel(interaction, view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
