import discord
from discord import app_commands
from discord.ext import commands
import re
from utils.style import send_card

PRESET_COLORS = [
    ("🌸 Sakura Pink", "#FFB7B2", "Soft pastel Japanese cherry blossom pink"),
    ("💖 Neon Magenta", "#FF007F", "High-energy cyberpunk vibrant magenta"),
    ("🔴 Crimson Red", "#FF3B30", "Bold, fiery crimson battle red"),
    ("🧡 Sunset Coral", "#FF6B6B", "Warm oceanic sunset coral"),
    ("🍯 Honey Gold", "#FFD166", "Rich radiant gold luxury hue"),
    ("💚 Emerald Mint", "#06D6A0", "Fresh crystalline emerald mint green"),
    ("💎 Cyber Cyan", "#00F0FF", "Electrifying sci-fi cyan blue"),
    ("🌌 Deep Ocean", "#118AB2", "Mystic deep twilight sea blue"),
    ("💜 Amethyst Purple", "#9D4EDD", "Regal glowing anime amethyst purple"),
    ("🖤 Midnight Charcoal", "#2B2D42", "Stealth dark luxury carbon tone"),
    ("🤍 Pure Platinum", "#E2E8F0", "Clean sleek celestial silver white"),
]


async def apply_member_color(interaction: discord.Interaction, hex_code: str | None, label: str):
    """Applies a color role to the member, replacing previous color roles."""
    member = interaction.user
    guild = interaction.guild

    # The palette dropdown is a View callback, which never passes through the
    # cog's interaction_check, so enforce the module toggle here as well.
    bot = interaction.client
    if interaction.guild_id and hasattr(bot, "is_module_enabled") and not bot.is_module_enabled(interaction.guild_id, "colors"):
        return await interaction.response.send_message(
            "⚠️ The **Color Studio** system is currently **turned off** on this server.", ephemeral=True
        )

    if not guild.me.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ The bot needs the **Manage Roles** permission to assign color roles.", ephemeral=True)

    # Remove all existing color roles from this member
    color_roles_to_remove = [r for r in member.roles if r.name.lower().startswith("color-")]
    if color_roles_to_remove:
        try:
            await member.remove_roles(*color_roles_to_remove, reason="Changing custom color role")
        except Exception as e:
            print(f"Could not remove old color roles: {e}")

    if hex_code is None:
        return await interaction.response.send_message("✅ Removed your custom color role!", ephemeral=True)

    # Format hex
    clean_hex = hex_code.strip("#").upper()
    role_name = f"color-#{clean_hex}"

    # Find or create role
    existing_role = discord.utils.get(guild.roles, name=role_name)
    color_obj = discord.Color(int(clean_hex, 16))

    if not existing_role:
        try:
            # Create role below bot's top role
            bot_top = guild.me.top_role
            pos = max(1, bot_top.position - 1)
            existing_role = await guild.create_role(
                name=role_name,
                color=color_obj,
                reason=f"Custom color role requested by {member}"
            )
            try:
                await existing_role.edit(position=pos)
            except Exception:
                pass
        except Exception as e:
            return await interaction.response.send_message(f"❌ Failed to create color role: {e}", ephemeral=True)

    # Assign role
    try:
        await member.add_roles(existing_role, reason="Custom color role assigned")
    except Exception as e:
        return await interaction.response.send_message(f"❌ Could not assign color role: {e}", ephemeral=True)

    embed = discord.Embed(
        title="🎨 Color Role Applied!",
        description=(
            f"Your username color has been set to **{label}**!\n\n"
            f"> 🏷️ **Role:** {existing_role.mention}\n"
            f"> 🎨 **Hex Code:** `#{clean_hex}`\n"
            f"> 👤 **Member:** {member.mention}"
        ),
        color=color_obj,
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="🐾 Server Color Studio • Use /color remove to reset", icon_url=member.display_avatar.url)

    if interaction.response.is_done():
        await send_card(interaction, embed=embed, ephemeral=True)
    else:
        await send_card(interaction, embed=embed, ephemeral=True)


class ColorSelectMenu(discord.ui.Select):
    def __init__(self):
        options = []
        for name, hex_val, desc in PRESET_COLORS:
            options.append(discord.SelectOption(label=name, value=hex_val, description=desc[:50]))
        options.append(discord.SelectOption(label="🗑️ Remove Current Color", value="REMOVE", description="Reset back to standard role color"))
        super().__init__(placeholder="🎨 Choose your name color from the studio palette...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        if val == "REMOVE":
            await apply_member_color(interaction, None, "Default")
        else:
            name_label = next((n for n, h, d in PRESET_COLORS if h == val), val)
            await apply_member_color(interaction, val, name_label)


class ColorPaletteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ColorSelectMenu())


class Colors(commands.Cog):
    """Vibrant custom color roles and palette picker."""

    color_group = app_commands.Group(name="color", description="Custom name color roles and palette studio")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "colors"):
                await interaction.response.send_message(
                    "⚠️ The **Color Studio** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True
                )
                return False
        return True

    @color_group.command(name="palette", description="Open the interactive color studio palette to pick a username color.")
    async def color_palette(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎨 Server Color Studio",
            description=(
                "Personalize your Discord profile with a custom aesthetic name color!\n\n"
                "### 🌈 Available Palette Choices:\n"
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )

        lines = []
        for name, hex_val, desc in PRESET_COLORS:
            lines.append(f"• **{name}** (`{hex_val}`) — *{desc}*")
        embed.description += "\n".join(lines) + "\n\n*Select a color from the dropdown below to apply it instantly!*"

        embed.set_footer(text="🐾 Or use /color custom #HEX to apply any custom color code!")
        view = ColorPaletteView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @color_group.command(name="custom", description="Set any custom HEX color code for your name (e.g. #FF007F).")
    @app_commands.describe(hex_code="6-character hex color code (e.g. #FF007F or 00F0FF)")
    async def color_custom(self, interaction: discord.Interaction, hex_code: str):
        clean = hex_code.strip("#").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{6}", clean):
            return await interaction.response.send_message("❌ Invalid hex code! Please enter a valid 6-character hex code, e.g. `#FF007F` or `#00F0FF`.", ephemeral=True)

        await apply_member_color(interaction, f"#{clean}", f"Custom #{clean.upper()}")

    @color_group.command(name="remove", description="Remove your custom color role and reset your name color.")
    async def color_remove(self, interaction: discord.Interaction):
        await apply_member_color(interaction, None, "Default")


async def setup(bot: commands.Bot):
    await bot.add_cog(Colors(bot))
