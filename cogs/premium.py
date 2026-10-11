"""DaCat VIP & Premium features, status and invites."""
import discord
from discord import app_commands
from discord.ext import commands

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
]


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
        description="DaCat VIP & Premium edition perks, status, and free invite!",
    )

    @premium.command(
        name="status",
        description="Check your server's VIP tier status and active bot instances.",
    )
    async def premium_status(self, interaction: discord.Interaction):
        is_vip = is_vip_context(interaction)
        embed = make_vip_embed(
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
            thumbnail_url=VIP_GEM_GIF,
            author_icon=VIP_CROWN_GIF,
            footer_icon=VIP_SPARKLE_GIF,
        )
        await send_card(interaction, embed=embed, view=PremiumInviteView())

    @premium.command(
        name="perks",
        description="Explore the complete list of DaCat VIP & Premium features.",
    )
    async def premium_perks(self, interaction: discord.Interaction):
        fields = [(name, desc, False) for name, desc in PREMIUM_PERKS]
        embed = make_vip_embed(
            title="DaCat VIP Feature Showcase",
            description=(
                "Everything included with the **DaCat VIP Edition** (`1558691012975534222`).\n"
                "All features are unlocked with zero billing during our early-access preview."
            ),
            fields=fields,
            thumbnail_url=VIP_GEM_GIF,
            author_icon=VIP_CROWN_GIF,
            footer_icon=VIP_SPARKLE_GIF,
        )
        await send_card(interaction, embed=embed, view=PremiumInviteView())

    @premium.command(
        name="invite",
        description="Get the official 1-click invite link to add the VIP Bot to your server.",
    )
    async def premium_invite(self, interaction: discord.Interaction):
        embed = make_vip_embed(
            title="Invite DaCat VIP • Client ID: 1558691012975534222",
            description=(
                "Invite the dedicated **DaCat VIP Bot** to run side-by-side with your server!\n\n"
                "• **Client ID:** `1558691012975534222`\n"
                "• **Permissions:** Administrator (recommended for full security & channel management)\n"
                "• **Cost:** **$0.00** (Free early-access promotional period)\n\n"
                "Click the button below to authorize the bot on Discord in seconds!"
            ),
            image_url=VIP_BANNER_GIF or None,
            fields=[
                ("VIP Bot (1558691012975534222)", f"[Direct Authorization Link]({VIP_INVITE_URL})", True),
                ("Main Bot (1548380269776605324)", f"[Direct Authorization Link]({MAIN_INVITE_URL})", True),
            ],
            thumbnail_url=VIP_GEM_GIF,
            author_icon=VIP_CROWN_GIF,
            footer_icon=VIP_SPARKLE_GIF,
        )
        await send_card(interaction, embed=embed, view=PremiumInviteView())


async def setup(bot: commands.Bot):
    await bot.add_cog(Premium(bot))
