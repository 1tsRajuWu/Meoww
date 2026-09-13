import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from utils import db

class ReactionRoles(commands.Cog):
    """React to a message to get/remove a role."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "moderation"):
                await interaction.response.send_message(
                    "⚠️ The **Moderation & Roles** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True
                )
                return False
        return True

    @app_commands.command(name="reactionrole-add", description="Link an emoji reaction on a message to a role.")
    @app_commands.describe(message_id="The ID of the message to watch", emoji="The emoji to react with", role="The role to give")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reactionrole_add(self, interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
        try:
            channel = interaction.channel
            message = await channel.fetch_message(int(message_id))
        except (discord.NotFound, ValueError):
            return await interaction.response.send_message("Couldn't find that message in this channel.", ephemeral=True)

        await message.add_reaction(emoji)
        
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute("INSERT OR REPLACE INTO reaction_roles (message_id, emoji, role_id) VALUES (?, ?, ?)", 
                             (int(message_id), emoji, role.id))
            await conn.commit()
            
        await interaction.response.send_message(
            f"Reacting with {emoji} on that message now gives **{role.name}**.", ephemeral=True
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.member and payload.member.bot:
            return
            
        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute("SELECT role_id FROM reaction_roles WHERE message_id = ? AND emoji = ?", 
                                  (payload.message_id, str(payload.emoji))) as cursor:
                row = await cursor.fetchone()
                
        if not row:
            return
            
        role_id = row[0]
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        role = guild.get_role(role_id)
        member = guild.get_member(payload.user_id)
        if role and member:
            await member.add_roles(role)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute("SELECT role_id FROM reaction_roles WHERE message_id = ? AND emoji = ?", 
                                  (payload.message_id, str(payload.emoji))) as cursor:
                row = await cursor.fetchone()
                
        if not row:
            return
            
        role_id = row[0]
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        role = guild.get_role(role_id)
        member = guild.get_member(payload.user_id)
        if role and member:
            await member.remove_roles(role)

async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
