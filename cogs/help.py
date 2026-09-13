import discord
from discord import app_commands
from discord.ext import commands
from utils import features as feature_registry
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
        "title": "🐾 Safari Pets & Social Roleplay",
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
        "emoji": "🐾"
    },
    "economy": {
        "title": "💰 Economy & Currency",
        "desc": (
            "Full community banking, daily rewards, work jobs, and trading currency.\n\n"
            "> `/balance [member]` — Check wallet & bank balance\n"
            "> `/daily` — Claim 24-hour daily coin reward\n"
            "> `/weekly` — Claim 7-day weekly bonus\n"
            "> `/work` — Work shifts across various jobs for coins\n"
            "> `/pay <member> <amount>` — Transfer coins to another member\n"
            "> `/rob <member>` — Attempt a daring coin heist\n"
            "> `/deposit <amount>` — Deposit wallet coins into your bank\n"
            "> `/withdraw <amount>` — Withdraw bank coins into your wallet\n"
            "> `/richlist` — Server wealth leaderboard\n"
            "> `/eco-admin add / remove / set / reset` — Admin currency management"
        ),
        "color": 0x2ECC71,
        "emoji": "💰"
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
            "> `/levelroles` — View all configured level role milestones\n"
            "> `/addlevelrole <level> <role>` — Assign a role reward for hitting a level\n"
            "> `/removelevelrole <level>` — Delete a level role reward\n"
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
            "> `/kms` — Relatable funny life checks"
        ),
        "color": 0xE74C3C,
        "emoji": "😆"
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
            "> `/sync` — Force re-sync all slash commands directly to this server\n"
            "> `/updatedb` — Verify all database tables and default channels\n"
            "> `/privacy` & `/tos` — Bot privacy policy and terms of service"
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
    "- 🎫 **Tickets** — private staff support"
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


class HelpLayout(discord.ui.LayoutView):
    def __init__(self, *, thumbnail: str | None, gallery: list[str] | None = None):
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
        parts.append(row)
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
        view = HelpLayout(thumbnail=thumb, gallery=[banner] if banner else None)
        await send_panel(interaction, view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
