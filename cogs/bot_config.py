import discord
from discord import app_commands
from discord.ext import commands
import aiohttp

class BotConfig(commands.Cog):
    """Commands to configure the bot's own profile."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setbanner", description="Set the bot's profile banner (Owner only).")
    @app_commands.describe(image="The image file to use as the banner")
    @app_commands.checks.has_permissions(administrator=True)
    async def setbanner(self, interaction: discord.Interaction, image: discord.Attachment):
        if not interaction.user.id == interaction.guild.owner_id:
            return await interaction.response.send_message("Only the server owner can change the bot's banner.", ephemeral=True)
            
        if not image.content_type or not image.content_type.startswith("image/"):
            return await interaction.response.send_message("Please upload a valid image file.", ephemeral=True)
            
        await interaction.response.defer(ephemeral=True)
        try:
            image_bytes = await image.read()
            await self.bot.user.edit(banner=image_bytes)
            await interaction.followup.send("Banner updated successfully! (It may take a moment to cache in Discord)")
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to update banner: {e.text}")
        except Exception as e:
            await interaction.followup.send(f"An unexpected error occurred: {e}")

    @app_commands.command(name="setavatar", description="Set the bot's profile picture (Owner only).")
    @app_commands.describe(image="The image file to use as the avatar")
    @app_commands.checks.has_permissions(administrator=True)
    async def setavatar(self, interaction: discord.Interaction, image: discord.Attachment):
        if not interaction.user.id == interaction.guild.owner_id:
            return await interaction.response.send_message("Only the server owner can change the bot's avatar.", ephemeral=True)
            
        if not image.content_type or not image.content_type.startswith("image/"):
            return await interaction.response.send_message("Please upload a valid image file.", ephemeral=True)
            
        await interaction.response.defer(ephemeral=True)
        try:
            image_bytes = await image.read()
            await self.bot.user.edit(avatar=image_bytes)
            await interaction.followup.send("Avatar updated successfully! (It may take a moment to cache in Discord)")
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to update avatar: {e.text}")
        except Exception as e:
            await interaction.followup.send(f"An unexpected error occurred: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(BotConfig(bot))
