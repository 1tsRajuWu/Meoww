import discord
from discord import app_commands
from discord.ext import commands
from utils import permissions
from utils.style import send_card, display_name

class ServerControl(commands.Cog):
    """Lockdown, slowmode, and role management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            cmd_name = interaction.command.name if interaction.command else None
            if cmd_name not in ["serverinfo", "userinfo"]:
                if not self.bot.is_module_enabled(interaction.guild_id, "moderation"):
                    await interaction.response.send_message(
                        "⚠️ The **Moderation** system is currently **turned off** on this server.\n"
                        "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                        ephemeral=True
                    )
                    return False
        return True

    @app_commands.command(name="serverinfo", description="Get comprehensive information about the server.")
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        created_ts = int(guild.created_at.timestamp())

        # ``guild.owner`` is ``get_member(owner_id)``, which is None without the
        # Members intent — this card used to say "Server Owner: Unknown" on a
        # server whose owner was sitting right there. Resolve the name instead.
        owner = guild.owner
        owner_name = (
            owner.display_name
            if owner
            else await display_name(guild, guild.owner_id, client=interaction.client)
        )

        embed = discord.Embed(
            title=f"🏰 {guild.name}",
            description=(
                f"> 👑 **Server Owner:** {owner_name}\n"
                f"> 📅 **Created:** <t:{created_ts}:D> (<t:{created_ts}:R>)\n"
                f"> 🆔 **Server ID:** `{guild.id}`"
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_author(name="🐾 Meow — Server Information", icon_url=guild.me.display_avatar.url)

        embed.add_field(
            name="👥 Members",
            value=f"**{guild.member_count:,}** total members",
            inline=True
        )
        embed.add_field(
            name="💬 Channels",
            value=f"**{len(guild.text_channels)}** text · **{len(guild.voice_channels)}** voice",
            inline=True
        )
        embed.add_field(
            name="✨ Boost Status",
            value=f"Level **{guild.premium_tier}** ({guild.premium_subscription_count} boosts)",
            inline=True
        )
        embed.add_field(
            name="🛡️ Roles",
            value=f"**{len(guild.roles)}** roles configured",
            inline=True
        )
        embed.add_field(
            name="🔒 Verification",
            value=f"`{str(guild.verification_level).capitalize()}`",
            inline=True
        )
        embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await send_card(interaction, embed=embed)

    @app_commands.command(name="userinfo", description="Get detailed information about a user.")
    @app_commands.describe(member="The member to inspect (leave empty for yourself)")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        created_ts = int(member.created_at.timestamp())
        joined_ts = int(member.joined_at.timestamp()) if member.joined_at else None

        embed = discord.Embed(
            title=f"👤 {member.display_name}",
            description=(
                f"> 📛 **Username:** `{member.name}`\n"
                f"> 🆔 **User ID:** `{member.id}`\n"
                f"> 🤖 **Bot Account:** {'Yes' if member.bot else 'No'}"
            ),
            color=member.color if member.color.value != 0 else 0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name="🐾 Meow — User Profile", icon_url=interaction.guild.me.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)

        embed.add_field(
            name="📅 Account Created",
            value=f"<t:{created_ts}:D>\n(<t:{created_ts}:R>)",
            inline=True
        )
        if joined_ts:
            embed.add_field(
                name="📥 Joined Server",
                value=f"<t:{joined_ts}:D>\n(<t:{joined_ts}:R>)",
                inline=True
            )
        embed.add_field(
            name="🔝 Top Role",
            value=member.top_role.mention if member.top_role else "None",
            inline=True
        )

        roles = [r.mention for r in reversed(member.roles[1:])]
        if roles:
            role_text = " ".join(roles)
            if len(role_text) > 1024:
                role_text = f"{' '.join(roles[:15])} ... (+{len(roles)-15} more)"
            embed.add_field(name=f"🎭 Roles [{len(roles)}]", value=role_text, inline=False)

        embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await send_card(interaction, embed=embed)

    @app_commands.command(name="lockdown", description="Lock this channel so regular members cannot send messages.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lockdown(self, interaction: discord.Interaction):
        await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
        embed = discord.Embed(
            title="🔒 Channel Locked Down",
            description=f"This channel has been locked by {interaction.user.mention}.\nMembers can no longer send messages until unlocked.",
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Meow Security")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="unlock", description="Unlock this channel for all members.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=True)
        embed = discord.Embed(
            title="🔓 Channel Unlocked",
            description=f"This channel has been unlocked by {interaction.user.mention}.\nChat permissions have been restored.",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Meow Security")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="lockdown-server", description="Lock ALL text channels in the server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def lockdown_server(self, interaction: discord.Interaction):
        await interaction.response.defer()
        count = 0
        for channel in interaction.guild.text_channels:
            try:
                await channel.set_permissions(interaction.guild.default_role, send_messages=False)
                count += 1
            except discord.Forbidden:
                pass
        
        embed = discord.Embed(
            title="🚨 Server-Wide Lockdown Activated",
            description=f"**{count}** channel(s) have been locked by {interaction.user.mention}.\nAll public chat has been suspended.",
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Meow Security")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="unlock-server", description="Unlock ALL text channels in the server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def unlock_server(self, interaction: discord.Interaction):
        await interaction.response.defer()
        count = 0
        for channel in interaction.guild.text_channels:
            try:
                await channel.set_permissions(interaction.guild.default_role, send_messages=True)
                count += 1
            except discord.Forbidden:
                pass

        embed = discord.Embed(
            title="🔓 Server-Wide Lockdown Lifted",
            description=f"**{count}** channel(s) have been unlocked by {interaction.user.mention}.\nPublic chat permissions restored.",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Meow Security")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="slowmode", description="Set slowmode delay for this channel (seconds, 0 to disable).")
    @app_commands.describe(seconds="Delay in seconds (0 to 21600)")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        await interaction.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            embed = discord.Embed(
                description="⏱️ **Slowmode has been disabled** for this channel.",
                color=0x57F287
            )
        else:
            embed = discord.Embed(
                description=f"⏱️ **Slowmode set to `{seconds}` second(s)** by {interaction.user.mention}.",
                color=0x5865F2
            )
        await send_card(interaction, embed=embed)

    @app_commands.command(name="addrole", description="Add a role to a member.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def addrole(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        # Two different hierarchy questions, and only the first one was asked
        # here before: whether *I* may assign it, and whether *you* may. Without
        # the second, a moderator with Manage Roles could grant Administrator to
        # themselves or an alt.
        problem = permissions.role_grant_problem(role, interaction.user, interaction.guild)
        if problem:
            return await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("❌ I cannot assign a role higher than or equal to my own role.", ephemeral=True)
        await member.add_roles(role)
        
        embed = discord.Embed(
            title="✅ Role Assigned",
            description=f"Successfully added {role.mention} to {member.mention}.",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Actioned by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await send_card(interaction, embed=embed)

    @app_commands.command(name="removerole", description="Remove a role from a member.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def removerole(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        problem = permissions.role_grant_problem(role, interaction.user, interaction.guild)
        if problem:
            return await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
        if role >= interaction.guild.me.top_role:                    
            return await interaction.response.send_message("❌ I cannot remove a role higher than or equal to my own role.", ephemeral=True)
        await member.remove_roles(role)

        embed = discord.Embed(
            title="🗑️ Role Removed",
            description=f"Successfully removed {role.mention} from {member.mention}.",
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Actioned by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await send_card(interaction, embed=embed)

    @app_commands.command(name="announce", description="Send a styled announcement embed to a channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def announce(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str, title: str = "📢 Announcement"):
        embed = discord.Embed(
            title=title,
            description=message,
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        if interaction.guild.icon:
            embed.set_author(name=interaction.guild.name, icon_url=interaction.guild.icon.url)
        embed.set_footer(text=f"Announced by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        
        # Announcements are the one place a ping is the point: the text is written
        # by an admin with Manage Server, so honour whatever they typed. Every
        # other card stays silent by default.
        await send_card(
            channel,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=True),
        )
        await interaction.response.send_message(f"✅ Announcement posted in {channel.mention}!", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(ServerControl(bot))

