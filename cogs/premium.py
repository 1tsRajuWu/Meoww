"""DaCat VIP & Premium features, status and invites."""
import discord
from discord import app_commands
from discord.ext import commands

from utils import games
from utils.style import (
    send_card,
    make_vip_embed,
    VIP_BOT_ID,
    VIP_CROWN_GIF,
    VIP_GEM_GIF,
    VIP_SPARKLE_GIF,
    VIP_BANNER_GIF,
    VIP_INVITE_URL,
    MAIN_INVITE_URL,
    is_vip_context,
)

PREMIUM_PERKS = [
    ("🎵 Spotify Music Engine (/music)", "Rich Spotify track cards, server song library, listen counters, and interactive collaborative Jam sessions."),
    ("💍 Relationship Bonds (/bond)", "Exclusive relationship bonds for dating (bf/gf), besties, twins, siblings, homeboys/homegirls, crushes & rivals with auto-assigned custom roles."),
    ("🎮 100% Free & Ad-Free Games Engine (/game)", "Instant verified zero-ad game finder for web and PC: Minecraft, DOOM, Pokémon, retro, puzzles, horror, and weekly free commercial games."),
]


def _brand_embed(interaction: discord.Interaction, *, title: str, description: str,
                 fields=None, thumbnail: bool = False, banner: bool = False) -> discord.Embed:
    """Gold and animated on the VIP instance, plain everywhere else.

    The motion set (crown author, gem thumbnail, sparkle footer, crown banner)
    belongs to the premium bot only — the main bot answers the same commands
    with the same words, minus the show.
    """
    if is_vip_context(interaction):
        return make_vip_embed(
            title=title,
            description=description,
            fields=fields,
            thumbnail_url=VIP_GEM_GIF if thumbnail else None,
            image_url=VIP_BANNER_GIF if banner else None,
            author_icon=VIP_CROWN_GIF,
            footer_icon=VIP_SPARKLE_GIF,
        )
    embed = discord.Embed(
        title=title,
        description=description,
        color=0x5865F2,
        timestamp=discord.utils.utcnow(),
    )
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="DaCat • Free Early-Access Preview")
    return embed


class PremiumInviteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="💎 Add VIP Bot (1558691012975534222)",
                url=VIP_INVITE_URL,
                style=discord.ButtonStyle.link,
                emoji="👑",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="🌐 View Web Pricing & Perks",
                url="https://dacat.pages.dev/pricing",
                style=discord.ButtonStyle.link,
                emoji="✨",
            )
        )


class GameLinksView(discord.ui.View):
    """Interactive link buttons for verified free and ad-free games."""

    def __init__(self, game_info: games.GameInfo):
        super().__init__(timeout=None)
        # Primary verified destination
        self.add_item(
            discord.ui.Button(
                label=game_info.primary_label[:80],
                url=game_info.primary_url,
                style=discord.ButtonStyle.link,
            )
        )
        # Secondary verified destination if available
        if game_info.secondary_url and game_info.secondary_label:
            self.add_item(
                discord.ui.Button(
                    label=game_info.secondary_label[:80],
                    url=game_info.secondary_url,
                    style=discord.ButtonStyle.link,
                )
            )
        # Epic Games Weekly Free Commercial Games
        self.add_item(
            discord.ui.Button(
                label="🎁 Epic Weekly Freebies",
                url="https://store.epicgames.com/free-games",
                style=discord.ButtonStyle.link,
            )
        )


class Premium(commands.Cog):
    """VIP Premium Bot edition, perks, and status."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # URL buttons die with the process otherwise; re-register so old
        # invite cards keep working after a restart.
        if bot is not None and hasattr(bot, "add_view"):
            bot.add_view(PremiumInviteView())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and self.bot is not None and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "premium"):
                await interaction.response.send_message(
                    "⚠️ The **VIP Premium** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                    ephemeral=True,
                )
                return False
        return True

    premium = app_commands.Group(
        name="premium",
        description="👑 DaCat VIP & Premium edition perks, status, and free invite!",
    )

    @premium.command(
        name="status",
        description="Check your server's VIP tier status and active bot instances.",
    )
    async def premium_status(self, interaction: discord.Interaction):
        is_vip = is_vip_context(interaction)
        embed = _brand_embed(
            interaction,
            title="DaCat VIP Status • Complimentary Preview",
            description=(
                "**Good news!** Premium features are currently in **Free Early-Access Preview**!\n"
                "There are **no charges** right now — any server can invite the VIP Bot and "
                "enjoy 100% of the premium features without paying a single cent."
            ),
            fields=[
                ("Current Tier", "💎 **VIP Early-Access (Complimentary Preview)**", True),
                ("Current Price", "**$0.00 / free** (no payment required)", True),
                ("Active Instance", f"`{'👑 VIP Bot (' + str(VIP_BOT_ID) + ')' if is_vip else '🐾 Main Bot (1548380269776605324)'}`", True),
                (
                    "Connected Premium Features",
                    "• 🎵 **Spotify Music Engine** (`/music add`, `/music jam`, `/music top`): **Active (VIP)**\n"
                    "• 💍 **Relationship Bonds** (`/bond propose`, `/bond list`, `/bond break`): **Active (VIP)**\n"
                    "• 🎮 **100% Free & Ad-Free Games** (`/game`): **Active (VIP)**\n"
                    "• 👑 **100x Enhanced Visuals & VIP Cards**: **Active (VIP)**",
                    False,
                ),
                (
                    "📜 Terms & Privacy",
                    "• Terms of Service: `/legal tos` · [Read Online](https://dacat.pages.dev/terms)\n"
                    "• Privacy Policy: `/legal privacy` · [Read Online](https://dacat.pages.dev/privacy)\n"
                    "• Zero financial credentials collected · Compliant with Discord Developer Policy",
                    False,
                ),
            ],
            thumbnail=True,
        )
        await send_card(interaction, embed=embed, view=PremiumInviteView())

    @premium.command(
        name="perks",
        description="Explore the complete list of DaCat VIP & Premium features.",
    )
    async def premium_perks(self, interaction: discord.Interaction):
        fields = [(name, desc, False) for name, desc in PREMIUM_PERKS]
        embed = _brand_embed(
            interaction,
            title="DaCat VIP Feature Showcase",
            description=(
                "Everything included with the **DaCat VIP Edition** (`1558691012975534222`).\n"
                "All features are unlocked with zero billing during our early-access preview."
            ),
            fields=fields,
            thumbnail=True,
        )
        await send_card(interaction, embed=embed, view=PremiumInviteView())

    @premium.command(
        name="invite",
        description="Get the official 1-click invite link to add the VIP Bot to your server.",
    )
    async def premium_invite(self, interaction: discord.Interaction):
        embed = _brand_embed(
            interaction,
            title="Invite DaCat VIP • Client ID: 1558691012975534222",
            description=(
                "Invite the dedicated **DaCat VIP Bot** to run side-by-side with your server!\n\n"
                "• **Client ID:** `1558691012975534222`\n"
                "• **Permissions:** Administrator (recommended for full security & channel management)\n"
                "• **Cost:** **$0.00** (Free early-access promotional period)\n\n"
                "Click the button below to authorize the bot on Discord in seconds!"
            ),
            banner=True,
            fields=[
                ("VIP Bot (1558691012975534222)", f"[Direct Authorization Link]({VIP_INVITE_URL})", True),
                ("Main Bot (1548380269776605324)", f"[Direct Authorization Link]({MAIN_INVITE_URL})", True),
            ],
            thumbnail=True,
        )
        await send_card(interaction, embed=embed, view=PremiumInviteView())

    @app_commands.command(
        name="game",
        description="🎮 Find 100% free & ad-free games by name, genre, or type (VIP Exclusive)!",
    )
    @app_commands.describe(
        query="Game name, genre, or type (e.g. Minecraft, Doom, Pokemon, Chess, horror, retro, puzzle, rpg)",
    )
    async def game(self, interaction: discord.Interaction, query: str):
        game_info = games.lookup_game(query)

        fields = [
            ("Category & Platform", f"📂 **{game_info.category}** · 💻 {game_info.platform_type}", False),
            ("🛡️ Ad-Free Guarantee", f"✅ **{game_info.ad_free_guarantee}**", False),
        ]

        sources_text = [f"• [{game_info.primary_label}]({game_info.primary_url})"]
        if game_info.secondary_url and game_info.secondary_label:
            sources_text.append(f"• [{game_info.secondary_label}]({game_info.secondary_url})")
        for label, url in game_info.extra_sources:
            sources_text.append(f"• [{label}]({url})")

        fields.append(("🌐 Verified Ad-Free Destination(s)", "\n".join(sources_text), False))

        embed = _brand_embed(
            interaction,
            title=f"🎮 {game_info.title}",
            description=(
                f"{game_info.description}\n\n"
                "**Zero Ads · Zero Popups · Safe & Legal**\n"
                "Play or download completely free without advertisements or intrusive paywalls!"
            ),
            fields=fields,
            thumbnail=True,
        )
        view = GameLinksView(game_info)
        await send_card(interaction, embed=embed, view=view)

    @game.autocomplete("query")
    async def game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        choices = games.popular_suggestions(current)
        return [
            app_commands.Choice(name=c[:100], value=c.split(" (")[0][:100])
            for c in choices[:25]
        ]


async def setup(bot: commands.Bot):
    await bot.add_cog(Premium(bot))
