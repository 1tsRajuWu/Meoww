import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from utils import db

DB_PATH = db.DB_PATH


class TempVoice(commands.Cog):
    """Dynamic Join-to-Create temporary voice channels that auto-clean when empty."""

    tempvoice_group = app_commands.Group(name="tempvoice", description="Join-to-create temporary voice channel controls")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        if not guild:
            return
        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(guild.id, "tempvoice"):
            return

        # 1. User joins the generator channel
        if after.channel:
            async with aiosqlite.connect(DB_PATH) as conn:
                async with conn.execute(
                    "SELECT category_id FROM tempvoice_settings WHERE guild_id=? AND generator_channel_id=?",
                    (guild.id, after.channel.id)
                ) as cur:
                    row = await cur.fetchone()

            if row:
                category_id = row[0]
                category = guild.get_channel(category_id) if category_id else after.channel.category

                # Setup permissions: Channel owner has manage channel permissions
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(connect=True, speak=True),
                    member: discord.PermissionOverwrite(manage_channels=True, move_members=True, connect=True, speak=True)
                }

                ch_name = f"🔊 {member.display_name}'s Lounge"
                try:
                    new_vc = await guild.create_voice_channel(
                        name=ch_name,
                        category=category,
                        overwrites=overwrites,
                        bitrate=min(after.channel.bitrate, 128000),
                        reason=f"TempVoice created for {member}"
                    )
                    # Move member into their new channel
                    await member.move_to(new_vc)

                    # Save channel to database
                    async with aiosqlite.connect(DB_PATH) as conn:
                        await conn.execute(
                            "INSERT INTO tempvoice_channels (channel_id, owner_id, guild_id) VALUES (?, ?, ?)",
                            (new_vc.id, member.id, guild.id)
                        )
                        await conn.commit()
                except Exception as e:
                    print(f"Error creating temp voice channel: {e}")

        # 2. User leaves a voice channel: check if it is an empty temp channel
        if before.channel and before.channel != (after.channel if after else None):
            left_ch = before.channel
            async with aiosqlite.connect(DB_PATH) as conn:
                async with conn.execute("SELECT 1 FROM tempvoice_channels WHERE channel_id=?", (left_ch.id,)) as cur:
                    is_temp = await cur.fetchone()

            if is_temp:
                # If channel is now empty, delete it
                if len(left_ch.members) == 0:
                    try:
                        await left_ch.delete(reason="TempVoice channel empty")
                        async with aiosqlite.connect(DB_PATH) as conn:
                            await conn.execute("DELETE FROM tempvoice_channels WHERE channel_id=?", (left_ch.id,))
                            await conn.commit()
                    except Exception as e:
                        print(f"Error deleting empty temp channel: {e}")

    @tempvoice_group.command(name="setup", description="Configure the Join-to-Create voice generator.")
    @app_commands.describe(category="The category where temporary voice channels will be created")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def tempvoice_setup(self, interaction: discord.Interaction, category: discord.CategoryChannel = None):
        guild = interaction.guild
        target_cat = category or interaction.channel.category

        # Create the generator channel
        try:
            generator = await guild.create_voice_channel(
                name="➕ Join to Create",
                category=target_cat,
                reason="TempVoice generator channel created"
            )
        except Exception as e:
            return await interaction.response.send_message(f"❌ Failed to create generator channel: {e}", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                """INSERT OR REPLACE INTO tempvoice_settings (guild_id, generator_channel_id, category_id)
                   VALUES (?, ?, ?)""",
                (guild.id, generator.id, target_cat.id if target_cat else None)
            )
            await conn.commit()

        embed = discord.Embed(
            title="🔊 TempVoice System Configured!",
            description=(
                f"The Join-to-Create voice system is now fully active!\n\n"
                f"> ➕ **Generator Channel:** {generator.mention}\n"
                f"> 📁 **Category:** **{target_cat.name if target_cat else 'None'}**\n\n"
                f"*When members click {generator.mention}, a private custom voice channel is created for them automatically, and deleted as soon as everyone leaves!*"
            ),
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Server Voice Hub • Powered by Meow")
        await interaction.response.send_message(embed=embed)

    async def _get_member_temp_channel(self, member: discord.Member) -> discord.VoiceChannel | None:
        if not member.voice or not member.voice.channel:
            return None
        ch = member.voice.channel
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute("SELECT owner_id FROM tempvoice_channels WHERE channel_id=?", (ch.id,)) as cur:
                row = await cur.fetchone()
                if row and (row[0] == member.id or member.guild_permissions.manage_channels):
                    return ch
        return None

    @tempvoice_group.command(name="lock", description="Lock your temporary voice channel so others cannot join.")
    async def tempvoice_lock(self, interaction: discord.Interaction):
        ch = await self._get_member_temp_channel(interaction.user)
        if not ch:
            return await interaction.response.send_message("❌ You must be inside your own temporary voice channel to use this command.", ephemeral=True)

        try:
            await ch.set_permissions(interaction.guild.default_role, connect=False)
            await interaction.response.send_message("🔒 Your voice channel is now **Locked**! Only invited users can join.")
        except Exception as e:
            await interaction.response.send_message(f"❌ Error locking channel: {e}", ephemeral=True)

    @tempvoice_group.command(name="unlock", description="Unlock your temporary voice channel so anyone can join.")
    async def tempvoice_unlock(self, interaction: discord.Interaction):
        ch = await self._get_member_temp_channel(interaction.user)
        if not ch:
            return await interaction.response.send_message("❌ You must be inside your own temporary voice channel to use this command.", ephemeral=True)

        try:
            await ch.set_permissions(interaction.guild.default_role, connect=True)
            await interaction.response.send_message("🔓 Your voice channel is now **Unlocked**! Anyone can join.")
        except Exception as e:
            await interaction.response.send_message(f"❌ Error unlocking channel: {e}", ephemeral=True)

    @tempvoice_group.command(name="limit", description="Set a maximum member limit for your temporary voice room.")
    @app_commands.describe(limit="Maximum capacity from 0 (unlimited) to 99")
    async def tempvoice_limit(self, interaction: discord.Interaction, limit: app_commands.Range[int, 0, 99]):
        ch = await self._get_member_temp_channel(interaction.user)
        if not ch:
            return await interaction.response.send_message("❌ You must be inside your own temporary voice channel to use this command.", ephemeral=True)

        try:
            await ch.edit(user_limit=limit)
            cap_text = f"**{limit} members**" if limit > 0 else "**Unlimited**"
            await interaction.response.send_message(f"👥 Channel member capacity set to {cap_text}!")
        except Exception as e:
            await interaction.response.send_message(f"❌ Error setting limit: {e}", ephemeral=True)

    @tempvoice_group.command(name="rename", description="Rename your temporary voice channel.")
    @app_commands.describe(new_name="New title for your voice channel")
    async def tempvoice_rename(self, interaction: discord.Interaction, new_name: str):
        ch = await self._get_member_temp_channel(interaction.user)
        if not ch:
            return await interaction.response.send_message("❌ You must be inside your own temporary voice channel to use this command.", ephemeral=True)

        clean_name = new_name.strip()[:32]
        try:
            await ch.edit(name=f"🔊 {clean_name}")
            await interaction.response.send_message(f"✏️ Channel renamed to **🔊 {clean_name}**!")
        except Exception as e:
            await interaction.response.send_message(f"❌ Error renaming channel: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoice(bot))
