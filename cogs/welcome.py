import discord
from discord import app_commands
from discord.ext import commands
from utils import db

class Welcome(commands.Cog):
    """Welcome and goodbye messages."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setwelcome", description="Set the channel for welcome messages.")
    @app_commands.describe(channel="The channel to welcome members in")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setwelcome(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.set_setting(interaction.guild.id, "welcome_channel", channel.id)
        await interaction.response.send_message(f"Welcome messages will now be sent to {channel.mention}.")

    @app_commands.command(name="setgoodbye", description="Set the channel for goodbye messages.")
    @app_commands.describe(channel="The channel to say goodbye in")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setgoodbye(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.set_setting(interaction.guild.id, "goodbye_channel", channel.id)
        await interaction.response.send_message(f"Goodbye messages will now be sent to {channel.mention}.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        channel = None
        channel_id = await db.get_setting(member.guild.id, "welcome_channel")
        if channel_id:
            channel = member.guild.get_channel(int(channel_id))
        if not channel:
            for ch in member.guild.text_channels:
                if "welcome" in ch.name.lower():
                    channel = ch
                    break
        if not channel or not channel.permissions_for(member.guild.me).send_messages:
            return
        embed = discord.Embed(
            title="Welcome!",
            description=f"Welcome to **{member.guild.name}**, {member.mention}!\nWe're glad to have you.",
            color=discord.Color.green(),
        )
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Member #{member.guild.member_count}")
        await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        channel_id = await db.get_setting(member.guild.id, "goodbye_channel")
        if not channel_id:
            return
        channel = member.guild.get_channel(channel_id)
        if not channel:
            return
        embed = discord.Embed(
            description=f"**{member}** has left the server.",
            color=discord.Color.dark_grey(),
        )
        await channel.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
