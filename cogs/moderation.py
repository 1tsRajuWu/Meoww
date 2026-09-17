import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import datetime
from utils import db
from utils.style import send_card

# Action type config: emoji, color, verb, ansi_color
ACTION_CONFIG = {
    "Warn":   ("⚠️",  0xF1C40F, "Warned",   "\u001b[1;33m"),   # gold
    "Ban":    ("🔨",  0xE74C3C, "Banned",   "\u001b[1;31m"),   # red
    "Kick":   ("👢",  0xE67E22, "Kicked",   "\u001b[1;33m"),   # orange
    "Mute":   ("🔇",  0x9B59B6, "Muted",    "\u001b[1;35m"),   # purple
    "Unmute": ("🔊",  0x2ECC71, "Unmuted",  "\u001b[1;32m"),   # green
    "Unban":  ("✅",  0x2ECC71, "Unbanned", "\u001b[1;32m"),   # green
}

RESET = "\u001b[0m"

def _mod_target_problem(actor: discord.Member, target: discord.Member) -> str | None:
    """Refuse self / owner / bot / equal-or-higher role moderation targets."""
    if target.id == actor.id:
        return "You cannot moderate yourself."
    if target.id == actor.guild.owner_id:
        return "You cannot moderate the server owner."
    if getattr(actor.guild, "me", None) and target.id == actor.guild.me.id:
        return "You cannot moderate me."
    if target.guild_permissions.administrator and not actor.guild_permissions.administrator:
        return "You cannot moderate an administrator."
    if actor.id != actor.guild.owner_id and target.top_role >= actor.top_role:
        return (
            f"**{target.display_name}** has a role at or above yours "
            f"(**{target.top_role.name}**)."
        )
    return None

class Moderation(commands.Cog):
    """Full-featured moderation with rich embeds and log channel support."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "moderation"):
                await interaction.response.send_message(
                    "⚠️ The **Moderation** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True
                )
                return False
        return True

    # ── helpers ──────────────────────────────────────────────────────────────

    def _action_embed(
        self,
        action_type: str,
        target: discord.Member | discord.User,
        moderator: discord.Member,
        reason: str,
        case_id: int,
    ) -> discord.Embed:
        emoji, color, verb, _ = ACTION_CONFIG.get(action_type, ("📋", 0x7289DA, "Actioned", "\u001b[1;37m"))

        embed = discord.Embed(
            title=f"{emoji} {action_type} — Case #{case_id}",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(
            name="Meow Moderation",
            icon_url=self.bot.user.display_avatar.url
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.add_field(
            name="👤 Target",
            value=f"{target.mention}\n`{target.name}`",
            inline=True
        )
        embed.add_field(
            name="🛡️ Moderator",
            value=f"{moderator.mention}\n`{moderator.name}`",
            inline=True
        )
        embed.add_field(
            name="📋 Reason",
            value=reason,
            inline=False
        )

        embed.set_footer(
            text=f"Target ID: {target.id} • {verb} by {moderator.display_name}",
            icon_url=moderator.display_avatar.url
        )
        return embed

    def _log_embed(
        self,
        action_type: str,
        target: discord.Member | discord.User,
        moderator: discord.Member,
        reason: str,
        case_id: int,
    ) -> discord.Embed:
        # This card keys off the action name and IDs, unlike _action_embed which
        # puts the verb in its footer, so the verb slot is unused here.
        emoji, color, _, _ = ACTION_CONFIG.get(action_type, ("📋", 0x7289DA, "Actioned", "\u001b[1;37m"))

        embed = discord.Embed(
            title=f"{emoji} {action_type} — Case #{case_id}",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(
            name="Meow Moderation Log",
            icon_url=self.bot.user.display_avatar.url
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.add_field(
            name="👤 Target",
            value=f"{target.mention}\n`{target.name}`",
            inline=True
        )
        embed.add_field(
            name="🛡️ Moderator",
            value=f"{moderator.mention}\n`{moderator.name}`",
            inline=True
        )
        embed.add_field(
            name="📋 Reason",
            value=reason,
            inline=False
        )
        embed.add_field(
            name="🕐 Time",
            value=f"<t:{int(discord.utils.utcnow().timestamp())}:R>",
            inline=True
        )

        embed.set_footer(
            text=f"Target ID: {target.id} • Mod ID: {moderator.id}",
            icon_url=moderator.display_avatar.url
        )
        return embed


    async def _send_log(
        self,
        guild: discord.Guild,
        action_type: str,
        target: discord.Member | discord.User,
        moderator: discord.Member,
        reason: str,
        case_id: int,
    ):
        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute(
                "SELECT value FROM guild_settings WHERE guild_id = ? AND key = 'log_channel_id'",
                (guild.id,),
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return
        channel = guild.get_channel(int(row[0]))
        if channel is None:
            # A stale log_channel_id (deleted channel, or a foreign ID written by
            # an old /updatedb) used to make every log vanish without a trace.
            print(
                f"[MODLOG] Guild {guild.id} has log_channel_id={row[0]} but that channel "
                f"does not exist; the '{action_type}' case was not logged."
            )
            return
        try:
            await send_card(channel, embed=self._log_embed(action_type, target, moderator, reason, case_id))
        except discord.Forbidden:
            print(f"[MODLOG] No permission to post in <#{row[0]}> (guild {guild.id}).")

    # ── commands ─────────────────────────────────────────────────────────────

    @app_commands.command(name="warn", description="Warn a member.")
    @app_commands.describe(member="Member to warn", reason="Reason for the warning")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        await interaction.response.defer()
        problem = _mod_target_problem(interaction.user, member)
        if problem:
            return await interaction.followup.send(f"❌ {problem}", ephemeral=True)


        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO warnings (guild_id, user_id, reason) VALUES (?, ?, ?)",
                (interaction.guild.id, member.id, reason),
            )
            await conn.commit()

        case_id = await db.add_mod_case(interaction.guild.id, "Warn", member.id, interaction.user.id, reason)

        try:
            dm = discord.Embed(color=0xFFD700, title="⚠️ You have been warned", timestamp=discord.utils.utcnow())
            dm.add_field(name="Server", value=interaction.guild.name, inline=True)
            dm.add_field(name="Reason", value=reason, inline=False)
            await send_card(member, embed=dm)
        except discord.Forbidden:
            pass

        await send_card(interaction, embed=self._action_embed("Warn", member, interaction.user, reason, case_id))
        await self._send_log(interaction.guild, "Warn", member, interaction.user, reason, case_id)

    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(member="Member to ban", reason="Reason for the ban", delete_days="Days of messages to delete (0-7)")
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: app_commands.Range[int, 0, 7] = 0):
        await interaction.response.defer()
        problem = _mod_target_problem(interaction.user, member)
        if problem:
            return await interaction.followup.send(f"❌ {problem}", ephemeral=True)


        # Try to DM before banning
        try:
            dm = discord.Embed(color=0xFF3333, title="🔨 You have been banned", timestamp=discord.utils.utcnow())
            dm.add_field(name="Server", value=interaction.guild.name, inline=True)
            dm.add_field(name="Reason", value=reason, inline=False)
            await send_card(member, embed=dm)
        except discord.Forbidden:
            pass

        try:
            await member.ban(reason=f"{interaction.user}: {reason}", delete_message_days=delete_days)
            case_id = await db.add_mod_case(interaction.guild.id, "Ban", member.id, interaction.user.id, reason)
            await send_card(interaction, embed=self._action_embed("Ban", member, interaction.user, reason, case_id))
            await self._send_log(interaction.guild, "Ban", member, interaction.user, reason, case_id)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I cannot ban this member — move the bot's role above their highest role in `Server Settings → Roles`.",
                ephemeral=True,
            )

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(member="Member to kick", reason="Reason for the kick")
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        await interaction.response.defer()
        problem = _mod_target_problem(interaction.user, member)
        if problem:
            return await interaction.followup.send(f"❌ {problem}", ephemeral=True)


        try:
            dm = discord.Embed(color=0xFF8C00, title="👢 You have been kicked", timestamp=discord.utils.utcnow())
            dm.add_field(name="Server", value=interaction.guild.name, inline=True)
            dm.add_field(name="Reason", value=reason, inline=False)
            await send_card(member, embed=dm)
        except discord.Forbidden:
            pass

        try:
            await member.kick(reason=f"{interaction.user}: {reason}")
            case_id = await db.add_mod_case(interaction.guild.id, "Kick", member.id, interaction.user.id, reason)
            await send_card(interaction, embed=self._action_embed("Kick", member, interaction.user, reason, case_id))
            await self._send_log(interaction.guild, "Kick", member, interaction.user, reason, case_id)
        except discord.Forbidden:
            await interaction.followup.send("❌ I cannot kick this member — move my role above theirs.", ephemeral=True)

    @app_commands.command(name="mute", description="Timeout (mute) a member.")
    @app_commands.describe(member="Member to mute", minutes="Duration in minutes", reason="Reason for the mute")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def mute(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
        await interaction.response.defer()
        duration = datetime.timedelta(minutes=minutes)
        full_reason = f"{reason} ({minutes}m)"
        problem = _mod_target_problem(interaction.user, member)
        if problem:
            return await interaction.followup.send(f"❌ {problem}", ephemeral=True)


        try:
            await member.timeout(duration, reason=f"{interaction.user}: {full_reason}")
            case_id = await db.add_mod_case(interaction.guild.id, "Mute", member.id, interaction.user.id, full_reason)
            await send_card(interaction, embed=self._action_embed("Mute", member, interaction.user, full_reason, case_id))
            await self._send_log(interaction.guild, "Mute", member, interaction.user, full_reason, case_id)
        except discord.Forbidden:
            await interaction.followup.send("❌ I cannot mute this member — move my role above theirs.", ephemeral=True)

    @app_commands.command(name="unmute", description="Remove timeout from a member.")
    @app_commands.describe(member="Member to unmute")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def unmute(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        problem = _mod_target_problem(interaction.user, member)
        if problem:
            return await interaction.followup.send(f"❌ {problem}", ephemeral=True)

        try:
            await member.timeout(None, reason=f"Unmuted by {interaction.user}")
            case_id = await db.add_mod_case(interaction.guild.id, "Unmute", member.id, interaction.user.id, "Timeout removed")
            await send_card(interaction, embed=self._action_embed("Unmute", member, interaction.user, "Timeout removed", case_id))
            await self._send_log(interaction.guild, "Unmute", member, interaction.user, "Timeout removed", case_id)
        except discord.Forbidden:
            await interaction.followup.send("❌ I cannot unmute this member.", ephemeral=True)

    @app_commands.command(name="unban", description="Unban a user by ID.")
    @app_commands.describe(user_id="ID of the user to unban", reason="Reason for the unban")
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "Manual Unban"):
        await interaction.response.defer()
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=f"Unbanned by {interaction.user}: {reason}")
            case_id = await db.add_mod_case(interaction.guild.id, "Unban", user.id, interaction.user.id, reason)
            await send_card(interaction, embed=self._action_embed("Unban", user, interaction.user, reason, case_id))
            await self._send_log(interaction.guild, "Unban", user, interaction.user, reason, case_id)
        except ValueError:
            await interaction.followup.send("❌ Invalid user ID.", ephemeral=True)
        except discord.NotFound:
            await interaction.followup.send("❌ User is not banned or does not exist.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ I do not have permission to unban.", ephemeral=True)

    @app_commands.command(name="warnings", description="View a member's warnings.")
    @app_commands.describe(member="Member to check")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute(
                "SELECT id, reason FROM warnings WHERE guild_id = ? AND user_id = ?",
                (interaction.guild.id, member.id),
            ) as cursor:
                rows = await cursor.fetchall()

        embed = discord.Embed(
            title=f"⚠️ Warnings — {member.display_name}",
            color=0xFFD700,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        if not rows:
            embed.description = "✅ This member has no warnings."
        else:
            embed.description = "\n".join([f"`#{row[0]}` — {row[1]}" for row in rows])
            embed.set_footer(text=f"Total: {len(rows)} warning(s)")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="clearwarnings", description="Clear all warnings for a member.")
    @app_commands.describe(member="Member to clear")
    @app_commands.checks.has_permissions(administrator=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute(
                "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
                (interaction.guild.id, member.id),
            )
            await conn.commit()
        embed = discord.Embed(
            description=f"✅ Cleared all warnings for {member.mention}.",
            color=0x2ECC71,
        )
        await send_card(interaction, embed=embed)

    @app_commands.command(name="purge", description="Delete recent messages in the channel.")
    @app_commands.describe(amount="Number of messages to delete (1-100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        embed = discord.Embed(
            description=f"🗑️ Deleted **{len(deleted)}** message(s).",
            color=0xFF3333,
        )
        await send_card(interaction, embed=embed)

    @app_commands.command(name="modlog", description="Set the channel for moderation logs.")
    @app_commands.describe(channel="The channel to send logs to")
    @app_commands.checks.has_permissions(administrator=True)
    async def modlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, 'log_channel_id', ?)",
                (interaction.guild.id, channel.id),
            )
            await conn.commit()
        embed = discord.Embed(
            description=f"📋 Moderation logs will now be sent to {channel.mention}.",
            color=0x2ECC71,
        )
        await send_card(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
