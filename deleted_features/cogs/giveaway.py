import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import aiosqlite
import datetime
import time
import re
import secrets
from utils import db
from utils.style import Panel, send_panel, edit_to_panel, ACCENT, SUCCESS, DANGER, GOLD, guild_icon, avatar_url

DB_PATH = db.DB_PATH


def parse_duration(time_str: str) -> int | None:
    """Converts strings like '10m', '2h', '1d', '30s' into total seconds."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    pattern = re.compile(r"(\d+)([smhd])", re.IGNORECASE)
    matches = pattern.findall(time_str.strip())
    if not matches:
        try:
            val = int(time_str)
            return val * 60
        except ValueError:
            return None

    total_seconds = 0
    for amount, unit in matches:
        total_seconds += int(amount) * units[unit.lower()]
    return total_seconds if total_seconds > 0 else None


class GiveawayView(discord.ui.View):
    def __init__(self, message_id: int = 0):
        super().__init__(timeout=None)
        self.message_id = message_id

    @discord.ui.button(label="Enter Giveaway", style=discord.ButtonStyle.primary, emoji="🎉", custom_id="giveaway_enter_btn")
    async def enter_giveaway(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = interaction.message.id

        # Button callbacks don't pass through the cog's interaction_check, so the
        # module toggle has to be enforced here too.
        bot = interaction.client
        if interaction.guild_id and hasattr(bot, "is_module_enabled") and not bot.is_module_enabled(interaction.guild_id, "giveaway"):
            return await interaction.response.send_message(
                "⚠️ The **Giveaway** system is currently **turned off** on this server.", ephemeral=True
            )

        async with aiosqlite.connect(DB_PATH) as conn:
            # Check if giveaway is still active
            async with conn.execute("SELECT prize, ended FROM giveaways WHERE message_id=?", (msg_id,)) as cur:
                row = await cur.fetchone()
                if not row or row[1] == 1:
                    return await interaction.response.send_message("❌ This giveaway has already ended!", ephemeral=True)
                prize = row[0]

            # Check if user already entered
            async with conn.execute("SELECT 1 FROM giveaway_entries WHERE message_id=? AND user_id=?", (msg_id, interaction.user.id)) as cur:
                exists = await cur.fetchone()

            if exists:
                # Toggle off (leave giveaway)
                await conn.execute("DELETE FROM giveaway_entries WHERE message_id=? AND user_id=?", (msg_id, interaction.user.id))
                await conn.commit()
                # Count remaining
                async with conn.execute("SELECT COUNT(*) FROM giveaway_entries WHERE message_id=?", (msg_id,)) as cur:
                    c_row = await cur.fetchone()
                    count = c_row[0] if c_row else 0
                return await interaction.response.send_message(f"🚪 You have left the giveaway for **{prize}**.", ephemeral=True)
            else:
                # Add entry
                await conn.execute("INSERT INTO giveaway_entries (message_id, user_id) VALUES (?, ?)", (msg_id, interaction.user.id))
                await conn.commit()
                return await interaction.response.send_message(f"🎉 You're in for **{prize}**. Good luck!", ephemeral=True)


class Giveaway(commands.Cog):
    """Modern community giveaway management with live countdowns and fair winner selection."""

    giveaway_group = app_commands.Group(name="giveaway", description="Community giveaway commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Register persistent view
        self.bot.add_view(GiveawayView())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "giveaway"):
                await interaction.response.send_message(
                    "⚠️ The **Giveaway** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True
                )
                return False
        return True

    async def cog_load(self):
        await db.setup()
        self.check_giveaways.start()

    def cog_unload(self):
        self.check_giveaways.cancel()

    @tasks.loop(seconds=10)
    async def check_giveaways(self):
        now = time.time()
        try:
            async with aiosqlite.connect(DB_PATH) as conn:
                async with conn.execute(
                    "SELECT message_id, channel_id, guild_id, prize, winners, end_ts, host_id FROM giveaways WHERE ended=0 AND end_ts <= ?",
                    (now,)
                ) as cur:
                    expired = await cur.fetchall()

                for row in expired:
                    msg_id, ch_id, guild_id, prize, winners_count, end_ts, host_id = row
                    # With the module turned off, stop processing giveaways in the
                    # background so nothing keeps running behind the admin's back.
                    if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(guild_id, "giveaway"):
                        continue
                    await self._end_giveaway(msg_id, ch_id, guild_id, prize, winners_count, host_id)
        except Exception as e:
            print(f"Giveaway loop error: {e}")

    @check_giveaways.before_loop
    async def before_check(self):
        try:
            await self.bot.wait_until_ready()
        except Exception:
            pass

    async def _end_giveaway(self, message_id: int, channel_id: int, guild_id: int, prize: str, winners_count: int, host_id: int):
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("UPDATE giveaways SET ended=1 WHERE message_id=?", (message_id,))
            async with conn.execute("SELECT user_id FROM giveaway_entries WHERE message_id=?", (message_id,)) as cur:
                rows = await cur.fetchall()
            await conn.commit()

        entries = [r[0] for r in rows]
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None

        if not channel:
            return

        try:
            msg = await channel.fetch_message(message_id)
        except Exception:
            msg = None

        if not entries:
            # No participants
            embed = discord.Embed(
                title=f"🎉 Giveaway Ended: {prize}",
                description="❌ No valid entries were received. No winner could be picked!",
                color=0xED4245,
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text="🐾 Server Giveaways • Ended")
            if msg:
                try:
                    ended = Panel(
                        title="Giveaway ended",
                        body=f"**{prize}**\n\nNobody entered, so no winner was drawn.",
                        accent=DANGER,
                        thumbnail=guild_icon(guild),
                        footer="Giveaway closed",
                        timeout=None,
                    )
                    await edit_to_panel(msg, ended)
                except Exception:
                    pass
            await channel.send(f"📢 The giveaway for **{prize}** ended, but nobody entered! 😢")
            return

        # Fair cryptographic selection
        selected_winners = secrets.SystemRandom().sample(entries, min(len(entries), winners_count))
        mentions = [f"<@{uid}>" for uid in selected_winners]
        winner_text = ", ".join(mentions)

        embed = discord.Embed(
            title="🎉 GIVEAWAY ENDED! 🎉",
            description=(
                f"### Prize: **{prize}**\n\n"
                f"👑 **Winner(s):** {winner_text}\n"
                f"👤 **Hosted by:** <@{host_id}>\n"
                f"🎟️ **Total Entries:** `{len(entries):,}`\n\n"
                f"Congratulations to the winner(s)! Contact the host to claim your prize!"
            ),
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Server Giveaways • Congratulations!")

        if msg:
            try:
                await msg.edit(embed=embed, view=None)
            except Exception:
                pass

        await channel.send(
            f"🥳 **Congratulations {winner_text}!** You won the giveaway for **{prize}**! 🎁",
            reference=msg
        )

    @giveaway_group.command(name="start", description="Launch an interactive community giveaway.")
    @app_commands.describe(
        duration="Giveaway duration (e.g. '10m', '2h', '1d')",
        winners="Number of winners to select",
        prize="The prize being given away",
        channel="Channel to post the giveaway in (defaults to current)"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def giveaway_start(
        self,
        interaction: discord.Interaction,
        duration: str,
        winners: app_commands.Range[int, 1, 20],
        prize: str,
        channel: discord.TextChannel = None
    ):
        target_ch = channel or interaction.channel
        sec = parse_duration(duration)
        if not sec or sec < 10:
            return await interaction.response.send_message("❌ Invalid duration! Use formats like `30s`, `10m`, `2h`, or `1d` (min 10s).", ephemeral=True)

        end_ts = int(time.time() + sec)

        view = Panel(
            title=f"Giveaway  ·  {prize}",
            body=(
                f"Tap **Enter** to join. Ends <t:{end_ts}:R>.\n\n"
                f"- 🎁 **Prize:** **{prize}**\n"
                f"- 👑 **Winners:** `{winners}`\n"
                f"- 👤 **Host:** {interaction.user.mention}\n"
                f"- ⏱️ **Closes:** <t:{end_ts}:f>"
            ),
            accent=GOLD,
            thumbnail=guild_icon(interaction.guild) or avatar_url(interaction.user),
            footer="Fair random draw  ·  enter once",
            timeout=None,
        )
        view.add_row(
            discord.ui.Button(
                label="Enter Giveaway",
                style=discord.ButtonStyle.primary,
                emoji="🎉",
                custom_id="giveaway_enter_btn",
            )
        )
        msg = await send_panel(target_ch, view)

        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                """INSERT INTO giveaways (message_id, channel_id, guild_id, prize, winners, end_ts, host_id, ended)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                (msg.id, target_ch.id, interaction.guild.id, prize, winners, end_ts, interaction.user.id)
            )
            await conn.commit()

        await interaction.response.send_message(f"✅ Giveaway launched in {target_ch.mention}!", ephemeral=True)

    @giveaway_group.command(name="end", description="End an active giveaway immediately and roll winners.")
    @app_commands.describe(message_id="The message ID of the active giveaway")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def giveaway_end(self, interaction: discord.Interaction, message_id: str):
        try:
            m_id = int(message_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Please provide a valid message ID.", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT channel_id, guild_id, prize, winners, host_id, ended FROM giveaways WHERE message_id=?",
                (m_id,)
            ) as cur:
                row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message("❌ Giveaway not found in the database.", ephemeral=True)
        if row[5] == 1:
            return await interaction.response.send_message("❌ That giveaway has already ended!", ephemeral=True)

        ch_id, guild_id, prize, winners, host_id, _ = row
        await interaction.response.send_message("⚡ Ending giveaway and picking winner(s)...", ephemeral=True)
        await self._end_giveaway(m_id, ch_id, guild_id, prize, winners, host_id)

    @giveaway_group.command(name="reroll", description="Re-roll new winner(s) for an ended giveaway.")
    @app_commands.describe(message_id="The message ID of the ended giveaway")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def giveaway_reroll(self, interaction: discord.Interaction, message_id: str):
        try:
            m_id = int(message_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Please provide a valid message ID.", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT channel_id, guild_id, prize, winners, host_id, ended FROM giveaways WHERE message_id=?",
                (m_id,)
            ) as cur:
                row = await cur.fetchone()

            if not row:
                return await interaction.response.send_message("❌ Giveaway not found in the database.", ephemeral=True)

            async with conn.execute("SELECT user_id FROM giveaway_entries WHERE message_id=?", (m_id,)) as cur:
                entries_rows = await cur.fetchall()

        entries = [r[0] for r in entries_rows]
        if not entries:
            return await interaction.response.send_message("❌ No participants entered that giveaway.", ephemeral=True)

        ch_id, guild_id, prize, winners_count, host_id, _ = row
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(ch_id) if guild else None
        if not channel:
            return await interaction.response.send_message("❌ Original giveaway channel no longer exists.", ephemeral=True)

        winner_id = secrets.choice(entries)
        await channel.send(f"🎲 **Re-Roll Winner!** Congratulations <@{winner_id}>! You are the new winner of **{prize}**! 🎁")
        await interaction.response.send_message(f"✅ Rerolled! New winner: <@{winner_id}>", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaway(bot))
