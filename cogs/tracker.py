"""Activity Tracker — per-day, per-channel message and voice time tracking.

Tracks every message sent and every voice-channel second spent, broken down by
day and channel, with a 365-day rolling window. Provides slash commands for
personal stats, channel breakdowns, and server-wide leaderboards.

Design decisions:
- **Per-day aggregation** instead of per-event logging keeps the database small
  (thousands of rows per user per year, not millions) while still giving exact
  daily breakdowns.
- **Voice time is measured in seconds** via join/leave timestamps stored
  in-memory and flushed to the DB on leave (and periodically for still-connected
  users), giving sub-minute accuracy.
- **365-day cleanup** runs every 6 hours, deleting rows older than the window.
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
VOICE_FLUSH_INTERVAL = 120  # every 2 minutes
CLEANUP_INTERVAL = 6 * 3600  # every 6 hours


def _today_str() -> str:
    """UTC date string YYYY-MM-DD for row keys."""
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


class Tracker(commands.Cog):
    """Per-day, per-channel message & voice activity tracker with 365-day retention."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # {guild_id: {user_id: {channel_id: join_timestamp}}}
        self._vc_join: dict[int, dict[int, dict[int, float]]] = defaultdict(lambda: defaultdict(dict))
        # Track when the cleanup last ran (epoch seconds)
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
        """Record every non-bot message toward the daily per-channel count."""
        if message.author.bot or not message.guild:
            return
        # Respect the leveling module toggle — if someone turned off the bot's
        # activity tracking for a guild, don't count.
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
        """Track join (start timer) and leave (flush exact seconds)."""
        if member.bot or not member.guild:
            return
        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(
            member.guild.id, "tracker"
        ):
            return

        guild_id = member.guild.id
        user_id = member.id
        now = time.time()

        # ── User joined a VC (or moved into one) ──
        if after.channel is not None and (before.channel is None or before.channel.id != after.channel.id):
            self._vc_join[guild_id][user_id][after.channel.id] = now

        # ── User left a VC (or moved out of one) ──
        if before.channel is not None and (after.channel is None or before.channel.id != after.channel.id):
            join_ts = self._vc_join.get(guild_id, {}).get(user_id, {}).pop(before.channel.id, None)
            if join_ts is not None:
                duration = max(0, int(now - join_ts))
                if duration > 0:
                    await self._flush_voice(guild_id, user_id, before.channel.id, duration)

    async def _flush_voice(
        self, guild_id: int, user_id: int, channel_id: int, seconds: int
    ) -> None:
        """Write accumulated voice seconds to the daily row."""
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

    # ── Background: flush still-connected users + cleanup old data ──────────

    @tasks.loop(seconds=VOICE_FLUSH_INTERVAL)
    async def _vc_flush_loop(self):
        """Periodically flush voice time for users still in VC and run cleanup."""
        now = time.time()
        today = _today_str()

        # Flush every tracked user still in voice
        for guild_id, users in list(self._vc_join.items()):
            for user_id, channels in list(users.items()):
                for channel_id, join_ts in list(channels.items()):
                    elapsed = int(now - join_ts)
                    if elapsed >= 60:  # only flush if >= 1 minute accumulated
                        await self._flush_voice(guild_id, user_id, channel_id, elapsed)
                        # Re-check after the await: the leave handler may have
                        # popped this entry during the DB write, which would mean
                        # the leave event already flushed and removed the timer.
                        # Resetting unconditionally would re-create a phantom entry
                        # that never gets cleaned up and double-counts every cycle.
                        current_ts = (
                            self._vc_join.get(guild_id, {})
                            .get(user_id, {})
                            .get(channel_id)
                        )
                        if current_ts is not None and current_ts == join_ts:
                            self._vc_join[guild_id][user_id][channel_id] = now

        # Cleanup old data every 6 hours
        if now - self._last_cleanup > CLEANUP_INTERVAL:
            self._last_cleanup = now
            cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
            try:
                async with aiosqlite.connect(DB_PATH) as conn:
                    r1 = await conn.execute("DELETE FROM activity_messages WHERE date < ?", (cutoff,))
                    r2 = await conn.execute("DELETE FROM activity_voice WHERE date < ?", (cutoff,))
                    await conn.commit()
                    print(
                        f"[TRACKER] Cleaned up data before {cutoff} "
                        f"({r1.rowcount} message rows, {r2.rowcount} voice rows removed).",
                        flush=True,
                    )
            except Exception as e:
                print(f"[TRACKER] Cleanup error: {e}", flush=True)

    @_vc_flush_loop.before_loop
    async def before_vc_flush_loop(self):
        await self.bot.wait_until_ready()

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _format_seconds(total: int) -> str:
        """Human-readable time from seconds."""
        if total < 60:
            return f"{total}s"
        if total < 3600:
            m, s = divmod(total, 60)
            return f"{m}m {s}s" if s else f"{m}m"
        h, remainder = divmod(total, 3600)
        m, s = divmod(remainder, 60)
        if s:
            return f"{h}h {m}m {s}s"
        return f"{h}h {m}m" if m else f"{h}h"

    @staticmethod
    def _date_range(period: str) -> tuple[str, str]:
        """Return (start_date, end_date) YYYY-MM-DD strings for a period key."""
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
        else:  # all
            start = "2000-01-01"
        return start, end

    # ── Slash Commands ──────────────────────────────────────────────────────

    @app_commands.command(
        name="activity",
        description="View your message & voice activity stats on this server!",
    )
    @app_commands.describe(
        member="Check another member's activity (optional)",
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
        start, end = self._date_range(period_val)

        async with aiosqlite.connect(DB_PATH) as conn:
            # Total messages in period
            async with conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM activity_messages "
                "WHERE guild_id = ? AND user_id = ? AND date BETWEEN ? AND ?",
                (interaction.guild.id, target.id, start, end),
            ) as cur:
                total_msgs = (await cur.fetchone())[0]

            # Total voice seconds in period
            async with conn.execute(
                "SELECT COALESCE(SUM(seconds), 0) FROM activity_voice "
                "WHERE guild_id = ? AND user_id = ? AND date BETWEEN ? AND ?",
                (interaction.guild.id, target.id, start, end),
            ) as cur:
                total_voice = (await cur.fetchone())[0]

            # Top 5 channels by messages
            async with conn.execute(
                "SELECT channel_id, SUM(count) as c FROM activity_messages "
                "WHERE guild_id = ? AND user_id = ? AND date BETWEEN ? AND ? "
                "GROUP BY channel_id ORDER BY c DESC LIMIT 5",
                (interaction.guild.id, target.id, start, end),
            ) as cur:
                top_msg_channels = await cur.fetchall()

            # Top 5 channels by voice time
            async with conn.execute(
                "SELECT channel_id, SUM(seconds) as s FROM activity_voice "
                "WHERE guild_id = ? AND user_id = ? AND date BETWEEN ? AND ? "
                "GROUP BY channel_id ORDER BY s DESC LIMIT 5",
                (interaction.guild.id, target.id, start, end),
            ) as cur:
                top_vc_channels = await cur.fetchall()

            # Active days count
            async with conn.execute(
                "SELECT COUNT(DISTINCT date) FROM ("
                "  SELECT date FROM activity_messages WHERE guild_id = ? AND user_id = ? AND date BETWEEN ? AND ? "
                "  UNION "
                "  SELECT date FROM activity_voice WHERE guild_id = ? AND user_id = ? AND date BETWEEN ? AND ?"
                ")",
                (
                    interaction.guild.id, target.id, start, end,
                    interaction.guild.id, target.id, start, end,
                ),
            ) as cur:
                active_days = (await cur.fetchone())[0]

            # Daily average (messages and voice)
            if period_val in ("week", "month", "year"):
                days_in_period = max(1, (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days + 1)
            else:
                days_in_period = max(1, active_days) if active_days else 1

        period_labels = {
            "today": "Today",
            "week": "This Week (7 days)",
            "month": "This Month (30 days)",
            "year": "This Year (365 days)",
            "all": "All Time",
        }

        embed = discord.Embed(
            title=f"📊 Activity — {target.display_name}",
            description=f"**Period:** {period_labels.get(period_val, period_val)}",
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.add_field(
            name="💬 Messages",
            value=f"**{total_msgs:,}** messages",
            inline=True,
        )
        embed.add_field(
            name="🎙️ Voice Time",
            value=f"**{self._format_seconds(total_voice)}**",
            inline=True,
        )
        embed.add_field(
            name="📅 Active Days",
            value=f"**{active_days}** / {days_in_period} days",
            inline=True,
        )

        # Averages
        avg_msgs = total_msgs / max(1, days_in_period)
        avg_voice = total_voice / max(1, days_in_period)
        embed.add_field(
            name="📈 Daily Average",
            value=f"💬 {avg_msgs:.1f} msgs · 🎙️ {self._format_seconds(int(avg_voice))}",
            inline=False,
        )

        # Top message channels
        if top_msg_channels:
            lines = []
            for ch_id, count in top_msg_channels:
                ch = interaction.guild.get_channel(ch_id)
                name = ch.mention if ch else f"<#{ch_id}>"
                lines.append(f"{name} — **{count:,}** msgs")
            embed.add_field(
                name="💬 Top Channels (Messages)",
                value="\n".join(lines),
                inline=False,
            )

        # Top voice channels
        if top_vc_channels:
            lines = []
            for ch_id, secs in top_vc_channels:
                ch = interaction.guild.get_channel(ch_id)
                name = ch.mention if ch else f"<#{ch_id}>"
                lines.append(f"{name} — **{self._format_seconds(secs)}**")
            embed.add_field(
                name="🎙️ Top Channels (Voice)",
                value="\n".join(lines),
                inline=False,
            )

        embed.set_footer(
            text=f"🐾 Activity Tracker • {interaction.guild.name} • 365-day rolling window"
        )
        await style.send_card(interaction, embed=embed)

    @app_commands.command(
        name="activity-top",
        description="See the most active members on this server!",
    )
    @app_commands.describe(
        category="What to rank by",
        period="Time range to view",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="💬 Messages", value="messages"),
            app_commands.Choice(name="🎙️ Voice Time", value="voice"),
            app_commands.Choice(name="📅 Active Days", value="days"),
        ],
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
        category: app_commands.Choice[str] = None,
        period: app_commands.Choice[str] = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This command only works in a server!", ephemeral=True
            )

        cat_val = category.value if category else "messages"
        period_val = period.value if period else "year"
        start, end = self._date_range(period_val)

        period_labels = {
            "today": "Today",
            "week": "This Week (7 days)",
            "month": "This Month (30 days)",
            "year": "This Year (365 days)",
            "all": "All Time",
        }
        cat_labels = {
            "messages": "💬 Message Leaderboard",
            "voice": "🎙️ Voice Time Leaderboard",
            "days": "📅 Most Active Days",
        }

        async with aiosqlite.connect(DB_PATH) as conn:
            if cat_val == "messages":
                async with conn.execute(
                    "SELECT user_id, SUM(count) as total FROM activity_messages "
                    "WHERE guild_id = ? AND date BETWEEN ? AND ? "
                    "GROUP BY user_id ORDER BY total DESC LIMIT 10",
                    (interaction.guild.id, start, end),
                ) as cur:
                    rows = await cur.fetchall()
            elif cat_val == "voice":
                async with conn.execute(
                    "SELECT user_id, SUM(seconds) as total FROM activity_voice "
                    "WHERE guild_id = ? AND date BETWEEN ? AND ? "
                    "GROUP BY user_id ORDER BY total DESC LIMIT 10",
                    (interaction.guild.id, start, end),
                ) as cur:
                    rows = await cur.fetchall()
            else:  # days
                async with conn.execute(
                    "SELECT user_id, COUNT(DISTINCT date) as total FROM ("
                    "  SELECT user_id, date FROM activity_messages WHERE guild_id = ? AND date BETWEEN ? AND ? "
                    "  UNION "
                    "  SELECT user_id, date FROM activity_voice WHERE guild_id = ? AND date BETWEEN ? AND ?"
                    ") GROUP BY user_id ORDER BY total DESC LIMIT 10",
                    (
                        interaction.guild.id, start, end,
                        interaction.guild.id, start, end,
                    ),
                ) as cur:
                    rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message(
                "❌ No activity recorded yet for this period! Start chatting or join VC.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title=f"🏆 {interaction.guild.name} — {cat_labels.get(cat_val, cat_val)}",
            description=f"**Period:** {period_labels.get(period_val, period_val)}",
            color=0xF1C40F,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(
            url=interaction.guild.icon.url if interaction.guild.icon else None
        )

        medals = ["👑", "💎", "⭐", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        lines = []
        user_ids = [row[0] for row in rows]
        names = await style.display_names(interaction.guild, user_ids, client=interaction.client)

        for idx, row in enumerate(rows):
            uid, total = row
            display = names[idx]
            medal = medals[idx] if idx < len(medals) else f"#{idx + 1}"

            if cat_val == "messages":
                lines.append(f"{medal} {display} — **{total:,}** messages")
            elif cat_val == "voice":
                lines.append(f"{medal} {display} — **{self._format_seconds(total)}**")
            else:
                lines.append(f"{medal} {display} — **{total}** active days")

        embed.description = f"**Period:** {period_labels.get(period_val, period_val)}\n\n" + "\n\n".join(lines)
        embed.set_footer(text="🐾 Activity Tracker • Chat & join VC to climb!")
        await style.send_card(interaction, embed=embed)

    @app_commands.command(
        name="activity-channel",
        description="See activity stats for a specific channel!",
    )
    @app_commands.describe(
        channel="The channel to check (defaults to current)",
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
        start, end = self._date_range(period_val)

        period_labels = {
            "today": "Today",
            "week": "This Week (7 days)",
            "month": "This Month (30 days)",
            "year": "This Year (365 days)",
            "all": "All Time",
        }

        async with aiosqlite.connect(DB_PATH) as conn:
            # Top chatters
            async with conn.execute(
                "SELECT user_id, SUM(count) as c FROM activity_messages "
                "WHERE guild_id = ? AND channel_id = ? AND date BETWEEN ? AND ? "
                "GROUP BY user_id ORDER BY c DESC LIMIT 10",
                (interaction.guild.id, target_channel.id, start, end),
            ) as cur:
                top_chatters = await cur.fetchall()

            # Total messages in channel
            async with conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM activity_messages "
                "WHERE guild_id = ? AND channel_id = ? AND date BETWEEN ? AND ?",
                (interaction.guild.id, target_channel.id, start, end),
            ) as cur:
                total_msgs = (await cur.fetchone())[0]

            # Total voice time in channel
            async with conn.execute(
                "SELECT COALESCE(SUM(seconds), 0) FROM activity_voice "
                "WHERE guild_id = ? AND channel_id = ? AND date BETWEEN ? AND ?",
                (interaction.guild.id, target_channel.id, start, end),
            ) as cur:
                total_voice = (await cur.fetchone())[0]

            # Unique users
            async with conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM activity_messages "
                "WHERE guild_id = ? AND channel_id = ? AND date BETWEEN ? AND ?",
                (interaction.guild.id, target_channel.id, start, end),
            ) as cur:
                unique_users = (await cur.fetchone())[0]

        embed = discord.Embed(
            title=f"📊 Channel Activity — {target_channel.name}",
            description=f"**Period:** {period_labels.get(period_val, period_val)}",
            color=0x2ECC71,
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(name="💬 Total Messages", value=f"**{total_msgs:,}**", inline=True)
        embed.add_field(name="🎙️ Total Voice", value=f"**{self._format_seconds(total_voice)}**", inline=True)
        embed.add_field(name="👥 Unique Users", value=f"**{unique_users}**", inline=True)

        if top_chatters:
            names = await style.display_names(
                interaction.guild, [row[0] for row in top_chatters], client=interaction.client
            )
            medals = ["👑", "💎", "⭐", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            lines = []
            for idx, (uid, count) in enumerate(top_chatters):
                medal = medals[idx] if idx < len(medals) else f"#{idx + 1}"
                lines.append(f"{medal} {names[idx]} — **{count:,}** msgs")
            embed.add_field(
                name="💬 Top Chatters",
                value="\n".join(lines),
                inline=False,
            )

        embed.set_footer(text=f"🐾 Activity Tracker • {interaction.guild.name}")
        await style.send_card(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tracker(bot))
