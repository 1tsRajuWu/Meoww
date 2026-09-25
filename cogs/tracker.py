"""Activity Tracker — per-channel message counts and voice channel time.

The core tracking: every message counted per channel, every voice-channel
second logged per VC. 365-day rolling window with automatic cleanup.

Commands:
  /activity-top   — Server-wide: top channels by messages, top VCs by time
  /activity       — Personal: your message + voice breakdown by channel
  /activity-channel — One channel's message leaderboard
"""

import datetime
import time
from collections import defaultdict

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import db, style
# The duration and count spellings live in one place so the digest the scheduler
# posts and the numbers this cog prints cannot drift apart on the same data.
from utils.digest import human_count as _fmt_count, human_voice as _fmt_voice

DB_PATH = db.DB_PATH
RETENTION_DAYS = 365
VOICE_FLUSH_INTERVAL = 120
CLEANUP_INTERVAL = 6 * 3600


def _today() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def _period_label(p: str) -> str:
    return {
        "today": "Today",
        "week": "Last 7 Days",
        "month": "Last 30 Days",
        "year": "Last 365 Days",
        "all": "All Time",
    }.get(p, p)


def _date_range(p: str) -> tuple[str, str]:
    today = datetime.date.today()
    end = today.isoformat()
    if p == "today":
        start = end
    elif p == "week":
        start = (today - datetime.timedelta(days=6)).isoformat()
    elif p == "month":
        start = (today - datetime.timedelta(days=29)).isoformat()
    elif p == "year":
        start = (today - datetime.timedelta(days=RETENTION_DAYS - 1)).isoformat()
    else:
        start = "2000-01-01"
    return start, end


# ── DB helpers ──────────────────────────────────────────────────────────────

async def _top_msg_channels(guild_id: int, start: str, end: str, n: int = 10):
    """Top N channels by message count."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT channel_id, SUM(count) as total FROM activity_messages "
            "WHERE guild_id = ? AND date BETWEEN ? AND ? "
            "GROUP BY channel_id ORDER BY total DESC LIMIT ?",
            (guild_id, start, end, n),
        ) as cur:
            return await cur.fetchall()


async def _top_vc_channels(guild_id: int, start: str, end: str, n: int = 10):
    """Top N voice channels by seconds."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT channel_id, SUM(seconds) as total FROM activity_voice "
            "WHERE guild_id = ? AND date BETWEEN ? AND ? "
            "GROUP BY channel_id ORDER BY total DESC LIMIT ?",
            (guild_id, start, end, n),
        ) as cur:
            return await cur.fetchall()


async def _top_msg_users(guild_id: int, start: str, end: str, n: int = 10):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT user_id, SUM(count) as total FROM activity_messages "
            "WHERE guild_id = ? AND date BETWEEN ? AND ? "
            "GROUP BY user_id ORDER BY total DESC LIMIT ?",
            (guild_id, start, end, n),
        ) as cur:
            return await cur.fetchall()


async def _top_vc_users(guild_id: int, start: str, end: str, n: int = 10):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT user_id, SUM(seconds) as total FROM activity_voice "
            "WHERE guild_id = ? AND date BETWEEN ? AND ? "
            "GROUP BY user_id ORDER BY total DESC LIMIT ?",
            (guild_id, start, end, n),
        ) as cur:
            return await cur.fetchall()


async def _user_totals(guild_id: int, user_id: int, start: str, end: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM activity_messages "
            "WHERE guild_id = ? AND user_id = ? AND date BETWEEN ? AND ?",
            (guild_id, user_id, start, end),
        ) as cur:
            msgs = (await cur.fetchone())[0]
        async with conn.execute(
            "SELECT COALESCE(SUM(seconds), 0) FROM activity_voice "
            "WHERE guild_id = ? AND user_id = ? AND date BETWEEN ? AND ?",
            (guild_id, user_id, start, end),
        ) as cur:
            voice = (await cur.fetchone())[0]
        async with conn.execute(
            "SELECT channel_id, SUM(count) as c FROM activity_messages "
            "WHERE guild_id = ? AND user_id = ? AND date BETWEEN ? AND ? "
            "GROUP BY channel_id ORDER BY c DESC LIMIT 10",
            (guild_id, user_id, start, end),
        ) as cur:
            msg_ch = await cur.fetchall()
        async with conn.execute(
            "SELECT channel_id, SUM(seconds) as s FROM activity_voice "
            "WHERE guild_id = ? AND user_id = ? AND date BETWEEN ? AND ? "
            "GROUP BY channel_id ORDER BY s DESC LIMIT 10",
            (guild_id, user_id, start, end),
        ) as cur:
            vc_ch = await cur.fetchall()
    return msgs, voice, msg_ch, vc_ch


# ── Cog ─────────────────────────────────────────────────────────────────────

class Tracker(commands.Cog):
    """Per-channel message & voice-time tracker with 365-day retention."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._vc_join: dict[int, dict[int, dict[int, float]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        self._last_cleanup: float = 0.0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The toggle has to gate the commands, not only the collecting.

        Every other cog with a module does this, and this one did not: the
        listeners that *collect* activity checked the module, but ``/activity``
        itself did not, so a server that turned the tracker off kept every
        command — reading rows nothing was writing any more. Caught by the
        invariant test that every command a server loses is one the bot would
        refuse (``tests/test_command_scope.py``), which only holds if the gate
        the registry declares is really enforced.
        """
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "tracker"):
                await interaction.response.send_message(
                    "⚠️ The **Activity Tracker** is currently **turned off** on "
                    "this server.\nAn administrator can re-enable it in the "
                    "[Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                    ephemeral=True,
                )
                return False
        return True

    @commands.Cog.listener()
    async def on_ready(self):
        # Catch members already in VC on startup
        now = time.time()
        tracked = 0
        for guild in self.bot.guilds:
            if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(
                guild.id, "tracker"
            ):
                continue
            for vc in guild.voice_channels:
                for m in vc.members:
                    if m.bot or m.voice is None:
                        continue
                    self._vc_join[guild.id][m.id][vc.id] = now
                    tracked += 1
        if tracked:
            print(f"[TRACKER] Startup VC scan: tracking {tracked} member(s) already in voice.", flush=True)
        if not self._vc_flush_loop.is_running():
            self._vc_flush_loop.start()

    def cog_unload(self):
        self._vc_flush_loop.cancel()

    # ── Channel tracking for new channels ─────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        """Ensure new channels are tracked when created."""
        if not channel.guild:
            return
        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(
            channel.guild.id, "tracker"
        ):
            return
        print(f"[TRACKER] New channel created: #{channel.name} ({channel.id}) - tracking enabled", flush=True)

    # ── Message tracking ────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(
            message.guild.id, "tracker"
        ):
            return
        try:
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    "INSERT INTO activity_messages (guild_id, user_id, channel_id, date, count) "
                    "VALUES (?, ?, ?, ?, 1) "
                    "ON CONFLICT(guild_id, user_id, channel_id, date) "
                    "DO UPDATE SET count = count + 1",
                    (message.guild.id, message.author.id, message.channel.id, _today()),
                )
                await conn.commit()
        except Exception as e:
            print(f"[TRACKER] msg error: {e}", flush=True)

    # ── Voice tracking ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or not member.guild:
            return
        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(
            member.guild.id, "tracker"
        ):
            return
        gid = member.guild.id
        uid = member.id
        now = time.time()

        # Join
        if after.channel and (not before.channel or before.channel.id != after.channel.id):
            self._vc_join[gid][uid][after.channel.id] = now
            print(f"[TRACKER] VC JOIN: {member.display_name} -> #{after.channel.name}", flush=True)

        # Leave
        if before.channel and (not after.channel or before.channel.id != after.channel.id):
            jts = None
            if gid in self._vc_join and uid in self._vc_join[gid]:
                jts = self._vc_join[gid][uid].pop(before.channel.id, None)
                # Clean up empty entries to prevent memory leak
                if not self._vc_join[gid][uid]:
                    del self._vc_join[gid][uid]
                if not self._vc_join[gid]:
                    del self._vc_join[gid]
            if jts is not None:
                dur = max(0, int(now - jts))
                if dur > 0:
                    print(f"[TRACKER] VC LEAVE: {member.display_name} left #{before.channel.name} ({dur}s)", flush=True)
                    await self._write_voice(gid, uid, before.channel.id, dur)

    async def _write_voice(self, gid, uid, cid, secs):
        try:
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    "INSERT INTO activity_voice (guild_id, user_id, channel_id, date, seconds) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, user_id, channel_id, date) "
                    "DO UPDATE SET seconds = seconds + ?",
                    (gid, uid, cid, _today(), secs, secs),
                )
                await conn.commit()
        except Exception as e:
            print(f"[TRACKER] voice write error: {e}", flush=True)

    # ── Background flush loop ───────────────────────────────────────────────

    @tasks.loop(seconds=VOICE_FLUSH_INTERVAL)
    async def _vc_flush_loop(self):
        now = time.time()
        flushed = 0
        for gid, users in list(self._vc_join.items()):
            for uid, chs in list(users.items()):
                for cid, jts in list(chs.items()):
                    elapsed = int(now - jts)
                    if elapsed >= 60:
                        await self._write_voice(gid, uid, cid, elapsed)
                        flushed += 1
                        guild_map = self._vc_join.get(gid)
                        user_map = guild_map.get(uid) if guild_map else None
                        cur = user_map.get(cid) if user_map else None
                        if cur is not None and cur == jts:
                            user_map[cid] = now
        if flushed:
            print(f"[TRACKER] Flushed {flushed} VC session(s).", flush=True)

        if now - self._last_cleanup > CLEANUP_INTERVAL:
            self._last_cleanup = now
            cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
            try:
                async with aiosqlite.connect(DB_PATH) as conn:
                    r1 = await conn.execute("DELETE FROM activity_messages WHERE date < ?", (cutoff,))
                    r2 = await conn.execute("DELETE FROM activity_voice WHERE date < ?", (cutoff,))
                    await conn.commit()
                    print(f"[TRACKER] Cleanup: {r1.rowcount} msg + {r2.rowcount} voice rows removed.", flush=True)
            except Exception as e:
                print(f"[TRACKER] Cleanup error: {e}", flush=True)

    @_vc_flush_loop.before_loop
    async def _before_flush(self):
        await self.bot.wait_until_ready()

    # ════════════════════════════════════════════════════════════════════════
    #  SLASH COMMANDS
    # ════════════════════════════════════════════════════════════════════════

    # One command with three views, which is what the tracker always was. Three
    # top-level commands cost Discord slots the bot does not have, and asking for
    # "activity" and getting a picker is how it reads anyway.
    activity_group = app_commands.Group(
        name="activity",
        description="Messages and voice time, for a member, a channel or the server.",
    )

    @activity_group.command(name="top", description="Server activity: top channels by messages & voice time!")
    @app_commands.describe(period="Time range")
    @app_commands.choices(period=[
        app_commands.Choice(name="Today", value="today"),
        app_commands.Choice(name="This Week", value="week"),
        app_commands.Choice(name="This Month", value="month"),
        app_commands.Choice(name="This Year", value="year"),
        app_commands.Choice(name="All Time", value="all"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def activity_top(self, interaction: discord.Interaction, period: app_commands.Choice[str] = None):
        if not interaction.guild:
            return await interaction.response.send_message("Server only!", ephemeral=True)

        pv = period.value if period else "month"
        start, end = _date_range(pv)
        await interaction.response.defer()

        msg_chs = await _top_msg_channels(interaction.guild.id, start, end, 10)
        vc_chs = await _top_vc_channels(interaction.guild.id, start, end, 10)
        msg_users = await _top_msg_users(interaction.guild.id, start, end, 10)
        vc_users = await _top_vc_users(interaction.guild.id, start, end, 10)

        # ── Build embed ─────────────────────────────────────────────────
        icon = interaction.guild.icon.url if interaction.guild.icon else None
        embed = discord.Embed(
            title=f"📊 {interaction.guild.name}",
            description=f"**🏆 Top Statistics**\n*{_period_label(pv)} — Timezone: UTC*",
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        if icon:
            embed.set_thumbnail(url=icon)

        # ── Messages section ────────────────────────────────────────────
        # Left: top users
        user_lines = []
        if msg_users:
            all_uids = [u for u, _ in msg_users]
            names = await style.display_names(interaction.guild, all_uids, client=interaction.client)
            for i, ((uid, cnt), name) in enumerate(zip(msg_users, names)):
                medal = ["👑", "💎", "⭐"][i] if i < 3 else f"  {i+1}"
                user_lines.append(f"`{medal}` **{name}** — `{_fmt_count(cnt)}`")
        embed.add_field(
            name="💬 Top Users (Messages)",
            value="\n".join(user_lines) or "*No data*",
            inline=True,
        )
        # Right: top channels
        ch_lines = []
        if msg_users:  # only if there's data at all
            for i, (cid, cnt) in enumerate(msg_chs):
                ch = interaction.guild.get_channel(cid)
                name = ch.mention if ch else f"<#{cid}>"
                medal = ["👑", "💎", "⭐"][i] if i < 3 else f"  {i+1}"
                ch_lines.append(f"`{medal}` {name} — `{_fmt_count(cnt)}`")
        embed.add_field(
            name="💬 Top Channels (Messages)",
            value="\n".join(ch_lines) or "*No data*",
            inline=True,
        )

        # ── Voice section ───────────────────────────────────────────────
        vc_user_lines = []
        if vc_users:
            all_uids = [u for u, _ in vc_users]
            names = await style.display_names(interaction.guild, all_uids, client=self.bot)
            for i, ((uid, secs), name) in enumerate(zip(vc_users, names)):
                medal = ["👑", "💎", "⭐"][i] if i < 3 else f"  {i+1}"
                vc_user_lines.append(f"`{medal}` **{name}** — `{_fmt_voice(secs)}`")
        embed.add_field(
            name="🔊 Top Users (Voice)",
            value="\n".join(vc_user_lines) or "*No data*",
            inline=True,
        )
        vc_ch_lines = []
        if vc_chs:
            for i, (cid, secs) in enumerate(vc_chs):
                ch = interaction.guild.get_channel(cid)
                name = ch.name if ch else f"Channel {cid}"
                medal = ["👑", "💎", "⭐"][i] if i < 3 else f"  {i+1}"
                vc_ch_lines.append(f"`{medal}` 🔊 {name} — `{_fmt_voice(secs)}`")
        embed.add_field(
            name="🔊 Top Channels (Voice)",
            value="\n".join(vc_ch_lines) or "*No data*",
            inline=True,
        )

        embed.set_footer(text=f"Activity Tracker • {_period_label(pv)}")
        await style.send_card(interaction, embed=embed)

    @activity_top.error
    async def activity_top_error(self, interaction: discord.Interaction, error):
        # A cog's error handler runs *before* the tree's, so raising here would
        # abort the chain: the tree's handler never runs and the interaction is
        # never answered — which Discord shows as "the application did not
        # respond". Anything that is not the permission case is left to it.
        if isinstance(error, app_commands.MissingPermissions):
            await style.send_card(
                interaction,
                content="❌ You need **Administrator** permission to use this command.",
                ephemeral=True,
            )

    @activity_group.command(name="me", description="Your personal activity stats on this server!")
    @app_commands.describe(member="Check another member", period="Time range")
    @app_commands.choices(period=[
        app_commands.Choice(name="Today", value="today"),
        app_commands.Choice(name="This Week", value="week"),
        app_commands.Choice(name="This Month", value="month"),
        app_commands.Choice(name="This Year", value="year"),
        app_commands.Choice(name="All Time", value="all"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def activity(self, interaction: discord.Interaction, member: discord.Member = None, period: app_commands.Choice[str] = None):
        if not interaction.guild:
            return await interaction.response.send_message("Server only!", ephemeral=True)
        target = member or interaction.user
        if target.bot:
            return await interaction.response.send_message("Bots don't have stats!", ephemeral=True)
        pv = period.value if period else "year"
        start, end = _date_range(pv)
        await interaction.response.defer()

        msgs, voice, msg_chs, vc_chs = await _user_totals(interaction.guild.id, target.id, start, end)

        embed = discord.Embed(
            title=f"📊 {target.display_name}'s Activity",
            description=f"*{_period_label(pv)}*",
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="💬 Total Messages", value=f"`{_fmt_count(msgs)}`", inline=True)
        embed.add_field(name="🔊 Total Voice", value=f"`{_fmt_voice(voice)}`", inline=True)

        if msg_chs:
            lines = []
            for i, (cid, cnt) in enumerate(msg_chs[:5]):
                ch = interaction.guild.get_channel(cid)
                name = ch.mention if ch else f"<#{cid}>"
                lines.append(f"**{i+1}.** {name} — `{_fmt_count(cnt)}`")
            embed.add_field(name="💬 Messages by Channel", value="\n".join(lines), inline=False)

        if vc_chs:
            lines = []
            for i, (cid, secs) in enumerate(vc_chs[:5]):
                ch = interaction.guild.get_channel(cid)
                name = ch.name if ch else f"Channel {cid}"
                lines.append(f"**{i+1}.** 🔊 {name} — `{_fmt_voice(secs)}`")
            embed.add_field(name="🔊 Voice by Channel", value="\n".join(lines), inline=False)

        embed.set_footer(text=f"{interaction.guild.name} • {_period_label(pv)}")
        await style.send_card(interaction, embed=embed)

    @activity.error
    async def activity_error(self, interaction: discord.Interaction, error):
        # Never re-raise: see ``activity_top_error`` above — a raise here skips
        # the tree's own handler and leaves the command unanswered.
        if isinstance(error, app_commands.MissingPermissions):
            await style.send_card(
                interaction,
                content="❌ You need **Administrator** permission to use this command.",
                ephemeral=True,
            )

    @activity_group.command(name="channel", description="Activity stats for a specific channel!")
    @app_commands.describe(channel="Channel to check", period="Time range")
    @app_commands.choices(period=[
        app_commands.Choice(name="Today", value="today"),
        app_commands.Choice(name="This Week", value="week"),
        app_commands.Choice(name="This Month", value="month"),
        app_commands.Choice(name="This Year", value="year"),
        app_commands.Choice(name="All Time", value="all"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def activity_channel(self, interaction: discord.Interaction, channel: discord.TextChannel = None, period: app_commands.Choice[str] = None):
        if not interaction.guild:
            return await interaction.response.send_message("Server only!", ephemeral=True)
        ch = channel or interaction.channel
        pv = period.value if period else "month"
        start, end = _date_range(pv)
        await interaction.response.defer()

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT user_id, SUM(count) as c FROM activity_messages "
                "WHERE guild_id = ? AND channel_id = ? AND date BETWEEN ? AND ? "
                "GROUP BY user_id ORDER BY c DESC LIMIT 10",
                (interaction.guild.id, ch.id, start, end),
            ) as cur:
                top = await cur.fetchall()
            async with conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM activity_messages "
                "WHERE guild_id = ? AND channel_id = ? AND date BETWEEN ? AND ?",
                (interaction.guild.id, ch.id, start, end),
            ) as cur:
                total = (await cur.fetchone())[0]

        embed = discord.Embed(
            title=f"📊 {ch.name} Activity",
            description=f"*{_period_label(pv)}*",
            color=0x2ECC71,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="💬 Total Messages", value=f"`{_fmt_count(total)}`", inline=True)

        if top:
            uids = [u for u, _ in top]
            names = await style.display_names(interaction.guild, uids, client=self.bot)
            lines = []
            for i, ((uid, cnt), name) in enumerate(zip(top, names)):
                medal = ["👑", "💎", "⭐"][i] if i < 3 else f"  {i+1}"
                lines.append(f"`{medal}` **{name}** — `{_fmt_count(cnt)}`")
            embed.add_field(name="💬 Top Chatters", value="\n".join(lines), inline=False)

        embed.set_footer(text=f"{interaction.guild.name}")
        await style.send_card(interaction, embed=embed)

    @activity_channel.error
    async def activity_channel_error(self, interaction: discord.Interaction, error):
        # Never re-raise: see ``activity_top_error`` above — a raise here skips
        # the tree's own handler and leaves the command unanswered.
        if isinstance(error, app_commands.MissingPermissions):
            await style.send_card(
                interaction,
                content="❌ You need **Administrator** permission to use this command.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Tracker(bot))
