"""Basic auto-moderation: spam detection and invite/link filtering.

Two things here are as important as the filters themselves, and both were
missing until a pass over the bot-platform rules asked what happens *after* an
automatic decision:

* **Every automatic action leaves a record.** A deleted invite and a five-minute
timeout used to live only in a notice that deleted itself after ten seconds, so
nobody but the member had ever seen it and a moderator asked "did the bot mute
somebody?" had nothing to read. Each decision now posts a card — the rule that
fired, who it hit, where, and whether Discord accepted the action — to the same
log channel a manual case goes to (``utils.modlog``).
* **The member can appeal it.** Automated filters are wrong sometimes, and the
notice now says so out loud, naming ``/appeal``; the command files a review with
the humans instead of leaving the member to argue with a bot.
"""
import discord
from discord import app_commands
from discord.ext import commands
import re
import time
import datetime
from utils import db, modlog
from utils.style import WARN

INVITE_REGEX = re.compile(r"(discord\.gg|discordapp\.com/invite|discord\.com/invite)/\S+", re.IGNORECASE)
LINK_REGEX = re.compile(r"https?://\S+", re.IGNORECASE)

#: One sentence, in every notice an automatic action leaves behind. A member who
#: disagrees with a bot needs a door, and it has to be named where they are
#: standing — a policy page three clicks away is not one.
APPEAL_POINTER = "If that was wrong, run `/appeal` — a human will read it."


def _audit_embed(
    action: str,
    member: discord.abc.User,
    channel,
    rule: str,
    detail: str,
    outcome: str,
) -> discord.Embed:
    """What a moderator would need to judge an automatic action.

    No message content: the rules live on a count and a pattern, and copying the
    text into a log would keep a copy of something the server never asked to
    keep. The member, the rule, the channel and the outcome are the whole of it.
    """
    embed = discord.Embed(
        title=f"🤖 Auto-mod — {action}",
        # A warning, through the palette the rest of the cards read: the literal
        # this used to be was one more colour the product had to keep in step.
        color=WARN,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name="Meow Moderation Log", icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="👤 Member", value=f"{member.mention}\n`{member.name}`", inline=True)
    embed.add_field(name="📍 Channel", value=getattr(channel, "mention", "—"), inline=True)
    embed.add_field(name="📐 Rule", value=rule, inline=False)
    embed.add_field(name="⚙️ Outcome", value=f"{detail}\n{outcome}", inline=False)
    embed.set_footer(text=f"Automatic action • Member ID: {member.id}")
    return embed


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
                    "An administrator can re-enable it in the [Web Dashboard](https://meow-bot.pages.dev/dashboard).",
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
                    f"{message.author.mention}, invite links aren't allowed here. "
                    f"{APPEAL_POINTER}",
                    delete_after=15,
                )
            except discord.Forbidden:
                pass
            await modlog.post(
                message.guild,
                _audit_embed(
                    "Invite deleted",
                    message.author,
                    message.channel,
                    rule="Invite links are blocked in this server (`/automod invites`).",
                    detail="The message was deleted; a notice naming `/appeal` was posted.",
                    outcome="No timeout, no warning — the message was the whole of it.",
                ),
                what=f"the automod invite deletion for member {message.author.id}",
            )
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
                outcome = "Discord accepted the timeout."
                try:
                    await message.author.timeout(discord.utils.utcnow() + datetime.timedelta(minutes=5),
                                                   reason="Auto-mod: spamming")
                    await message.channel.send(
                        f"{message.author.mention} was muted for 5 minutes for spamming. "
                        f"{APPEAL_POINTER}",
                        delete_after=15,
                    )
                except discord.Forbidden:
                    outcome = (
                        "Discord refused it (missing permission, or this member's role "
                        "is above mine) — they were **not** muted. Fix that in Server "
                        "Settings → Roles, or the filter cannot act on them."
                    )
                except discord.HTTPException as e:
                    # e.g. the target holds a role above the bot; don't let one
                    # unmutable spammer take the listener down every message.
                    print(f"[AUTOMOD] Could not time out {message.author}: {e}", flush=True)
                    outcome = f"Discord rejected it ({type(e).__name__}) — they were **not** muted."
                await modlog.post(
                    message.guild,
                    _audit_embed(
                        "Member timed out",
                        message.author,
                        message.channel,
                        rule="Six or more messages in five seconds (`/automod antispam`).",
                        detail="A five-minute timeout was attempted after rapid messages.",
                        outcome=outcome,
                    ),
                    what=f"the automod timeout for member {message.author.id}",
                )
                self.message_log[key] = []

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
