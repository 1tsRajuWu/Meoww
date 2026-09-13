import discord
from discord import app_commands
from discord.ext import commands
import re
import time
import datetime
from utils import db

INVITE_REGEX = re.compile(r"(discord\.gg|discordapp\.com/invite|discord\.com/invite)/\S+", re.IGNORECASE)
LINK_REGEX = re.compile(r"https?://\S+", re.IGNORECASE)

class AutoMod(commands.Cog):
    """Basic auto-moderation: spam detection and invite/link filtering."""

    automod_group = app_commands.Group(name="automod", description="Automod moderation filters")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings_cache = {}  # {guild_id: {"block_invites": bool, "anti_spam": bool}}
        self.message_log = {}  # {(guild_id, user_id): [timestamps]}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "automod"):
                await interaction.response.send_message(
                    "⚠️ The **Automod** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True
                )
                return False
        return True

    async def get_settings(self, guild_id):
        if guild_id not in self.settings_cache:
            block_invites = await db.get_setting(guild_id, "automod_block_invites", False)
            anti_spam = await db.get_setting(guild_id, "automod_anti_spam", True)
            self.settings_cache[guild_id] = {"block_invites": block_invites, "anti_spam": anti_spam}
        return self.settings_cache[guild_id]

    async def update_setting(self, guild_id, key, value):
        await db.set_setting(guild_id, f"automod_{key}", value)
        if guild_id in self.settings_cache:
            self.settings_cache[guild_id][key] = value

    @automod_group.command(name="invites", description="Toggle blocking of Discord invite links.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_invites(self, interaction: discord.Interaction, enabled: bool):
        await self.update_setting(interaction.guild.id, "block_invites", enabled)
        await interaction.response.send_message(f"Invite link blocking: **{'ON' if enabled else 'OFF'}**")

    @automod_group.command(name="antispam", description="Toggle anti-spam (rapid message) detection.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_antispam(self, interaction: discord.Interaction, enabled: bool):
        await self.update_setting(interaction.guild.id, "anti_spam", enabled)
        await interaction.response.send_message(f"Anti-spam: **{'ON' if enabled else 'OFF'}**")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if message.author.guild_permissions.manage_messages:
            return  # don't automod staff

        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(message.guild.id, "automod"):
            return

        settings = await self.get_settings(message.guild.id)

        # Invite link filter
        if settings["block_invites"] and INVITE_REGEX.search(message.content):
            # Deleting can fail (missing Manage Messages in this channel, message
            # already gone). An unhandled raise here aborted the listener, so the
            # notice and the rest of automod never ran.
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
            try:
                await message.channel.send(
                    f"{message.author.mention}, invite links aren't allowed here.", delete_after=5
                )
            except discord.Forbidden:
                pass
            return

        # Basic anti-spam: 5+ messages in 5 seconds
        if settings["anti_spam"]:
            key = (message.guild.id, message.author.id)
            now = time.time()
            timestamps = [t for t in self.message_log.get(key, []) if now - t < 5]
            timestamps.append(now)
            self.message_log[key] = timestamps

            # Prune users who have gone quiet, otherwise this dict grows forever
            # (one entry per member who ever sent a message, held in RAM).
            if len(self.message_log) > 5000:
                for stale_key, stamps in list(self.message_log.items()):
                    if not stamps or now - stamps[-1] > 60:
                        del self.message_log[stale_key]
                # Still over the cap (e.g. a burst from thousands of distinct
                # accounts): drop the least-recently-seen trackers until we're back
                # under it, so the dict has a hard ceiling.
                if len(self.message_log) > 5000:
                    over = len(self.message_log) - 5000
                    oldest = sorted(self.message_log.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
                    for old_key, _ in oldest[:over]:
                        del self.message_log[old_key]

            if len(timestamps) >= 6:
                try:
                    await message.author.timeout(discord.utils.utcnow() + datetime.timedelta(minutes=5),
                                                   reason="Auto-mod: spamming")
                    await message.channel.send(
                        f"{message.author.mention} was muted for 5 minutes for spamming.", delete_after=10
                    )
                except discord.Forbidden:
                    pass
                except discord.HTTPException as e:
                    # e.g. the target holds a role above the bot; don't let one
                    # unmutable spammer take the listener down every message.
                    print(f"[AUTOMOD] Could not time out {message.author}: {e}", flush=True)
                self.message_log[key] = []

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
