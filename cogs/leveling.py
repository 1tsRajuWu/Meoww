import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
import random
import time
import asyncio
from utils import db
from cogs.economy import update_wallet

DB_PATH = db.DB_PATH

# -----------------------------------------------------------------------------
# XP & Level Formula Mechanics
# Standard balanced formula: XP needed for next level = 5 * (lvl^2) + (50 * lvl) + 100
# -----------------------------------------------------------------------------

def xp_needed_for_level(level: int) -> int:
    """XP needed to advance from `level` to `level + 1` (Hardened MMO-style scaling)."""
    return 15 * (level ** 2) + (100 * level) + 250

def total_xp_for_level(level: int) -> int:
    """Cumulative total XP required from level 0 to reach `level`."""
    return sum(xp_needed_for_level(i) for i in range(level))

def calculate_level_from_xp(total_xp: int) -> tuple[int, int, int]:
    """
    Given total cumulative XP, returns:
    (current_level, xp_progress_in_current_level, xp_needed_for_next_level)
    """
    level = 0
    remaining = total_xp
    while True:
        needed = xp_needed_for_level(level)
        if remaining >= needed:
            remaining -= needed
            level += 1
        else:
            break
    return level, remaining, needed

def render_progress_bar(current: int, total: int, length: int = 10) -> str:
    """Renders a visual Unicode progress bar."""
    if total <= 0:
        return "▱" * length + " 0%"
    fraction = max(0.0, min(1.0, current / total))
    filled = int(round(fraction * length))
    empty = length - filled
    return f"`[{'▰' * filled}{'▱' * empty}]` **{int(fraction * 100)}%**"

def get_tier_info(level: int) -> tuple[str, str, int]:
    """Returns (tier_name, badge_emoji, embed_color) based on level (modern titles, zero medals)."""
    if level >= 100:
        return "Mythic God", "🌟", 0xFFD700
    elif level >= 75:
        return "Celestial Lord", "🪐", 0x00CEC9
    elif level >= 50:
        return "Shadow Monarch", "👑", 0xE74C3C
    elif level >= 30:
        return "Elite Champion", "💎", 0x9B59B6
    elif level >= 15:
        return "Vanguard", "🗡️", 0x2ECC71
    elif level >= 5:
        return "Pathfinder", "⚡", 0x3498DB
    else:
        return "Initiate", "🐾", 0x95A5A6

def format_minutes(minutes: int) -> str:
    """Formats voice minutes into human readable hours and minutes."""
    if minutes < 60:
        return f"{minutes}m"
    hrs = minutes // 60
    mins = minutes % 60
    return f"{hrs}h {mins}m" if mins else f"{hrs}h"


class Leveling(commands.Cog):
    """Advanced Role & Leveling System with Chat & Voice (VC) Money Rewards."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # In-memory chat cooldowns: (guild_id, user_id) -> timestamp (10 seconds)
        self._chat_cooldowns: dict[tuple[int, int], float] = {}

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.vc_reward_loop.is_running():
            self.vc_reward_loop.start()

    def cog_unload(self):
        self.vc_reward_loop.cancel()

    # -------------------------------------------------------------------------
    # Database Helper Methods
    # -------------------------------------------------------------------------

    async def get_guild_settings(self, guild_id: int) -> dict:
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT enabled, channel_id, dm_notifications, text_min_coins, text_max_coins, text_xp, vc_coins_per_min, vc_xp_per_min FROM level_settings WHERE guild_id = ?",
                (guild_id,)
            ) as cur:
                row = await cur.fetchone()

        if not row:
            def_ch = 1547637176697757846 if guild_id == 1542278896148422676 else 0
            default_settings = {
                "enabled": 1,
                "channel_id": def_ch,
                "dm_notifications": 0,
                "text_min_coins": 4,
                "text_max_coins": 10,
                "text_xp": 15,
                "vc_coins_per_min": 2,
                "vc_xp_per_min": 3
            }
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    """INSERT OR IGNORE INTO level_settings 
                       (guild_id, enabled, channel_id, dm_notifications, text_min_coins, text_max_coins, text_xp, vc_coins_per_min, vc_xp_per_min)
                       VALUES (?, 1, ?, 0, 4, 10, 15, 2, 3)""",
                    (guild_id, def_ch)
                )
                await conn.commit()
            return default_settings

        # Auto-balance legacy high defaults so VC leveling is properly hardened
        vc_c = min(row[6], 2) if row[6] >= 10 else row[6]
        vc_x = min(row[7], 3) if row[7] >= 15 else row[7]

        return {
            "enabled": row[0],
            "channel_id": row[1],
            "dm_notifications": row[2],
            "text_min_coins": row[3],
            "text_max_coins": row[4],
            "text_xp": row[5],
            "vc_coins_per_min": vc_c,
            "vc_xp_per_min": vc_x
        }

    async def get_user_data(self, guild_id: int, user_id: int) -> dict:
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT xp, level, total_messages, voice_minutes, total_earned, last_message_ts FROM user_levels WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)
            ) as cur:
                row = await cur.fetchone()

        if not row:
            return {
                "xp": 0,
                "level": 0,
                "total_messages": 0,
                "voice_minutes": 0,
                "total_earned": 0,
                "last_message_ts": 0
            }
        return {
            "xp": row[0],
            "level": row[1],
            "total_messages": row[2],
            "voice_minutes": row[3],
            "total_earned": row[4],
            "last_message_ts": row[5] if len(row) > 5 and row[5] is not None else 0
        }

    async def get_level_roles(self, guild_id: int) -> list[tuple[int, int]]:
        """Returns list of (level, role_id) configured for this server."""
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT level, role_id FROM level_roles WHERE guild_id=? ORDER BY level ASC",
                (guild_id,)
            ) as cur:
                return await cur.fetchall()

    async def check_and_award_roles(self, guild: discord.Guild, member: discord.Member, new_level: int) -> list[discord.Role]:
        """Automatically assigns any level roles unlocked up to new_level."""
        roles_unlocked = []
        level_roles = await self.get_level_roles(guild.id)
        for req_level, role_id in level_roles:
            if new_level >= req_level:
                role = guild.get_role(role_id)
                if role and role not in member.roles:
                    try:
                        await member.add_roles(role, reason=f"Level {req_level} Reward")
                        roles_unlocked.append(role)
                    except discord.Forbidden:
                        pass
                    except Exception as e:
                        print(f"Failed to award role {role_id}: {e}")
        return roles_unlocked

    async def handle_level_up(
        self,
        guild: discord.Guild,
        member: discord.Member,
        old_level: int,
        new_level: int,
        channel: discord.TextChannel = None
    ):
        """Sends celebration embed and awards unlocked level roles."""
        unlocked_roles = await self.check_and_award_roles(guild, member, new_level)
        settings = await self.get_guild_settings(guild.id)

        tier_name, badge, color = get_tier_info(new_level)

        embed = discord.Embed(
            title=f"⚡ Ranked Up! — Level {new_level}",
            description=(
                f"### GG {member.mention}! 🔥\n"
                f"You just ranked up to **Level {new_level}**!\n\n"
                f"> 🏷️ **Rank Title:** {badge} **{tier_name}**"
            ),
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        if unlocked_roles:
            role_mentions = ", ".join(r.mention for r in unlocked_roles)
            embed.add_field(
                name="🎁 Unlocked Role Rewards!",
                value=f"You have been granted: {role_mentions}",
                inline=False
            )

        embed.set_footer(text=f"🐾 Server Level System • {guild.name}", icon_url=guild.icon.url if guild.icon else None)

        if settings.get("dm_notifications"):
            try:
                await member.send(embed=embed)
            except discord.Forbidden:
                pass

        target_channel = None
        # 1. Configured channel in /levelsettings
        if settings.get("channel_id"):
            target_channel = guild.get_channel(settings["channel_id"])

        # 2. Priority: Dedicated bot commands channel (#bot-cmd: 1547637176697757846)
        # Explicitly routes to public bot-cmd and avoids private/admin channels!
        if not target_channel:
            specific_ch = guild.get_channel(1547637176697757846)
            if specific_ch and specific_ch.permissions_for(guild.me).send_messages:
                target_channel = specific_ch

        # 3. Dynamic priority: Look for member-accessible bot channels, excluding Admin/Staff categories
        if not target_channel:
            for ch in guild.text_channels:
                cat_name = ch.category.name.lower() if ch.category else ""
                if any(adm in cat_name for adm in ("admin", "staff", "mod", "ticket")):
                    continue
                cname = ch.name.lower()
                if any(kw in cname for kw in ("bot-cmd", "botcmd", "bot_cmd", "bot-commands", "level-up", "levelups")):
                    if ch.permissions_for(guild.me).send_messages:
                        target_channel = ch
                        break

        # 4. Secondary fallback: Channels containing cmd/commands that are NOT in an admin category
        if not target_channel:
            for ch in guild.text_channels:
                cat_name = ch.category.name.lower() if ch.category else ""
                if any(adm in cat_name for adm in ("admin", "staff", "mod", "ticket")):
                    continue
                cname = ch.name.lower()
                if any(kw in cname for kw in ("cmd", "cmds", "commands")):
                    if ch.permissions_for(guild.me).send_messages:
                        target_channel = ch
                        break

        # 5. Final Fallback: server system channel, or active channel
        if not target_channel:
            target_channel = guild.system_channel
        if not target_channel:
            target_channel = channel

        if target_channel and target_channel.permissions_for(guild.me).send_messages:
            try:
                await target_channel.send(embed=embed)
            except Exception as e:
                print(f"Could not send level up message: {e}")

    # -------------------------------------------------------------------------
    # Text Chat Reward Listener (on_message)
    # -------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(message.guild.id, "leveling"):
            return

        settings = await self.get_guild_settings(message.guild.id)
        if not settings["enabled"]:
            return

        now = time.time()
        key = (message.guild.id, message.author.id)
        last_time = self._chat_cooldowns.get(key, 0)

        # 10-second anti-spam cooldown for gaining coins & XP
        if now - last_time < 10:
            # Count message and keep activity timestamp active even during cooldown
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    """INSERT INTO user_levels (guild_id, user_id, total_messages, last_message_ts)
                       VALUES (?, ?, 1, ?)
                       ON CONFLICT(guild_id, user_id) DO UPDATE SET 
                           total_messages = total_messages + 1,
                           last_message_ts = ?""",
                    (message.guild.id, message.author.id, now, now)
                )
                await conn.commit()
            return

        self._chat_cooldowns[key] = now

        # Message Length Based Rewards:
        content_len = len(message.content.strip()) if message.content else 0
        # Attachments (photos, memes, clips) add 25 virtual characters of effort
        if message.attachments:
            content_len += len(message.attachments) * 25

        # Anti-spam: Ignore very short messages (< 3 chars like '.', 'k', '??')
        if content_len < 3:
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    """INSERT INTO user_levels (guild_id, user_id, total_messages, last_message_ts)
                       VALUES (?, ?, 1, ?)
                       ON CONFLICT(guild_id, user_id) DO UPDATE SET 
                           total_messages = total_messages + 1,
                           last_message_ts = ?""",
                    (message.guild.id, message.author.id, now, now)
                )
                await conn.commit()
            return

        base_min = settings["text_min_coins"]
        base_max = settings["text_max_coins"]
        base_xp = settings["text_xp"]

        # Scaled multipliers based on message length:
        # - Very Short (3 - 14 chars, e.g. 'hello', 'gg'): 0.5x multiplier
        # - Standard (15 - 49 chars, normal conversation): 1.0x baseline
        # - Medium (50 - 119 chars, active discussion): 1.5x multiplier
        # - Long (120 - 249 chars, detailed thoughts): 2.0x multiplier
        # - Effort / Paragraph (250+ chars, capped at 2.5x to prevent essay spam): 2.5x multiplier
        if content_len < 15:
            multiplier = 0.5
        elif content_len < 50:
            multiplier = 1.0
        elif content_len < 120:
            multiplier = 1.5
        elif content_len < 250:
            multiplier = 2.0
        else:
            multiplier = 2.5

        raw_coins = random.randint(base_min, base_max)
        coins = max(1, int(round(raw_coins * multiplier)))
        xp_gain = max(5, int(round(base_xp * multiplier)))

        try:
            await update_wallet(message.author.id, message.guild.id, coins)
        except Exception as e:
            print(f"Leveling coin deposit error: {e}")

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT xp, level, total_messages, total_earned FROM user_levels WHERE guild_id=? AND user_id=?",
                (message.guild.id, message.author.id)
            ) as cur:
                row = await cur.fetchone()

            if not row:
                old_xp, old_lvl, total_msgs, total_earned = 0, 0, 0, 0
            else:
                old_xp, old_lvl, total_msgs, total_earned = row

            new_xp = old_xp + xp_gain
            new_msgs = total_msgs + 1
            new_earned = total_earned + coins
            new_lvl, _, _ = calculate_level_from_xp(new_xp)

            await conn.execute(
                """INSERT INTO user_levels (guild_id, user_id, xp, level, total_messages, total_earned, last_message_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id, user_id) DO UPDATE SET 
                       xp=?, level=?, total_messages=?, total_earned=?, last_message_ts=?""",
                (message.guild.id, message.author.id, new_xp, new_lvl, new_msgs, new_earned, now,
                 new_xp, new_lvl, new_msgs, new_earned, now)
            )
            await conn.commit()

        if new_lvl > old_lvl:
            await self.handle_level_up(message.guild, message.author, old_lvl, new_lvl, message.channel)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """When member connects to a voice channel, give them an initial 1-hour active window."""
        if member.bot or not member.guild:
            return
        if before.channel is None and after.channel is not None:
            now = time.time()
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    """INSERT INTO user_levels (guild_id, user_id, last_message_ts)
                       VALUES (?, ?, ?)
                       ON CONFLICT(guild_id, user_id) DO UPDATE SET 
                           last_message_ts = CASE 
                               WHEN (? - last_message_ts) > 3600 THEN ?
                               ELSE last_message_ts
                           END""",
                    (member.guild.id, member.id, now, now, now)
                )
                await conn.commit()

    # -------------------------------------------------------------------------
    # Voice Channel (VC) Reward Task Loop (every 60 seconds)
    # -------------------------------------------------------------------------

    @tasks.loop(seconds=60)
    async def vc_reward_loop(self):
        """Runs every minute to reward active members in voice channels with coins and XP."""
        for guild in self.bot.guilds:
            try:
                if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(guild.id, "leveling"):
                    continue

                settings = await self.get_guild_settings(guild.id)
                if not settings["enabled"]:
                    continue

                vc_coins = settings["vc_coins_per_min"]
                vc_xp = settings["vc_xp_per_min"]
                now = time.time()

                channels = guild.voice_channels + getattr(guild, "stage_channels", [])
                for vc in channels:
                    if guild.afk_channel and vc.id == guild.afk_channel.id:
                        continue

                    for member in vc.members:
                        if member.bot:
                            continue

                        # Anti-AFK: ignore users who are deafened
                        if member.voice and (member.voice.self_deaf or member.voice.deaf):
                            continue

                        # AFK Check: If member hasn't sent a message in voice channel or anywhere in the server for > 1 hour (3600s), they are considered AFK and do not earn VC coins or XP
                        async with aiosqlite.connect(DB_PATH) as conn:
                            async with conn.execute(
                                "SELECT xp, level, voice_minutes, total_earned, last_message_ts FROM user_levels WHERE guild_id=? AND user_id=?",
                                (guild.id, member.id)
                            ) as cur:
                                row = await cur.fetchone()

                        if not row:
                            # Newly tracked member - start active
                            async with aiosqlite.connect(DB_PATH) as conn:
                                await conn.execute(
                                    "INSERT INTO user_levels (guild_id, user_id, last_message_ts) VALUES (?, ?, ?)",
                                    (guild.id, member.id, now)
                                )
                                await conn.commit()
                            old_xp, old_lvl, v_mins, total_earned, last_msg_ts = 0, 0, 0, 0, now
                        else:
                            old_xp, old_lvl, v_mins, total_earned, last_msg_ts = row
                            last_msg_ts = last_msg_ts or 0

                        # If user hasn't sent a message in voice channel or anywhere for > 1 hour (3600s):
                        if (now - last_msg_ts) > 3600:
                            # User is AFK (>1 hour inactivity) - skip coin and XP rewards
                            continue

                        try:
                            await update_wallet(member.id, guild.id, vc_coins)
                        except Exception as e:
                            print(f"VC coin deposit error: {e}")

                        new_xp = old_xp + vc_xp
                        new_vmins = v_mins + 1
                        new_earned = total_earned + vc_coins
                        new_lvl, _, _ = calculate_level_from_xp(new_xp)

                        async with aiosqlite.connect(DB_PATH) as conn:
                            await conn.execute(
                                """INSERT INTO user_levels (guild_id, user_id, xp, level, voice_minutes, total_earned)
                                   VALUES (?, ?, ?, ?, ?, ?)
                                   ON CONFLICT(guild_id, user_id) DO UPDATE SET 
                                       xp=?, level=?, voice_minutes=?, total_earned=?""",
                                (guild.id, member.id, new_xp, new_lvl, new_vmins, new_earned,
                                 new_xp, new_lvl, new_vmins, new_earned)
                            )
                            await conn.commit()

                        if new_lvl > old_lvl:
                            await self.handle_level_up(guild, member, old_lvl, new_lvl, None)

            except Exception as e:
                print(f"Error in vc_reward_loop for guild {guild.id}: {e}")

    @vc_reward_loop.before_loop
    async def before_vc_reward_loop(self):
        await self.bot.wait_until_ready()

    # -------------------------------------------------------------------------
    # Slash Commands: /rank, /leaderboard, /levelroles, Admin commands
    # -------------------------------------------------------------------------

    @app_commands.command(name="rank", description="Check your or another member's level, XP, rank and activity stats!")
    @app_commands.describe(member="The member whose rank card to view")
    async def rank(self, interaction: discord.Interaction, member: discord.Member = None):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        target = member or interaction.user
        if target.bot:
            return await interaction.response.send_message("❌ Bots don't have rank cards!", ephemeral=True)

        user_data = await self.get_user_data(interaction.guild.id, target.id)
        total_xp = user_data["xp"]
        level, current_xp, needed_xp = calculate_level_from_xp(total_xp)
        tier_name, badge, color = get_tier_info(level)

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM user_levels WHERE guild_id=? AND xp > ?",
                (interaction.guild.id, total_xp)
            ) as cur:
                higher_count = (await cur.fetchone())[0]

            async with conn.execute(
                "SELECT COUNT(*) FROM user_levels WHERE guild_id=?",
                (interaction.guild.id,)
            ) as cur:
                total_tracked = (await cur.fetchone())[0]

        rank_pos = higher_count + 1
        total_members = max(total_tracked, interaction.guild.member_count or 1)

        level_roles = await self.get_level_roles(interaction.guild.id)
        current_role = None
        next_role = None
        for req_lvl, r_id in level_roles:
            r = interaction.guild.get_role(r_id)
            if not r:
                continue
            if level >= req_lvl:
                current_role = r
            elif next_role is None and level < req_lvl:
                next_role = (req_lvl, r)

        bar = render_progress_bar(current_xp, needed_xp, length=12)

        embed = discord.Embed(
            title=f"{badge} {target.display_name}'s Rank Card",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name=f"{target.name} • {tier_name}", icon_url=target.display_avatar.url)
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.description = (
            f"### 🏆 **Level {level}** • **Rank #{rank_pos}** of {total_members}\n"
            f"**Progress:** {current_xp:,} / {needed_xp:,} XP\n"
            f"{bar}\n"
        )

        embed.add_field(
            name="💬 Messages",
            value=f"`{user_data['total_messages']:,}` msgs",
            inline=True
        )
        embed.add_field(
            name="🎙️ Voice Time",
            value=f"`{format_minutes(user_data['voice_minutes'])}`",
            inline=True
        )
        embed.add_field(
            name="🪙 Activity Earned",
            value=f"`{user_data['total_earned']:,}` 🪙",
            inline=True
        )
        now_ts = time.time()
        last_act = user_data.get("last_message_ts", 0)
        is_user_afk = (now_ts - last_act > 3600) if last_act > 0 else True
        status_text = "🟢 Active" if not is_user_afk else "💤 Inactive / AFK (>1h no msg)"
        embed.add_field(
            name="⚡ Status",
            value=f"`{status_text}`",
            inline=True
        )

        role_info = []
        if current_role:
            role_info.append(f"• **Current Role:** {current_role.mention}")
        if next_role:
            role_info.append(f"• **Next Role Unlock:** {next_role[1].mention} *(at Level {next_role[0]})*")
        elif not level_roles:
            role_info.append("• *No level roles configured yet (Admins: use `/addlevelrole`)*")

        if role_info:
            embed.add_field(name="🎖️ Level Roles", value="\n".join(role_info), inline=False)

        embed.set_footer(text=f"Total Cumulative XP: {total_xp:,} XP")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="View the server's top ranked members!")
    @app_commands.describe(category="Filter leaderboard by category")
    @app_commands.choices(category=[
        app_commands.Choice(name="🌟 Levels & XP", value="xp"),
        app_commands.Choice(name="🎙️ Voice Chat Time", value="voice"),
        app_commands.Choice(name="💬 Chat Messages", value="messages"),
        app_commands.Choice(name="💰 Activity Earnings", value="earnings"),
    ])
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str] = None
    ):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        cat_val = category.value if category else "xp"

        order_col = {
            "xp": "xp",
            "voice": "voice_minutes",
            "messages": "total_messages",
            "earnings": "total_earned"
        }.get(cat_val, "xp")

        cat_titles = {
            "xp": "🌟 Level & XP Leaderboard",
            "voice": "🎙️ Voice Chat Leaderboard",
            "messages": "💬 Message Leaderboard",
            "earnings": "💰 Activity Earnings Leaderboard"
        }

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                f"SELECT user_id, xp, level, total_messages, voice_minutes, total_earned FROM user_levels WHERE guild_id=? ORDER BY {order_col} DESC LIMIT 10",
                (interaction.guild.id,)
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message("❌ No activity recorded yet! Start chatting or join VC to get ranked!", ephemeral=True)

        embed = discord.Embed(
            title=f"🏆 {interaction.guild.name} — {cat_titles[cat_val]}",
            color=0xF1C40F,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)

        medals = ["👑", "💎", "⭐", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        lines = []

        for idx, row in enumerate(rows):
            uid, u_xp, u_lvl, u_msgs, u_vmins, u_earned = row
            member = interaction.guild.get_member(uid)
            display = member.mention if member else f"<@{uid}>"
            medal = medals[idx] if idx < len(medals) else f"`#{idx+1}`"

            if cat_val == "xp":
                lines.append(f"{medal} {display} — **Level {u_lvl}** • `{u_xp:,}` XP")
            elif cat_val == "voice":
                lines.append(f"{medal} {display} — **{format_minutes(u_vmins)}** in VC • Level {u_lvl}")
            elif cat_val == "messages":
                lines.append(f"{medal} {display} — **{u_msgs:,}** messages • Level {u_lvl}")
            else:
                lines.append(f"{medal} {display} — **{u_earned:,}** 🪙 earned • Level {u_lvl}")

        embed.description = "\n\n".join(lines)
        embed.set_footer(text="🐾 Chat in text or chill in VC to climb the ranks!")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="levelroles", description="Show all configured level roles and rewards in this server.")
    async def levelroles(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        roles = await self.get_level_roles(interaction.guild.id)
        if not roles:
            return await interaction.response.send_message(
                "📜 No level roles configured for this server yet!\n"
                "Admins can set up role rewards using `/addlevelrole level:<number> role:@Role`.",
                ephemeral=True
            )

        embed = discord.Embed(
            title=f"🎖️ {interaction.guild.name} — Level Role Rewards",
            description="Reaching these levels automatically awards the corresponding roles:\n",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        for req_lvl, r_id in roles:
            role = interaction.guild.get_role(r_id)
            role_str = role.mention if role else f"`[Deleted Role ID {r_id}]`"
            members_count = len(role.members) if role else 0
            tier_name, badge, _ = get_tier_info(req_lvl)
            embed.description += f"• **Level {req_lvl}** {badge} ➔ {role_str} *({members_count} members)*\n"

        embed.set_footer(text="🐾 Roles are granted instantly when you level up!")
        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------------------
    # Admin / Management Commands
    # -------------------------------------------------------------------------

    @app_commands.command(name="addlevelrole", description="Set a role reward to be automatically granted at a level.")
    @app_commands.describe(level="The required level", role="The role to grant")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def addlevelrole(self, interaction: discord.Interaction, level: int, role: discord.Role):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        if level < 1:
            return await interaction.response.send_message("❌ Level must be 1 or higher!", ephemeral=True)

        if role.position >= interaction.guild.me.top_role.position:
            return await interaction.response.send_message(
                f"❌ The role {role.mention} is higher than or equal to my highest role! Please move my bot role above it in Server Settings -> Roles.",
                ephemeral=True
            )

        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                """INSERT INTO level_roles (guild_id, level, role_id)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id, level) DO UPDATE SET role_id=?""",
                (interaction.guild.id, level, role.id, role.id)
            )
            await conn.commit()

        embed = discord.Embed(
            title="✅ Level Role Configured!",
            description=(
                f"Members will now automatically unlock {role.mention} upon reaching **Level {level}**! 🚀\n\n"
                f"View all configured level roles with `/levelroles`."
            ),
            color=0x57F287
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="removelevelrole", description="Remove a role reward for a level.")
    @app_commands.describe(level="The level whose role reward should be removed")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def removelevelrole(self, interaction: discord.Interaction, level: int):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT role_id FROM level_roles WHERE guild_id=? AND level=?",
                (interaction.guild.id, level)
            ) as cur:
                row = await cur.fetchone()

            if not row:
                return await interaction.response.send_message(f"❌ No level role found for Level {level}.", ephemeral=True)

            await conn.execute("DELETE FROM level_roles WHERE guild_id=? AND level=?", (interaction.guild.id, level))
            await conn.commit()

        role = interaction.guild.get_role(row[0])
        role_name = role.name if role else f"Role {row[0]}"
        await interaction.response.send_message(f"✅ Removed role reward `{role_name}` for **Level {level}**.")

    @app_commands.command(name="levelsettings", description="Configure server level announcements, payout rates and toggles.")
    @app_commands.describe(
        channel="Channel for level up announcements (leave empty to announce where user sent message)",
        dm_notifications="Send level up celebrations directly to user's DMs?",
        enabled="Enable or disable the leveling and activity reward system",
        text_min_coins="Minimum coins earned per text message (default 8)",
        text_max_coins="Maximum coins earned per text message (default 15)",
        vc_coins_per_min="Coins earned per minute active in VC (default 12)"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelsettings(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel = None,
        dm_notifications: bool = None,
        enabled: bool = None,
        text_min_coins: int = None,
        text_max_coins: int = None,
        vc_coins_per_min: int = None
    ):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        current = await self.get_guild_settings(interaction.guild.id)

        new_enabled = current["enabled"] if enabled is None else (1 if enabled else 0)
        new_channel = current["channel_id"] if channel is None else channel.id
        new_dm = current["dm_notifications"] if dm_notifications is None else (1 if dm_notifications else 0)
        new_tmin = current["text_min_coins"] if text_min_coins is None else max(1, text_min_coins)
        new_tmax = current["text_max_coins"] if text_max_coins is None else max(new_tmin, text_max_coins)
        new_vc = current["vc_coins_per_min"] if vc_coins_per_min is None else max(1, vc_coins_per_min)

        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                """INSERT INTO level_settings (guild_id, enabled, channel_id, dm_notifications, text_min_coins, text_max_coins, vc_coins_per_min)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET 
                       enabled=?, channel_id=?, dm_notifications=?, text_min_coins=?, text_max_coins=?, vc_coins_per_min=?""",
                (interaction.guild.id, new_enabled, new_channel, new_dm, new_tmin, new_tmax, new_vc,
                 new_enabled, new_channel, new_dm, new_tmin, new_tmax, new_vc)
            )
            await conn.commit()

        target_ch = interaction.guild.get_channel(new_channel) if new_channel else interaction.guild.get_channel(1547637176697757846)
        ch_text = target_ch.mention if target_ch else "*💬・bot-cmd (Auto-detected)*"

        embed = discord.Embed(
            title="⚙️ Leveling & Reward Settings",
            description="Configuration updated successfully for this server:\n",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="System Status", value="🟢 Enabled" if new_enabled else "🔴 Disabled", inline=True)
        embed.add_field(name="Announcements Channel", value=ch_text, inline=True)
        embed.add_field(name="DM Alerts", value="✅ Enabled" if new_dm else "❌ Disabled", inline=True)
        embed.add_field(name="Text Rewards", value=f"`{new_tmin} - {new_tmax}` 🪙 + Length-scaled XP (10s cd)", inline=True)
        embed.add_field(name="Voice (VC) Rewards", value=f"`{new_vc}` 🪙 + 3 XP / min *(Hardened, AFK >1h stops)*", inline=True)
        embed.set_footer(text="🐾 Server Leveling System • Level up cards post to 💬・bot-cmd")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="setlevel", description="Set a member's level directly (Admin only).")
    @app_commands.describe(member="Member to modify", level="New level to set")
    @app_commands.checks.has_permissions(administrator=True)
    async def setlevel(self, interaction: discord.Interaction, member: discord.Member, level: int):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        if level < 0:
            return await interaction.response.send_message("❌ Level cannot be negative!", ephemeral=True)

        total_xp = total_xp_for_level(level)

        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                """INSERT INTO user_levels (guild_id, user_id, xp, level)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=?, level=?""",
                (interaction.guild.id, member.id, total_xp, level, total_xp, level)
            )
            await conn.commit()

        unlocked = await self.check_and_award_roles(interaction.guild, member, level)
        role_msg = f" (Granted: {', '.join(r.name for r in unlocked)})" if unlocked else ""

        await interaction.response.send_message(
            f"✅ Successfully set {member.mention}'s level to **Level {level}** (`{total_xp:,}` XP){role_msg}."
        )

    @app_commands.command(name="givexp", description="Award bonus XP to a member (Admin only).")
    @app_commands.describe(member="Member to receive XP", amount="Amount of XP to grant")
    @app_commands.checks.has_permissions(administrator=True)
    async def givexp(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        if amount <= 0:
            return await interaction.response.send_message("❌ Amount must be greater than 0!", ephemeral=True)

        user_data = await self.get_user_data(interaction.guild.id, member.id)
        old_xp = user_data["xp"]
        old_lvl = user_data["level"]
        new_xp = old_xp + amount
        new_lvl, _, _ = calculate_level_from_xp(new_xp)

        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                """INSERT INTO user_levels (guild_id, user_id, xp, level)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=?, level=?""",
                (interaction.guild.id, member.id, new_xp, new_lvl, new_xp, new_lvl)
            )
            await conn.commit()

        if new_lvl > old_lvl:
            await self.handle_level_up(interaction.guild, member, old_lvl, new_lvl, interaction.channel)

        await interaction.response.send_message(
            f"✅ Awarded **+{amount:,} XP** to {member.mention}! Current Level: **{new_lvl}** (`{new_xp:,}` XP)."
        )

    @app_commands.command(name="resetlevels", description="Reset leveling stats (Server Owner only).")
    @app_commands.describe(member="Member to reset", reset_all="Reset all server member levels?")
    async def resetlevels(self, interaction: discord.Interaction, member: discord.Member = None, reset_all: bool = False):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        is_owner = (interaction.guild.owner_id == interaction.user.id) or await interaction.client.is_owner(interaction.user)
        if not is_owner:
            return await interaction.response.send_message("❌ **Permission Denied:** Only the **Server Owner** can reset leveling progress!", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as conn:
            if reset_all:
                await conn.execute("DELETE FROM user_levels WHERE guild_id=?", (interaction.guild.id,))
                await conn.commit()
                embed = discord.Embed(
                    title="🧹 Server Leveling Reset",
                    description="✅ All member levels, XP, and activity stats have been reset to **0** by the **Server Owner**.",
                    color=0xED4245
                )
                return await interaction.response.send_message(embed=embed)
            elif member:
                await conn.execute("DELETE FROM user_levels WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
                await conn.commit()
                embed = discord.Embed(
                    title="🧹 Member Level Reset",
                    description=f"✅ Successfully reset leveling stats for {member.mention}.",
                    color=0xED4245
                )
                return await interaction.response.send_message(embed=embed)
            else:
                return await interaction.response.send_message("❌ Please specify a `member` to reset or set `reset_all: True`.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Leveling(bot))
