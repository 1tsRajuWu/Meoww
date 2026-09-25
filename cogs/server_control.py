import os
import re
import sqlite3
import time
import discord
from discord import app_commands
from discord.ext import commands
from utils import permissions, db
from utils.style import send_card, display_name

#: Table names this file will count. A name that is not one of these is skipped
#: rather than quoted into a query.
_PLAIN_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")


def _root_command_name(interaction: discord.Interaction) -> str | None:
    """The name that identifies the command, subcommand or not.

    ``interaction.command.name`` is the *sub*-command for a group member, so
    matching a group against the bypass list with it silently never matches
    (``/defaultrole set`` arrives as ``"set"``). The bot's own tree check
    already reads ``root_parent`` for this reason; do the same here.
    """
    command = interaction.command
    if command is None:
        return None
    parent = getattr(command, "root_parent", None)
    return parent.name if parent is not None else command.name


def _members_intent_warning(bot) -> str:
    """The one thing that turns the default role into a no-op, said out loud.

    Member events are only delivered when the privileged **Members intent** is
    enabled, so with it off nobody ever receives the role and nothing anywhere
    reports a problem — the setting just quietly does nothing. The person
    configuring it is the only one who can fix that, so they are the one told.
    """
    if getattr(getattr(bot, "intents", None), "members", False):
        return ""
    return (
        "\n\n⚠️ The **Members intent** is off, so Discord never tells the bot that "
        "somebody joined and no role will be handed out. Enable it in the "
        "[Developer Portal](https://discord.com/developers/applications), then set "
        "`ENABLE_MEMBERS_INTENT=1`."
    )


def _local_table_counts(db_path: str) -> tuple[dict[str, int], float, float, str]:
    """Row counts, sizes and an integrity check, with blocking work done off-loop.

    ``SELECT COUNT(*)`` is a full scan, and the tables that grow here are the ones
    with the most rows — called inline this held the event loop, and therefore
    every other command and the gateway heartbeat, for as long as the biggest
    table took to count. The sync-status half of /dbstatus already ran in a worker
    thread for the same reason.
    """
    counts: dict[str, int] = {}
    size_kb = 0.0
    wal_kb = 0.0
    integrity = "unknown"
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.Error as e:
        return counts, size_kb, wal_kb, str(e)
    try:
        try:
            size_kb = os.path.getsize(db_path) / 1024
        except OSError:
            size_kb = 0.0
        try:
            wal_kb = os.path.getsize(f"{db_path}-wal") / 1024
        except OSError:
            wal_kb = 0.0  # no WAL file is the normal case after a clean checkpoint
        cur = conn.cursor()
        try:
            integrity = str(cur.execute("PRAGMA quick_check(1)").fetchone()[0])
        except sqlite3.Error as e:
            integrity = str(e)
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        for (name,) in cur.fetchall():
            # A table name is an *identifier*, and an identifier cannot be a bound
            # parameter — ``?`` stands for a value, never for a name — so a query
            # that counts one is necessarily built by hand. The rule here is that
            # only names that are *nothing but* letters, digits and underscores
            # are counted at all: every table this bot creates is one of those,
            # so a name that needs quoting is either somebody else's table or a
            # name shaped like SQL, and a row count is not worth being the place
            # that finds out which.
            table = str(name)
            if not _PLAIN_IDENTIFIER.fullmatch(table):
                continue
            try:
                counts[table] = cur.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except sqlite3.Error:
                counts[table] = -1  # unreadable
    except sqlite3.Error as e:
        print(f"[DBSTATUS] Could not read the local database: {e}", flush=True)
    finally:
        conn.close()
    return counts, size_kb, wal_kb, integrity


class ServerControl(commands.Cog):
    """Lockdown, slowmode, and role management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            cmd_name = _root_command_name(interaction)
            # ``dbstatus`` belongs on this list for the same reason the other
            # three do: asking whether the database is healthy has nothing to do
            # with whether the moderation module is on. It was missing, so a
            # server with moderation off was refused ``/dbstatus`` — and it is
            # declared always-reachable in ``features.ALWAYS_ALLOWED_COMMANDS``,
            # so the per-server command rule and the enforcement disagreed about
            # it. Caught by ``tests/test_command_scope.py``.
            if cmd_name not in ["serverinfo", "userinfo", "defaultrole", "dbstatus"]:
                if not self.bot.is_module_enabled(interaction.guild_id, "moderation"):
                    await interaction.response.send_message(
                        "⚠️ The **Moderation** system is currently **turned off** on this server.\n"
                        "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
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
        embed.set_author(name="DaCat — Server Information", icon_url=guild.me.display_avatar.url)

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
        embed.set_author(name="DaCat — User Profile", icon_url=interaction.guild.me.display_avatar.url)
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
        embed.set_footer(text="DaCat Security")
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
        embed.set_footer(text="DaCat Security")
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
        embed.set_footer(text="DaCat Security")
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
        embed.set_footer(text="DaCat Security")
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

    @app_commands.command(name="dbstatus", description="Show database health: last backup, table row counts, and Supabase connection.")
    @app_commands.checks.has_permissions(administrator=True)
    async def dbstatus(self, interaction: discord.Interaction):
        import asyncio  # imported here, not inside one branch: every path needs it

        await interaction.response.defer(ephemeral=True)

        # --- local database stats (off the event loop: see _local_table_counts) ---
        db_path = db.DB_PATH
        tables, db_size_kb, wal_kb, integrity = await asyncio.to_thread(
            _local_table_counts, db_path
        )

        # --- Supabase / cloud status ---
        from utils.cloud_sync import (
            SUPABASE_URL,
            SUPABASE_KEY,
            _last_sync_hash,
            _remote_checked,
            _restored_from,
            _restored_counts,
            _storage_list,
            BUCKET,
            upload_refusal_reason,
            last_upload_ts,
        )
        supa_ok = bool(SUPABASE_URL and SUPABASE_KEY)
        cloud_files: list[dict] = []
        if supa_ok:
            cloud_files = await asyncio.to_thread(_storage_list)

        # --- build the embed ---
        embed = discord.Embed(
            title="🗄️ Database Status",
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name="DaCat — System",
            icon_url=interaction.guild.me.display_avatar.url,
        )

        # DB size. Integrity and the WAL are here because they are the two things a
        # row count cannot tell you: a corrupt restore and an un-checkpointed WAL
        # both look perfectly healthy as a count of rows.
        embed.add_field(
            name="💾 Local Database",
            value=(
                f"`{db_size_kb:.1f} KB` on disk\n"
                f"{len(tables)} table(s)\n"
                f"WAL: `{wal_kb:.1f} KB`\n"
                f"Integrity: {'✅ ok' if integrity == 'ok' else f'⚠️ {integrity[:60]}'}"
            ),
            inline=True,
        )

        # Cloud connection
        if supa_ok:
            current = next((f for f in cloud_files if f["name"] == "bot.db"), None)
            snapshots = [
                f for f in cloud_files
                if f["name"].startswith("bot-db-") and f["name"].endswith(".db")
            ]
            if current:
                size_b = current.get("metadata", {}).get("size", 0)
                embed.add_field(
                    name="☁️ Supabase Backup",
                    value=(
                        f"✅ Connected to `{BUCKET}`\n"
                        f"Current: `{size_b / 1024:.1f} KB`\n"
                        f"Snapshots: **{len(snapshots)}**"
                    ),
                    inline=True,
                )
            else:
                embed.add_field(
                    name="☁️ Supabase Backup",
                    value="⚠️ Connected but no `bot.db` found",
                    inline=True,
                )
        else:
            embed.add_field(
                name="☁️ Supabase Backup",
                value="❌ Not configured (env vars missing)",
                inline=True,
            )

        # Sync status. The refusal reason is the *whole* decision — an upload
        # silently not happening is how a backup turns out to be days old, so
        # this card is the place a human can see why.
        #
        # It talks to Supabase and reads SQLite, so it runs in a worker thread:
        # called inline it holds the event loop — and therefore every other
        # command, and the gateway heartbeat — for the length of a round trip.
        refusal = await asyncio.to_thread(upload_refusal_reason, False)
        uploaded = last_upload_ts()
        if refusal:
            sync_status = f"🛑 Not uploading: {refusal}"
        elif _remote_checked and _last_sync_hash:
            sync_status = "✅ Synced"
            if uploaded:
                # When, not just whether: a sync that stopped an hour ago and one
                # that ran twelve seconds ago read identically without this.
                age = max(0, int(time.time() - uploaded))
                sync_status += f"\nLast upload: {age}s ago"
        elif _remote_checked:
            sync_status = "⚠️ Restored but not yet uploaded"
        else:
            sync_status = "⏳ Not yet checked"
        if _restored_from:
            restored_rows = sum(_restored_counts.values())
            sync_status += f"\nRestored from `{_restored_from}` ({restored_rows} rows)"
        if len(sync_status) > 1024:
            sync_status = sync_status[:1021] + "…"
        embed.add_field(
            name="🔄 Sync Status",
            value=sync_status,
            inline=True,
        )

        # Row counts — chunked into 25-row fields (Discord field limit)
        if tables:
            lines = []
            for name, count in sorted(tables.items()):
                if count < 0:
                    lines.append(f"`{name}` — ❌")
                else:
                    lines.append(f"`{name}` — **{count:,}** rows")
            # Split into chunks of 15 lines each to stay under 1024 chars
            chunk_size = 15
            for i in range(0, len(lines), chunk_size):
                chunk = lines[i : i + chunk_size]
                embed.add_field(
                    name="📊 Table Rows" + (f" ({i // chunk_size + 1})" if i else ""),
                    value="\n".join(chunk),
                    inline=False,
                )

        embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── default role ───────────────────────────────────────────────────────

    default_role_group = app_commands.Group(
        name="defaultrole",
        description="Auto-assign a role to new members when they join.",
    )

    @default_role_group.command(name="set", description="Set a role to be auto-assigned to new members.")
    @app_commands.describe(role="The role to give to every new member")
    @app_commands.checks.has_permissions(administrator=True)
    async def defaultrole_set(self, interaction: discord.Interaction, role: discord.Role):
        problem = permissions.role_grant_problem(role, interaction.guild.me, interaction.guild)
        if problem:
            return await interaction.response.send_message(
                f"❌ I cannot assign {role.mention}: {problem}", ephemeral=True,
            )
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "❌ I cannot assign a role higher than or equal to my own role."
                " Move my role above it in **Server Settings → Roles**.",
                ephemeral=True,
            )
        if not interaction.guild.me.guild_permissions.manage_roles:
            return await interaction.response.send_message(
                "❌ I need the **Manage Roles** permission to assign roles.",
                ephemeral=True,
            )
        await db.set_setting(interaction.guild.id, "default_role_id", role.id)
        embed = discord.Embed(
            title="✅ Default Role Set",
            description=(
                f"New members will now automatically receive {role.mention} when "
                f"they join.{_members_intent_warning(self.bot)}"
            ),
            color=0x57F287,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(
            text=f"Set by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        await send_card(interaction, embed=embed)

    @default_role_group.command(name="view", description="View the current default role for new members.")
    async def defaultrole_view(self, interaction: discord.Interaction):
        role_id = await db.get_setting(interaction.guild.id, "default_role_id")
        if not role_id:
            embed = discord.Embed(
                description="ℹ️ No default role is set. Use `/defaultrole set` to configure one.",
                color=0x5865F2,
            )
            return await send_card(interaction, embed=embed)
        role = interaction.guild.get_role(int(role_id))
        if role is None:
            await db.set_setting(interaction.guild.id, "default_role_id", None)
            embed = discord.Embed(
                description="⚠️ The configured default role no longer exists. Use `/defaultrole set` to pick a new one.",
                color=0xED4245,
            )
            return await send_card(interaction, embed=embed)
        embed = discord.Embed(
            title="🎭 Default Role",
            description=(
                f"New members receive {role.mention} when they join."
                f"{_members_intent_warning(self.bot)}"
            ),
            color=role.color if role.color.value else 0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Role ID", value=f"`{role.id}`", inline=True)
        embed.add_field(name="Members with this role", value=f"`{len(role.members)}`", inline=True)
        embed.set_footer(
            text=f"Requested by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        await send_card(interaction, embed=embed)

    @default_role_group.command(name="clear", description="Remove the default role (stop auto-assigning on join).")
    @app_commands.checks.has_permissions(administrator=True)
    async def defaultrole_clear(self, interaction: discord.Interaction):
        current = await db.get_setting(interaction.guild.id, "default_role_id")
        if not current:
            embed = discord.Embed(
                description="ℹ️ No default role is currently set.",
                color=0x5865F2,
            )
            return await send_card(interaction, embed=embed)
        await db.set_setting(interaction.guild.id, "default_role_id", None)
        embed = discord.Embed(
            title="✅ Default Role Cleared",
            description="New members will no longer receive a role automatically.",
            color=0x57F287,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(
            text=f"Cleared by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        await send_card(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerControl(bot))

