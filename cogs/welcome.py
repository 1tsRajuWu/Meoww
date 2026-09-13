import discord
from discord import app_commands
from discord.ext import commands
from utils import db
from utils.style import Panel, send_panel, SUCCESS, NIGHT, avatar_url, guild_banner

class Welcome(commands.Cog):
    """Welcome and goodbye messages."""

    welcome_group = app_commands.Group(name="welcome", description="Welcome & goodbye message settings")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "welcome"):
                await interaction.response.send_message(
                    "⚠️ The **Welcome** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True
                )
                return False
        return True

    @welcome_group.command(name="set", description="Set the channel for welcome messages.")
    @app_commands.describe(channel="The channel to welcome members in")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setwelcome(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.set_setting(interaction.guild.id, "welcome_channel", channel.id)
        await interaction.response.send_message(f"Welcome messages will now be sent to {channel.mention}.")

    @welcome_group.command(name="goodbye", description="Set the channel for goodbye messages.")
    @app_commands.describe(channel="The channel to say goodbye in")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setgoodbye(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.set_setting(interaction.guild.id, "goodbye_channel", channel.id)
        await interaction.response.send_message(f"Goodbye messages will now be sent to {channel.mention}.")

    @welcome_group.command(name="disable", description="Disable welcome messages in this server.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disablewelcome(self, interaction: discord.Interaction):
        await db.set_setting(interaction.guild.id, "welcome_channel", None)
        await interaction.response.send_message("✅ Welcome messages have been disabled for this bot.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(member.guild.id, "welcome"):
            return
        channel_id = await db.get_setting(member.guild.id, "welcome_channel")
        if not channel_id:
            return
        channel = member.guild.get_channel(int(channel_id))
        if not channel or not channel.permissions_for(member.guild.me).send_messages:
            return
        view = Panel(
            title=f"Welcome to {member.guild.name}",
            body=(
                f"Hey {member.mention} — you just walked in.\n\n"
                f"- You're member **#{member.guild.member_count:,}**\n"
                "- Say hi, grab roles, and settle in\n"
                "- Staff is around if you need anything"
            ),
            accent=SUCCESS,
            thumbnail=avatar_url(member),
            gallery=[guild_banner(member.guild)] if guild_banner(member.guild) else None,
            footer=f"{member.guild.name}  ·  glad you're here",
            timeout=None,
        )
        await send_panel(channel, view)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        channel_id = await db.get_setting(member.guild.id, "goodbye_channel")
        if not channel_id:
            return
        channel = member.guild.get_channel(channel_id)
        if not channel:
            return
        view = Panel(
            title="Someone just left",
            body=f"**{member}** is no longer in **{member.guild.name}**.",
            accent=NIGHT,
            thumbnail=avatar_url(member),
            footer=f"{member.guild.member_count:,} members remaining",
            timeout=None,
        )
        await send_panel(channel, view)

async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
