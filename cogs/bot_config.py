import discord
from discord import app_commands
from discord.ext import commands

#: The two seeded channel IDs below only exist in the home server. Writing them
#: into any other guild pointed its moderation log at a channel that isn't there
#: (so /modlog went silent) and its level-up card at a foreign channel.
HOME_GUILD_ID = 1542278896148422676
HOME_BOT_CMD_CHANNEL_ID = 1547637176697757846
HOME_LOG_CHANNEL_ID = 1546718348119773260

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

    @app_commands.command(name="sync", description="Publish any missing or out-of-date slash commands.")
    # Declared rather than hand-rolled: the guild owner is always an
    # administrator, so this is the same audience as the body check it replaces.
    @app_commands.checks.has_permissions(administrator=True)
    async def sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # Deliberately *not* `tree.sync()` / `copy_global_to` + `tree.sync(guild=)`.
        # Those are **bulk overwrites**, and Discord applies them destructively: the
        # scope's whole command list is cleared before the new one is sent. A run
        # that hits the daily write limit (error 30034) therefore loses every
        # command it had not re-sent yet — this guild went from 74 commands to one
        # exactly that way, and `/sync` was a second route to the same wipe. The
        # publisher upserts only what is missing or out of date, so it can never
        # take a command away.
        publisher = getattr(self.bot, "publish_commands", None)
        if publisher is None:
            return await interaction.followup.send(
                "❌ This bot build cannot publish commands. Restart the bot and try again."
            )
        try:
            results = await publisher(self.bot, force=True)
            lines = ["✅ **Slash commands published** — nothing was removed."]
            for name, count in results.items():
                label = "Global (every server)" if name == "(global)" else name
                lines.append(f"• {label}: **{count}** commands")
            lines.append(
                "\nHitting Discord's daily write limit is normal — the rest publish "
                "themselves in the background as it refills."
            )
            await interaction.followup.send("\n".join(lines))
        except Exception as e:
            await interaction.followup.send(f"❌ Sync failed: {e}")

    @app_commands.command(name="updatedb", description="Verify and update all bot database tables and default guild settings.")
    @app_commands.checks.has_permissions(administrator=True)
    async def updatedb(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            from utils import db
            await db.setup()
            notes = ["• All database tables verified and created."]
            if interaction.guild:
                guild = interaction.guild
                if guild.id == HOME_GUILD_ID:
                    await db.set_setting(guild.id, "bot_cmd_channel_id", HOME_BOT_CMD_CHANNEL_ID)
                    await db.set_setting(guild.id, "log_channel_id", HOME_LOG_CHANNEL_ID)
                    notes.append(
                        "• Command channel set to `💬・bot-cmd` and moderation logs to `logs`."
                    )
                else:
                    logs = discord.utils.get(guild.text_channels, name="logs")
                    if logs:
                        await db.set_setting(guild.id, "log_channel_id", logs.id)
                        notes.append(f"• Moderation logs bound to {logs.mention}.")
                    else:
                        notes.append(
                            "• No `#logs` channel found here, so the log channel was left "
                            "alone — set one with `/modlog`."
                        )
                    cmd_channel = (
                        discord.utils.get(guild.text_channels, name="bot-cmd")
                        or discord.utils.get(guild.text_channels, name="💬・bot-cmd")
                    )
                    if cmd_channel:
                        await db.set_setting(guild.id, "bot_cmd_channel_id", cmd_channel.id)
                        notes.append(f"• Level-up cards will post in {cmd_channel.mention}.")
                    else:
                        notes.append(
                            "• No `#bot-cmd` channel found here, so level-up cards stay "
                            "off until one exists."
                        )
            notes.append("• Economy and leveling tables ready.")
            await interaction.followup.send(
                "✅ **Database Successfully Updated!**\n" + "\n".join(notes)
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Database update failed: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(BotConfig(bot))
