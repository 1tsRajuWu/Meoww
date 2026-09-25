import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from utils import db, moderation_actions, modlog
from utils.style import ACCENT, send_card

# Action type config: emoji, color, verb, ansi_color
ACTION_CONFIG = {
    "Warn":   ("⚠️",  0xF1C40F, "Warned",   "\u001b[1;33m"),   # gold
    "Ban":    ("🔨",  0xE74C3C, "Banned",   "\u001b[1;31m"),   # red
    "Kick":   ("👢",  0xE67E22, "Kicked",   "\u001b[1;33m"),   # orange
    "Mute":   ("🔇",  0x9B59B6, "Muted",    "\u001b[1;35m"),   # purple
    "Unmute": ("🔊",  0x2ECC71, "Unmuted",  "\u001b[1;32m"),   # green
    "Unban":  ("✅",  0x2ECC71, "Unbanned", "\u001b[1;32m"),   # green
}


#: The guard lives in ``utils.moderation_actions`` now, because the website became
#: a second caller of these commands' powers and a guard with two definitions is
#: how a panel ends up able to do what a command cannot.
_mod_target_problem = moderation_actions.target_problem


def _actor(interaction: discord.Interaction) -> moderation_actions.Actor:
    """The person who ran the command, as the shared module wants them."""
    user = interaction.user
    return moderation_actions.Actor(
        id=int(getattr(user, "id", 0) or 0),
        name=str(getattr(user, "display_name", None) or getattr(user, "name", "moderator")),
        member=user if isinstance(user, discord.Member) else None,
        source="discord",
    )

class Moderation(commands.Cog):
    """Full-featured moderation with rich embeds and log channel support."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "moderation"):
                await interaction.response.send_message(
                    "⚠️ The **Moderation** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://meow-bot.pages.dev/dashboard).",
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

    # The log card used to be built here too (``_log_embed``). It is
    # ``utils.moderation_actions.body_embed`` now, so a card the website caused
    # and a card ``/ban`` caused are the same card — the log is the record, and a
    # difference between the two doors would be visible in exactly the list people
    # read to find out what happened.

    # ── the one door ─────────────────────────────────────────────────────────
    #
    # Every command below is now a thin caller of ``utils.moderation_actions``,
    # which holds the guard, the Discord call, the member's DM and the record. The
    # website reaches the same function, so "what happened when somebody was
    # banned" has one answer whether it was typed or clicked — and a refused
    # timeout is written down as refused instead of only being told to the person
    # who ran the command.
    async def _act(
        self,
        interaction: discord.Interaction,
        action: str,
        member,
        *,
        reason: str = "No reason provided",
        minutes: int = 0,
        delete_days: int = 0,
        label: str = "",
    ):
        """Run one action and answer with the same card the website gets told about."""
        outcome = await moderation_actions.apply(
            moderation_actions.Request(
                guild=interaction.guild,
                action=action,
                target=member,
                actor=_actor(interaction),
                reason=reason,
                minutes=minutes,
                delete_days=delete_days,
            )
        )
        if not outcome.ok:
            return await interaction.followup.send(f"❌ {outcome.detail}", ephemeral=True)
        await send_card(
            interaction,
            embed=self._action_embed(
                label or outcome.verb, member, interaction.user, outcome.reason, outcome.case_id or 0
            ),
        )
        return outcome

    # ── commands ─────────────────────────────────────────────────────────────

    @app_commands.command(name="warn", description="Warn a member.")
    @app_commands.describe(member="Member to warn", reason="Reason for the warning")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        await interaction.response.defer()
        await self._act(interaction, "warn", member, reason=reason, label="Warn")

    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(member="Member to ban", reason="Reason for the ban", delete_days="Days of messages to delete (0-7)")
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: app_commands.Range[int, 0, 7] = 0):
        await interaction.response.defer()
        await self._act(interaction, "ban", member, reason=reason, delete_days=delete_days, label="Ban")

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(member="Member to kick", reason="Reason for the kick")
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        await interaction.response.defer()
        await self._act(interaction, "kick", member, reason=reason, label="Kick")

    @app_commands.command(name="mute", description="Timeout (mute) a member.")
    @app_commands.describe(member="Member to mute", minutes="Duration in minutes", reason="Reason for the mute")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def mute(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
        await interaction.response.defer()
        await self._act(interaction, "timeout", member, reason=reason, minutes=minutes, label="Mute")

    @app_commands.command(name="unmute", description="Remove timeout from a member.")
    @app_commands.describe(member="Member to unmute")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def unmute(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        await self._act(interaction, "timeout_remove", member, label="Unmute")

    @app_commands.command(name="unban", description="Unban a user by ID.")
    @app_commands.describe(user_id="ID of the user to unban", reason="Reason for the unban")
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "Manual Unban"):
        await interaction.response.defer()
        try:
            target_id = int(user_id)
        except ValueError:
            return await interaction.followup.send("❌ Invalid user ID.", ephemeral=True)
        user = await self.bot.fetch_user(target_id)
        await self._act(interaction, "unban", user, reason=reason, label="Unban")

    @app_commands.command(name="warnings", description="View a member's warnings.")
    @app_commands.describe(member="Member to check")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        rows = await db.get_warnings(interaction.guild.id, member.id)

        embed = discord.Embed(
            title=f"⚠️ Warnings — {member.display_name}",
            color=0xFFD700,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        if not rows:
            embed.description = "✅ This member has no warnings."
        else:
            embed.description = "\n".join([f"`#{row['id']}` — {row['reason']}" for row in rows])
            embed.set_footer(text=f"Total: {len(rows)} warning(s)")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="clearwarnings", description="Clear all warnings for a member.")
    @app_commands.describe(member="Member to clear")
    @app_commands.checks.has_permissions(administrator=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        problem = _mod_target_problem(interaction.user, member)
        if problem:
            return await interaction.followup.send(f"❌ {problem}", ephemeral=True)
        await db.clear_warnings(interaction.guild.id, member.id)
        await moderation_actions.record(
            interaction.guild,
            "clear_warnings",
            member,
            _actor(interaction),
            "All warnings cleared",
        )
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

    @app_commands.command(
        name="appeal",
        description="Ask a human to review a moderation action taken against you.",
    )
    @app_commands.describe(reason="What happened, and why you think the action was wrong")
    @app_commands.checks.cooldown(
        1, 300, key=lambda interaction: (interaction.guild_id, interaction.user.id)
    )
    async def appeal(self, interaction: discord.Interaction, reason: str):
        """The path a member has when the *bot* was the one that acted.

        ``/automod`` deletes an invite and times out a flood on its own, and
        automated decisions are wrong sometimes — a false positive with no way to
        say so is how a bot teaches a server to distrust it. The appeal does not
        lift anything: it puts a readable card in front of the humans, with the
        member's own words and the cases already on their record, and says so in
        the reply. A moderator answering it uses ``/unmute`` or ``/unban``, which
        are logged like every other action.
        """
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = interaction.user

        case_id = await db.add_mod_case(
            guild.id, "Appeal", member.id, member.id, reason
        )

        # What a moderator needs to judge it: the action they are appealing, and
        # the rest of their recent record. Read-only, and capped at three so the
        # card stays a card.
        history = []
        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute(
                "SELECT case_type, reason FROM mod_cases "
                "WHERE guild_id = ? AND user_id = ? AND case_id != ? "
                "ORDER BY case_id DESC LIMIT 3",
                (guild.id, member.id, case_id),
            ) as cursor:
                history = await cursor.fetchall()

        embed = discord.Embed(
            title="📨 Appeal — a member is asking for a review",
            color=ACCENT,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="Meow Moderation Log", icon_url=self.bot.user.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="👤 Member", value=f"{member.mention}\n`{member.name}`", inline=True)
        embed.add_field(name="📋 Case", value=f"`#{case_id}`", inline=True)
        embed.add_field(name="💬 In their words", value=reason, inline=False)
        if history:
            embed.add_field(
                name="🗂️ Recent record",
                value="\n".join(f"`{kind}` — {text}" for kind, text in history),
                inline=False,
            )
        embed.add_field(
            name="🛠️ How to answer it",
            value=(
                "Nothing changes on its own. If the action was wrong, `/unmute` or "
                "`/unban` reverses it and leaves its own case in this channel."
            ),
            inline=False,
        )
        embed.set_footer(text=f"Member ID: {member.id} • appeal case #{case_id}")

        filed = await modlog.post(guild, embed, what=f"the appeal (case #{case_id})")
        if filed:
            await interaction.followup.send(
                f"📨 Filed as case `#{case_id}` — a human can now read it in this "
                "server's moderation log. Nothing about the action changes on its "
                "own; if it was wrong, a moderator will reverse it.",
                ephemeral=True,
            )
            return

        # Nowhere to log it is not a dead end: the server owner is the one person
        # guaranteed to be able to act on it, and the reply says where it went so
        # nobody is left guessing.
        owner = guild.owner
        try:
            await owner.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "📋 I could not file that: this server has no moderation log "
                "channel set, and I could not reach the owner by DM. Nothing was "
                "recorded. An admin can run `/modlog #channel` and you can try "
                "again.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"📨 Filed as case `#{case_id}` — this server has no moderation log "
            f"channel set, so it went to {owner.display_name} (the server owner) "
            "as a DM.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
