"""Activity Tracker — per-day, per-channel message and voice time tracking.

Tracks every message sent and every voice-channel second spent, broken down by
day and channel, with a 365-day rolling window. The UI is modelled after
Statbot: a V2 card with the server icon, side-by-side top-3 columns for
messages and voice, and a period selector.
"""

import datetime
import time
from collections import defaultdict

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import db, style

DB_PATH = db.DB_PATH

# How many days of data to retain.
RETENTION_DAYS = 365

# How often (seconds) to flush voice time for users still in VC and clean old data.
VOICE_FLUSH_INTERVAL = 120
CLEANUP_INTERVAL = 6 * 3600

# Number of rows shown in the top-N leaderboard sections.
TOP_N = 3


def _today_str() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def _format_count(n: int) -> str:
    """1234 -> '1,234'."""
    return f"{n:,}"


def _format_voice(seconds: int) -> str:
    """12345 -> '3.43 h'."""
    if seconds < 60:
        return f"{seconds}s"
    hours = seconds / 3600
    if hours >= 1:
        return f"{hours:.2f} h".rstrip("0").rstrip(".")
    m = seconds // 60
    s = seconds % 60
    return f"{m}m {s}s" if s else f"{m}m"


def _period_label(period: str) -> str:
    return {
        "today": "Today",
        "week": "Last 7 Days",
        "month": "Last 30 Days",
        "year": "Last 365 Days",
        "all": "All Time",
    }.get(period, period)


def _date_range(period: str) -> tuple[str, str]:
    today = datetime.date.today()
    end = today.isoformat()
    if period == "today":
        start = end
    elif period == "week":
        start = (today - datetime.timedelta(days=6)).isoformat()
    elif period == "month":
        start = (today - datetime.timedelta(days=29)).isoformat()
    elif period == "year":
        start = (today - datetime.timedelta(days=RETENTION_DAYS - 1)).isoformat()
    else:
        start = "2000-01-01"
    return start, end


class Tracker(commands.Cog):
    """Per-day, per-channel message & voice activity tracker."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # {guild_id: {user_id: {channel_id: join_timestamp}}}
        self._vc_join: dict[int, dict[int, dict[int, float]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        self._last_cleanup: float = 0.0

    # ── Lifecycle ───────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._vc_flush_loop.is_running():
            self._vc_flush_loop.start()

    def cog_unload(self):
        self._vc_flush_loop.cancel()

    # ── Message Tracking ────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(
            message.guild.id, "tracker"
        ):
            return

        today = _today_str()
        try:
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    """INSERT INTO activity_messages (guild_id, user_id, channel_id, date, count)
                       VALUES (?, ?, ?, ?, 1)
                       ON CONFLICT(guild_id, user_id, channel_id, date)
                       DO UPDATE SET count = count + 1""",
                    (message.guild.id, message.author.id, message.channel.id, today),
                )
                await conn.commit()
        except Exception as e:
            print(f"[TRACKER] Message tracking error: {e}", flush=True)

    # ── Voice Tracking ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot or not member.guild:
            return
        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(
            member.guild.id, "tracker"
        ):
            return

        guild_id = member.guild.id
        user_id = member.id
        now = time.time()

        # User joined a VC
        if after.channel is not None and (
            before.channel is None or before.channel.id != after.channel.id
        ):
            self._vc_join[guild_id][user_id][after.channel.id] = now

        # User left a VC
        if before.channel is not None and (
            after.channel is None or before.channel.id != after.channel.id
        ):
            join_ts = (
                self._vc_join.get(guild_id, {})
                .get(user_id, {})
                .pop(before.channel.id, None)
            )
            if join_ts is not None:
                duration = max(0, int(now - join_ts))
                if duration > 0:
                    await self._flush_voice(
                        guild_id, user_id, before.channel.id, duration
                    )

    async def _flush_voice(
        self, guild_id: int, user_id: int, channel_id: int, seconds: int
    ) -> None:
        today = _today_str()
        try:
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    """INSERT INTO activity_voice (guild_id, user_id, channel_id, date, seconds)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(guild_id, user_id, channel_id, date)
                       DO UPDATE SET seconds = seconds + ?""",
                    (guild_id, user_id, channel_id, today, seconds, seconds),
                )
                await conn.commit()
        except Exception as e:
            print(f"[TRACKER] Voice flush error: {e}", flush=True)

    # ── Background loop ─────────────────────────────────────────────────────

    @tasks.loop(seconds=VOICE_FLUSH_INTERVAL)
    async def _vc_flush_loop(self):
        now = time.time()
        for guild_id, users in list(self._vc_join.items()):
            for user_id, channels in list(users.items()):
                for channel_id, join_ts in list(channels.items()):
                    elapsed = int(now - join_ts)
                    if elapsed >= 60:
                        await self._flush_voice(
                            guild_id, user_id, channel_id, elapsed
                        )
                        current_ts = (
                            self._vc_join.get(guild_id, {})
                            .get(user_id, {})
                            .get(channel_id)
                        )
                        if current_ts is not None and current_ts == join_ts:
                            self._vc_join[guild_id][user_id][channel_id] = now

        if now - self._last_cleanup > CLEANUP_INTERVAL:
            self._last_cleanup = now
            cutoff = (
                datetime.datetime.utcnow() - datetime.timedelta(days=RETENTION_DAYS)
            ).strftime("%Y-%m-%d")
            try:
                async with aiosqlite.connect(DB_PATH) as conn:
                    r1 = await conn.execute(
                        "DELETE FROM activity_messages WHERE date < ?", (cutoff,)
                    )
                    r2 = await conn.execute(
                        "DELETE FROM activity_voice WHERE date < ?", (cutoff,)
                    )
                    await conn.commit()
                    print(
                        f"[TRACKER] Cleanup: removed {r1.rowcount} msg rows, "
                        f"{r2.rowcount} voice rows before {cutoff}.",
                        flush=True,
                    )
            except Exception as e:
                print(f"[TRACKER] Cleanup error: {e}", flush=True)

    @_vc_flush_loop.before_loop
    async def before_vc_flush_loop(self):
        await self.bot.wait_until_ready()

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def _query_top_users(
        self, guild_id: int, col: str, start: str, end: str, limit: int = TOP_N
    ) -> list[tuple[int, int]]:
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                f"SELECT user_id, SUM({col}) as total "
                "FROM activity_{table} "
                "WHERE guild_id = ? AND date BETWEEN ? AND ? "
                "GROUP BY user_id ORDER BY total DESC LIMIT ?",
                (guild_id, start, end, limit),
            ) as cur:
                return await cur.fetchall()

    async def _query_top_channels(
        self, guild_id: int, col: str, start: str, end: str, limit: int = TOP_N
    ) -> list[tuple[int, int]]:
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                f"SELECT channel_id, SUM({col}) as total "
                "FROM activity_{table} "
                "WHERE guild_id = ? AND date BETWEEN ? AND ? "
                "GROUP BY channel_id ORDER BY total DESC LIMIT ?",
                (guild_id, start, end, limit),
            ) as cur:
                return await cur.fetchall()

    async def _query_user_totals(
        self, guild_id: int, user_id: int, start: str, end: str
    ) -> dict:
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
            # Top channels by messages
            async with conn.execute(
                "SELECT channel_id, SUM(count) as c FROM activity_messages "
                "WHERE guild_id = ? AND user_id = ? AND date BETWEEN ? AND ? "
                "GROUP BY channel_id ORDER BY c DESC LIMIT 5",
                (guild_id, user_id, start, end),
            ) as cur:
                top_msg_ch = await cur.fetchall()
            # Top channels by voice
            async with conn.execute(
                "SELECT channel_id, SUM(seconds) as s FROM activity_voice "
                "WHERE guild_id = ? AND user_id = ? AND date BETWEEN ? AND ? "
                "GROUP BY channel_id ORDER BY s DESC LIMIT 5",
                (guild_id, user_id, start, end),
            ) as cur:
                top_vc_ch = await cur.fetchall()
        return {
            "messages": msgs,
            "voice_seconds": voice,
            "top_msg_channels": top_msg_ch,
            "top_vc_channels": top_vc_ch,
        }

    # ── Build the Statbot-style card ────────────────────────────────────────

    async def _build_top_card(
        self, guild: discord.Guild, period: str
    ) -> discord.ui.LayoutView:
        """Build the Statbot-style top statistics card."""
        start, end = _date_range(period)

        # Query top users & channels
        top_msg_users = await self._query_top_users(
            guild.id, "count", start, end, TOP_N
        )
        top_msg_ch = await self._query_top_channels(
            guild.id, "count", start, end, TOP_N
        )
        top_vc_users = await self._query_top_users(
            guild.id, "seconds", start, end, TOP_N
        )
        top_vc_ch = await self._query_top_channels(
            guild.id, "seconds", start, end, TOP_N
        )

        # Resolve names
        all_ids = set()
        for uid, _ in top_msg_users + top_vc_users:
            all_ids.add(uid)
        names = {}
        if all_ids:
            resolved = await style.display_names(
                guild, list(all_ids), client=self.bot
            )
            for uid, name in zip(all_ids, resolved):
                names[uid] = name

        def resolve_user(uid: int) -> str:
            return names.get(uid, f"User {uid}")

        def resolve_channel(cid: int) -> str:
            ch = guild.get_channel(cid)
            return ch.mention if ch else f"<#{cid}>"

        def rank_row(
            idx: int, name: str, value: str, *, bold: bool = False
        ) -> str:
            star = "⭐" if idx == 0 else "  "
            return f"{star} **{idx + 1}.** {name}  ` {value} `"

        # ── Build the card ──────────────────────────────────────────────
        children = []

        # Header: server icon + title
        guild_icon = guild.icon.url if guild.icon else None
        if guild_icon:
            thumb = discord.ui.Thumbnail(guild_icon)
            children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(f"## 📊 {guild.name}"),
                    accessory=thumb,
                )
            )
        else:
            children.append(discord.ui.TextDisplay(f"## 📊 {guild.name}"))

        children.append(
            discord.ui.TextDisplay("### 🏆 Top Statistics")
        )

        # ── # Messages section ──────────────────────────────────────────
        children.append(discord.ui.Separator())
        children.append(discord.ui.TextDisplay("### 💬 Messages"))

        # Build message leaderboard rows
        msg_lines_left = []
        msg_lines_right = []
        for i in range(TOP_N):
            if i < len(top_msg_users):
                uid, count = top_msg_users[i]
                msg_lines_left.append(
                    rank_row(i, resolve_user(uid), _format_count(count))
                )
            else:
                msg_lines_left.append(f"  {i + 1}. *—*")

            if i < len(top_msg_ch):
                cid, count = top_msg_ch[i]
                msg_lines_right.append(
                    rank_row(i, resolve_channel(cid), _format_count(count))
                )
            else:
                msg_lines_right.append(f"  {i + 1}. *—*")

        # Combine left and right columns using a monospace table
        left_block = "\n".join(msg_lines_left)
        right_block = "\n".join(msg_lines_right)
        children.append(
            discord.ui.TextDisplay(
                f"**By User**\n{left_block}\n\n"
                f"**By Channel**\n{right_block}"
            )
        )

        # ── Voice Activity section ──────────────────────────────────────
        children.append(discord.ui.Separator())
        children.append(discord.ui.TextDisplay("### 🔊 Voice Activity"))

        vc_lines_left = []
        vc_lines_right = []
        for i in range(TOP_N):
            if i < len(top_vc_users):
                uid, secs = top_vc_users[i]
                vc_lines_left.append(
                    rank_row(i, resolve_user(uid), _format_voice(secs))
                )
            else:
                vc_lines_left.append(f"  {i + 1}. *—*")

            if i < len(top_vc_ch):
                cid, secs = top_vc_ch[i]
                vc_lines_right.append(
                    rank_row(i, resolve_channel(cid), _format_voice(secs))
                )
            else:
                vc_lines_right.append(f"  {i + 1}. *—*")

        vc_left_block = "\n".join(vc_lines_left)
        vc_right_block = "\n".join(vc_lines_right)
        children.append(
            discord.ui.TextDisplay(
                f"**By User**\n{vc_left_block}\n\n"
                f"**By Channel**\n{vc_right_block}"
            )
        )

        # Footer
        children.append(discord.ui.Separator())
        children.append(
            discord.ui.TextDisplay(
                f"-# Server Lookback: {_period_label(period)} — "
                f"Timezone: UTC"
            )
        )

        view = discord.ui.LayoutView(timeout=180)
        view.add_item(
            discord.ui.Container(*children, accent_color=0x5865F2)
        )
        return view

    # ── Slash Commands ──────────────────────────────────────────────────────

    @app_commands.command(
        name="activity-top",
        description="See the most active members & channels on this server!",
    )
    @app_commands.describe(period="Time range to view")
    @app_commands.choices(
        period=[
            app_commands.Choice(name="📅 Today", value="today"),
            app_commands.Choice(name="📆 This Week", value="week"),
            app_commands.Choice(name="🗓️ This Month", value="month"),
            app_commands.Choice(name="📊 This Year (365d)", value="year"),
            app_commands.Choice(name="📈 All Time", value="all"),
        ],
    )
    async def activity_top(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This command only works in a server!", ephemeral=True
            )

        period_val = period.value if period else "year"
        await interaction.response.defer()
        view = await self._build_top_card(interaction.guild, period_val)
        await interaction.followup.send(view=view, allowed_mentions=style.NO_PINGS)

    @app_commands.command(
        name="activity",
        description="View your or another member's activity stats!",
    )
    @app_commands.describe(
        member="Check another member's activity",
        period="Time range to view",
    )
    @app_commands.choices(
        period=[
            app_commands.Choice(name="📅 Today", value="today"),
            app_commands.Choice(name="📆 This Week", value="week"),
            app_commands.Choice(name="🗓️ This Month", value="month"),
            app_commands.Choice(name="📊 This Year (365d)", value="year"),
            app_commands.Choice(name="📈 All Time", value="all"),
        ]
    )
    async def activity(
        self,
        interaction: discord.Interaction,
        member: discord.Member = None,
        period: app_commands.Choice[str] = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This command only works in a server!", ephemeral=True
            )

        target = member or interaction.user
        if target.bot:
            return await interaction.response.send_message(
                "❌ Bots don't have activity stats!", ephemeral=True
            )

        period_val = period.value if period else "year"
        await interaction.response.defer()

        start, end = _date_range(period_val)
        data = await self._query_user_totals(interaction.guild.id, target.id, start, end)

        # Build personal stats card
        children = []

        # Header
        children.append(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    f"## 📊 {target.display_name}'s Activity"
                ),
                accessory=discord.ui.Thumbnail(target.display_avatar.url),
            )
        )

        children.append(
            discord.ui.TextDisplay(f"### Period: {_period_label(period_val)}")
        )

        # Stats
        children.append(discord.ui.Separator())
        msg_count = data["messages"]
        vc_secs = data["voice_seconds"]
        children.append(
            discord.ui.TextDisplay(
                f"**💬 Messages:** `{_format_count(msg_count)}`\n"
                f"**🔊 Voice Time:** `{_format_voice(vc_secs)}`"
            )
        )

        # Top channels by messages
        if data["top_msg_channels"]:
            lines = []
            for i, (cid, count) in enumerate(data["top_msg_channels"]):
                ch = interaction.guild.get_channel(cid)
                name = ch.mention if ch else f"<#{cid}>"
                lines.append(f"**{i + 1}.** {name}  `{_format_count(count)} msgs`")
            children.append(discord.ui.Separator())
            children.append(
                discord.ui.TextDisplay(
                    "### 💬 Top Channels (Messages)\n" + "\n".join(lines)
                )
            )

        # Top channels by voice
        if data["top_vc_channels"]:
            lines = []
            for i, (cid, secs) in enumerate(data["top_vc_channels"]):
                ch = interaction.guild.get_channel(cid)
                name = ch.mention if ch else f"<#{cid}>"
                lines.append(f"**{i + 1}.** {name}  `{_format_voice(secs)}`")
            children.append(discord.ui.Separator())
            children.append(
                discord.ui.TextDisplay(
                    "### 🔊 Top Channels (Voice)\n" + "\n".join(lines)
                )
            )

        # Footer
        children.append(discord.ui.Separator())
        children.append(
            discord.ui.TextDisplay(
                f"-# {interaction.guild.name} · 365-day rolling window"
            )
        )

        view = discord.ui.LayoutView(timeout=180)
        view.add_item(
            discord.ui.Container(*children, accent_color=0x5865F2)
        )
        await interaction.followup.send(view=view, allowed_mentions=style.NO_PINGS)

    @app_commands.command(
        name="activity-channel",
        description="See activity stats for a specific channel!",
    )
    @app_commands.describe(
        channel="The channel to check",
        period="Time range to view",
    )
    @app_commands.choices(
        period=[
            app_commands.Choice(name="📅 Today", value="today"),
            app_commands.Choice(name="📆 This Week", value="week"),
            app_commands.Choice(name="🗓️ This Month", value="month"),
            app_commands.Choice(name="📊 This Year (365d)", value="year"),
            app_commands.Choice(name="📈 All Time", value="all"),
        ]
    )
    async def activity_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel = None,
        period: app_commands.Choice[str] = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This command only works in a server!", ephemeral=True
            )

        target_channel = channel or interaction.channel
        period_val = period.value if period else "month"
        await interaction.response.defer()

        start, end = _date_range(period_val)

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT user_id, SUM(count) as c FROM activity_messages "
                "WHERE guild_id = ? AND channel_id = ? AND date BETWEEN ? AND ? "
                "GROUP BY user_id ORDER BY c DESC LIMIT 10",
                (interaction.guild.id, target_channel.id, start, end),
            ) as cur:
                top_chatters = await cur.fetchall()
            async with conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM activity_messages "
                "WHERE guild_id = ? AND channel_id = ? AND date BETWEEN ? AND ?",
                (interaction.guild.id, target_channel.id, start, end),
            ) as cur:
                total_msgs = (await cur.fetchone())[0]
            async with conn.execute(
                "SELECT COALESCE(SUM(seconds), 0) FROM activity_voice "
                "WHERE guild_id = ? AND channel_id = ? AND date BETWEEN ? AND ?",
                (interaction.guild.id, target_channel.id, start, end),
            ) as cur:
                total_voice = (await cur.fetchone())[0]
            async with conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM activity_messages "
                "WHERE guild_id = ? AND channel_id = ? AND date BETWEEN ? AND ?",
                (interaction.guild.id, target_channel.id, start, end),
            ) as cur:
                unique_users = (await cur.fetchone())[0]

        children = []
        children.append(
            discord.ui.TextDisplay(
                f"## 📊 {target_channel.mention} Activity"
            )
        )
        children.append(
            discord.ui.TextDisplay(f"### Period: {_period_label(period_val)}")
        )

        children.append(discord.ui.Separator())
        children.append(
            discord.ui.TextDisplay(
                f"**💬 Total Messages:** `{_format_count(total_msgs)}`\n"
                f"**🔊 Total Voice:** `{_format_voice(total_voice)}`\n"
                f"**👥 Unique Users:** `{unique_users}`"
            )
        )

        if top_chatters:
            user_ids = [row[0] for row in top_chatters]
            resolved = await style.display_names(
                interaction.guild, user_ids, client=self.bot
            )
            lines = []
            for i, ((uid, count), name) in enumerate(
                zip(top_chatters, resolved)
            ):
                medal = ["👑", "💎", "⭐"][i] if i < 3 else f"#{i+1}"
                lines.append(
                    f"{medal} **{name}**  `{_format_count(count)} msgs`"
                )
            children.append(discord.ui.Separator())
            children.append(
                discord.ui.TextDisplay(
                    "### 💬 Top Chatters\n" + "\n".join(lines)
                )
            )

        children.append(discord.ui.Separator())
        children.append(
            discord.ui.TextDisplay(
                f"-# {interaction.guild.name} · {_period_label(period_val)}"
            )
        )

        view = discord.ui.LayoutView(timeout=180)
        view.add_item(
            discord.ui.Container(*children, accent_color=0x2ECC71)
        )
        await interaction.followup.send(view=view, allowed_mentions=style.NO_PINGS)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tracker(bot))
