import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
import time
import asyncio
from utils import db, permissions, rewards, shop
from cogs.economy import update_wallet
from utils.style import send_card, display_names, member_cache_complete
from utils.levels import (
    MAX_LEVEL,
    MAX_VC_COINS_PER_MIN,
    MAX_VC_XP_PER_MIN,
    MAX_XP,
    calculate_level_from_xp,
    clamp_xp,
    total_xp_for_level,
)

DB_PATH = db.DB_PATH

# The two voice-rate ceilings are imported rather than defined here: the
# dashboard's own settings form writes the same row through
# ``db.set_level_settings_sync``, and a second copy of "50" in either place is
# how the page and the bot would end up disagreeing about the rule they are both
# reporting. They live beside the XP curve in ``utils/levels.py``.

# -----------------------------------------------------------------------------
# XP & Level Formula Mechanics
# The formula and the level cap live in utils/levels.py so the bot, the rank
# card and the database integrity repair all share one implementation:
#     xp_needed_for_level(L) = 15L^2 + 100L + 250
#     total_xp_for_level(L)  = 5L^3 + 42.5L^2 + 202.5L   (capped at MAX_LEVEL)
# -----------------------------------------------------------------------------

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
        # Anti-spam history (bounded fingerprints, never raw text) and the voice
        # activity clock. Both prune themselves and never touch the database.
        self._spam = rewards.SpamScreen()
        self._pulse = rewards.PulseClock()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "leveling"):
                await interaction.response.send_message(
                    "⚠️ The **Leveling & XP** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                    ephemeral=True
                )
                return False
        return True

    @commands.Cog.listener()
    async def on_ready(self):
        print("[LEVELS] Leveling cog loaded, starting VC reward loop...", flush=True)
        if not self.vc_reward_loop.is_running():
            self.vc_reward_loop.start()
            print("[LEVELS] VC reward loop started successfully", flush=True)
        else:
            print("[LEVELS] VC reward loop already running", flush=True)

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

        # Older rows can hold NULL in any of these columns (they were added over
        # time), so coerce rather than compare None — `None >= 10` would raise and
        # take the whole reward path down for that guild.
        #
        # Legacy rows used to be rewritten down here (10+ coins/min became 2),
        # which made /levelsettings look like it ignored the admin: setting 12
        # stored 12 and read back as 2. The payout is now the configured rate
        # multiplied by an activity factor, so a silly legacy value is bounded by
        # the documented cap rather than silently replaced.
        vc_c = min(row[6] if row[6] is not None else 2, MAX_VC_COINS_PER_MIN)
        vc_x = min(row[7] if row[7] is not None else 3, MAX_VC_XP_PER_MIN)

        t_min = row[3] if row[3] is not None else 4
        t_max = row[4] if row[4] is not None else 10
        if t_max < t_min:
            t_max = t_min

        return {
            "enabled": row[0] if row[0] is not None else 1,
            "channel_id": row[1] or 0,
            "dm_notifications": row[2] if row[2] is not None else 0,
            "text_min_coins": t_min,
            "text_max_coins": t_max,
            "text_xp": row[5] if row[5] is not None else 15,
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

    #: Dedicated bot-command channel used when an admin hasn't set one with
    #: /levelsettings. Overridable per guild via the `bot_cmd_channel_id` setting.
    BOT_CMD_CHANNEL_ID = 1547637176697757846

    async def _resolve_announce_channel(self, guild: discord.Guild, settings: dict) -> discord.TextChannel | None:
        """Where level-up cards are allowed to be posted.

        Order: the channel an admin chose with /levelsettings, else the dedicated
        bot-command channel. Returns ``None`` when neither exists or the bot can't
        post there, and the caller then posts *nothing* — level-up cards used to
        fall back to a name-matched "cmd" channel, the server system channel, or
        even the channel the member happened to be chatting in, which scattered
        them across the server.
        """
        candidates: list[int] = []

        if settings.get("channel_id"):
            candidates.append(int(settings["channel_id"]))

        # bot_cmd_channel_id was written by /updatedb but never read anywhere, so
        # the dedicated channel was hardcoded. Honour the setting when present.
        try:
            configured = await db.get_setting(guild.id, "bot_cmd_channel_id")
        except Exception:
            configured = None
        candidates.append(int(configured) if configured else self.BOT_CMD_CHANNEL_ID)

        me = getattr(guild, "me", None)
        for channel_id in candidates:
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            if me is None or channel.permissions_for(me).send_messages:
                return channel
        return None

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

        embed.set_footer(text=f"Server Level System • {guild.name}", icon_url=guild.icon.url if guild.icon else None)

        if settings.get("dm_notifications"):
            try:
                await send_card(member, embed=embed)
            except discord.Forbidden:
                pass

        # Level-up cards go to the dedicated command channel only — never the
        # system channel and never "wherever the member happened to be chatting".
        target_channel = await self._resolve_announce_channel(guild, settings)
        if target_channel is None:
            print(
                f"[LEVELS] No announcement channel available in {guild.name}; "
                f"level-up card for {member} was not posted.",
                flush=True,
            )
            return
        try:
            # The one card that SHOULD reach someone: notify the member who
            # levelled up, but never the unlocked roles (a role mention would
            # ping every member who already holds it).
            await send_card(
                target_channel,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except Exception as e:
            print(f"Could not send level up message: {e}")

    # -------------------------------------------------------------------------
    # Text Chat Reward Listener (on_message)
    # -------------------------------------------------------------------------

    async def _count_message(self, guild_id: int, user_id: int, now: float) -> None:
        """Record a message that earns nothing (cooldown, or no content at all).

        The message still counts toward the member's total and refreshes their
        text activity timestamp; only the payout is skipped.
        """
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                """INSERT INTO user_levels (guild_id, user_id, total_messages, last_message_ts)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(guild_id, user_id) DO UPDATE SET 
                       total_messages = total_messages + 1,
                       last_message_ts = ?""",
                (guild_id, user_id, now, now)
            )
            await conn.commit()

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

        # Message Length Based Rewards:
        text = (message.content or "").strip()
        # Without the Message Content privileged intent Discord delivers an
        # empty string for every message. Treat that as an unreadable-but-real
        # message (baseline reward) rather than a 0-character one, otherwise
        # text XP would silently never fire at all.
        text_readable = bool(text)
        attachments = len(message.attachments)
        points = rewards.effort_points(len(text), attachments)
        # An attachment is measurable effort even with no text to read.
        measurable = text_readable or attachments > 0

        # Typing in a text channel while sat in voice is evidence the voice
        # channel is alive, which is what keeps it earning (see the VC loop).
        # Any message counts, including one that earns nothing.
        voice = getattr(message.author, "voice", None)
        if voice is not None and voice.channel is not None:
            self._pulse.touch(voice.channel.id, now)

        # Anti-spam gate 1: at most one reward per cooldown window. Gate 2: never
        # pay for a message with nothing in it ('k', '.', '??'). Both only skip
        # the payout — the message is still counted, and still activity.
        too_soon = now - last_time < rewards.REWARD_COOLDOWN_SECONDS
        too_small = text_readable and points < rewards.MIN_EFFORT_POINTS
        if too_soon or too_small:
            await self._count_message(message.guild.id, message.author.id, now)
            return

        self._chat_cooldowns[key] = now
        # A cooldown only matters for a few seconds, but the dict held one entry
        # per (guild, member) forever — a slow RAM leak on a busy server.
        if len(self._chat_cooldowns) > 4096:
            for stale in [k for k, ts in self._chat_cooldowns.items() if now - ts > 60]:
                self._chat_cooldowns.pop(stale, None)

        # Anti-spam gate 3: the arithmetic. Reposting the same text, a sustained
        # flood and an emoji-only "message" each cut the payout, and the effort
        # curve decides where in the configured band the message lands. Nothing
        # here blocks the message itself — /automod does that.
        stats = self._spam.record(
            message.guild.id, message.author.id, text if text_readable else None, now
        )

        payout = rewards.text_reward(
            content_length=len(text),
            attachments=attachments,
            readable=measurable,
            text_xp=settings["text_xp"],
            text_min_coins=settings["text_min_coins"],
            text_max_coins=settings["text_max_coins"],
            duplicate_ratio=stats.duplicate_ratio,
            tempo_ratio=stats.tempo_ratio,
            low_signal=stats.low_signal,
        )
        # Shop perks (XP Boosters, Coin Charms, permanent badges) scale what
        # activity earns. Cached for a few seconds in utils.shop, because this
        # runs on every message.
        perks = await shop.multipliers(message.author.id, message.guild.id, now)
        coins, xp_gain = perks.scaled(payout.coins, payout.xp)

        try:
            await update_wallet(message.author.id, message.guild.id, coins)
        except Exception as e:
            print(f"Leveling coin deposit error: {e}")

        async with aiosqlite.connect(DB_PATH) as conn:
            # BEGIN IMMEDIATE serialises concurrent writers, so two messages
            # arriving together can't both read the same old XP and lose one.
            await conn.execute("BEGIN IMMEDIATE")
            async with conn.execute(
                "SELECT xp, level, total_messages, total_earned FROM user_levels WHERE guild_id=? AND user_id=?",
                (message.guild.id, message.author.id)
            ) as cur:
                row = await cur.fetchone()

            if not row:
                old_xp, old_lvl, total_msgs, total_earned = 0, 0, 0, 0
            else:
                old_xp, old_lvl, total_msgs, total_earned = row

            new_xp = clamp_xp(old_xp + xp_gain)
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
        """Record observable voice activity for the anti-AFK maths.

        There is no "who is speaking" event — Discord does not expose one — so the
        evidence that a voice channel is alive is what it does send: someone
        joining, leaving, moving, muting, unmuting, deafening, going live or
        turning on a camera, plus anyone typing in a text channel while sat in
        voice (handled in on_message). Those feed the freshness factor; a channel
        with none of them for VC_DEAD_SECONDS stops paying anyone.

        This used to write a free hour into ``last_message_ts`` on join, which let
        a member sit alone in voice and be paid for an hour without ever sending a
        message.
        """
        if member.bot or not member.guild:
            return
        now = time.time()
        
        # Log the voice state change
        before_ch = before.channel.name if before.channel else "None"
        after_ch = after.channel.name if after.channel else "None"
        print(f"[LEVELS] Voice state update: {member.display_name} - {before_ch} -> {after_ch}", flush=True)
        
        for channel in (before.channel, after.channel):
            if channel is not None:
                self._pulse.touch(channel.id, now)
                print(f"[LEVELS] Touched channel #{channel.name} ({channel.id}) for freshness", flush=True)

    # -------------------------------------------------------------------------
    # Voice Channel (VC) Reward Task Loop (every 60 seconds)
    # -------------------------------------------------------------------------

    @tasks.loop(seconds=60)
    async def vc_reward_loop(self):
        """Pay voice minutes by how active the channel demonstrably was.

        Every gate and every factor lives in ``utils/rewards``: a member alone in
        a channel earns nothing, a deafened or server-muted one earns nothing, and
        a channel that has shown no sign of life for hours stops paying. The
        configured per-minute rates are the ceiling, not a flat salary.
        """
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
                
                print(f"[LEVELS] VC reward loop running for {guild.name} - vc_coins: {vc_coins}, vc_xp: {vc_xp}", flush=True)

                channels = guild.voice_channels + getattr(guild, "stage_channels", [])
                vc_count = 0
                member_count = 0
                paid_count = 0
                
                for vc in channels:
                    if guild.afk_channel and vc.id == guild.afk_channel.id:
                        continue

                    vc_count += 1
                    # Anti-AFK gate 1: somebody else has to be in here. A member
                    # alone in a channel is the classic farm, and no amount of
                    # sitting in it is evidence of anything.
                    humans = [m for m in vc.members if not m.bot and m.voice is not None]
                    if len(humans) < rewards.VC_COMPANY_MIN:
                        print(f"[LEVELS] VC #{vc.name} - Skipped: only {len(humans)} human(s) (need {rewards.VC_COMPANY_MIN})", flush=True)
                        continue
                    
                    print(f"[LEVELS] VC #{vc.name} - {len(humans)} human(s) present", flush=True)
                    company = rewards.voice_company_factor(len(humans) - 1)
                    print(f"[LEVELS] VC #{vc.name} - Company factor: {company:.2f}", flush=True)
                    
                    # Anti-AFK gate 2: the channel itself must have shown a sign of
                    # life recently. ``seen`` starts the clock for a channel the bot
                    # has not observed before, so a restart does not read every
                    # occupied channel as dead and stop paying real members.
                    channel_age = self._pulse.seen(vc.id, now)
                    freshness = rewards.voice_freshness(channel_age)
                    print(f"[LEVELS] VC #{vc.name} - Channel age: {channel_age:.0f}s, Freshness: {freshness:.2f}", flush=True)

                    for member in humans:
                        member_count += 1
                        state = member.voice
                        # Anti-AFK gate 3: can this member take part at all? Deafened
                        # (not even receiving audio), server-muted or a suppressed
                        # stage audience means no. Self-muted is half, because
                        # push-to-talk and quiet listening are normal; on camera or
                        # live is proof of presence and lifts the ceiling.
                        presence = rewards.voice_presence(
                            self_deaf=bool(getattr(state, "self_deaf", False)),
                            deaf=bool(getattr(state, "deaf", False)),
                            mute=bool(getattr(state, "mute", False)),
                            self_mute=bool(getattr(state, "self_mute", False)),
                            suppress=bool(getattr(state, "suppress", False)),
                            self_video=bool(getattr(state, "self_video", False)),
                            self_stream=bool(getattr(state, "self_stream", False)),
                        )
                        print(f"[LEVELS] Member {member.display_name} - Presence factor: {presence:.2f} (self_deaf: {getattr(state, 'self_deaf', False)}, deaf: {getattr(state, 'deaf', False)}, mute: {getattr(state, 'mute', False)}, self_mute: {getattr(state, 'self_mute', False)})", flush=True)
                        
                        # The payout maths: the configured per-minute rate is the
                        # ceiling, and this factor is how much of it was earned.
                        factor = rewards.voice_factor(
                            presence=presence, company=company, freshness=freshness
                        )
                        print(f"[LEVELS] Member {member.display_name} - Combined factor: {factor:.2f}", flush=True)
                        
                        if factor <= 0.0:
                            print(f"[LEVELS] Member {member.display_name} - Skipped: factor <= 0", flush=True)
                            continue
                        
                        payout = rewards.voice_reward(factor, vc_xp=vc_xp, vc_coins=vc_coins)
                        print(f"[LEVELS] Member {member.display_name} - Base payout: {payout.coins} coins, {payout.xp} XP", flush=True)
                        
                        perks = await shop.multipliers(member.id, guild.id, now)
                        voice_coins, voice_xp = perks.scaled(payout.coins, payout.xp)
                        print(f"[LEVELS] Member {member.display_name} - Scaled payout: {voice_coins} coins, {voice_xp} XP", flush=True)

                        # Read, decide and write in ONE ``BEGIN IMMEDIATE``
                        # transaction (the shared atomic helper). The old code read on
                        # one connection and wrote on another, so a message arriving
                        # in the same second could overwrite this minute's XP with a
                        # stale value — and a brand-new member was inserted twice.
                        def _vc_tick(cur, xp_gain=voice_xp, coins=voice_coins):
                            cur.execute(
                                "SELECT xp, level, voice_minutes, total_earned, last_message_ts FROM user_levels WHERE guild_id=? AND user_id=?",
                                (guild.id, member.id),
                            )
                            row = cur.fetchone()
                            if not row:
                                old_xp, old_lvl, v_mins, total_earned, last_msg_ts = 0, 0, 0, 0, 0
                            else:
                                old_xp, old_lvl, v_mins, total_earned, last_msg_ts = row
                                last_msg_ts = last_msg_ts or 0

                            new_xp = clamp_xp((old_xp or 0) + xp_gain)
                            new_vmins = (v_mins or 0) + 1
                            new_earned = (total_earned or 0) + coins
                            new_lvl, _, _ = calculate_level_from_xp(new_xp)

                            cur.execute(
                                """INSERT INTO user_levels (guild_id, user_id, xp, level, voice_minutes, total_earned, last_message_ts)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)
                                   ON CONFLICT(guild_id, user_id) DO UPDATE SET
                                       xp=?, level=?, voice_minutes=?, total_earned=?""",
                                (guild.id, member.id, new_xp, new_lvl, new_vmins, new_earned, last_msg_ts,
                                 new_xp, new_lvl, new_vmins, new_earned),
                            )
                            return old_lvl, new_lvl

                        tick = await asyncio.to_thread(db.run_atomic, _vc_tick)
                        if tick is None:
                            print(f"[LEVELS] Member {member.display_name} - Skipped: atomic transaction failed", flush=True)
                            continue
                        old_lvl, new_lvl = tick
                        paid_count += 1

                        try:
                            await update_wallet(member.id, guild.id, voice_coins)
                            print(f"[LEVELS] Member {member.display_name} - Paid: +{voice_coins} coins, +{voice_xp} XP (Level {old_lvl} -> {new_lvl})", flush=True)
                        except Exception as e:
                            print(f"VC coin deposit error: {e}")

                        if new_lvl > old_lvl:
                            await self.handle_level_up(guild, member, old_lvl, new_lvl, None)
                
                print(f"[LEVELS] VC reward loop complete for {guild.name} - {vc_count} channels checked, {member_count} members evaluated, {paid_count} paid", flush=True)

            except Exception as e:
                print(f"Error in vc_reward_loop for guild {guild.id}: {e}", flush=True)

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

        at_max_level = level >= MAX_LEVEL
        progress_text = "MAX LEVEL" if at_max_level else f"{current_xp:,} / {needed_xp:,} XP"
        bar = "`[▰▰▰▰▰▰▰▰▰▰▰▰]` **MAX**" if at_max_level else render_progress_bar(current_xp, needed_xp, length=12)

        embed = discord.Embed(
            title=f"{badge} {target.display_name}'s Rank Card",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name=f"{target.name} • {tier_name}", icon_url=target.display_avatar.url)
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.description = (
            f"### 🏆 **Level {level}** • **Rank #{rank_pos}** of {total_members}\n"
            f"**Progress:** {progress_text}\n"
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
            role_info.append("• *No level roles configured yet (Admins: use `/levelroles add`)*")

        if role_info:
            embed.add_field(name="🎖️ Level Roles", value="\n".join(role_info), inline=False)

        embed.set_footer(text=f"Total Cumulative XP: {total_xp:,} XP")
        await send_card(interaction, embed=embed)

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
        # Plain resolved names, not mentions — in a V2 card a mention notifies
        # every member on the board just because someone asked for the rankings.
        names = await display_names(interaction.guild, [row[0] for row in rows], client=interaction.client)

        for idx, row in enumerate(rows):
            uid, u_xp, u_lvl, u_msgs, u_vmins, u_earned = row
            display = names[idx]
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
        embed.set_footer(text="Chat in text or chill in VC to climb the ranks!")
        await send_card(interaction, embed=embed)

    # Viewing, adding and removing level-role rewards were three top-level
    # commands. They are one feature with three verbs, and at Discord's
    # hundred-command ceiling each slot spent on a verb is a feature that cannot
    # exist — as a group they cost one slot instead of three, and the audience of
    # each verb is still declared separately (``levelroles add`` needs Manage
    # Roles, viewing needs nothing).
    levelroles_group = app_commands.Group(
        name="levelroles",
        description="Level role rewards: the list, adding one, and removing one.",
    )

    @levelroles_group.command(name="view", description="Show all configured level roles and rewards in this server.")
    async def levelroles_view(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        roles = await self.get_level_roles(interaction.guild.id)
        if not roles:
            return await interaction.response.send_message(
                "📜 No level roles configured for this server yet!\n"
                "Admins can set up role rewards using `/levelroles add level:<number> role:@Role`.",
                ephemeral=True
            )

        embed = discord.Embed(
            title=f"🎖️ {interaction.guild.name} — Level Role Rewards",
            description="Reaching these levels automatically awards the corresponding roles:\n",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        # ``role.members`` is only accurate when the Members intent is on;
        # otherwise the cache holds almost nobody and every role would read
        # "(0 members)". Report the count only when it can be trusted.
        show_counts = member_cache_complete(interaction.client)
        for req_lvl, r_id in roles:
            role = interaction.guild.get_role(r_id)
            role_str = role.mention if role else f"`[Deleted Role ID {r_id}]`"
            members_note = f" *({len(role.members)} members)*" if (show_counts and role) else ""
            _, badge, _ = get_tier_info(req_lvl)
            embed.description += f"• **Level {req_lvl}** {badge} ➔ {role_str}{members_note}\n"

        embed.set_footer(text="Roles are granted instantly when you level up!")
        await send_card(interaction, embed=embed)

    # -------------------------------------------------------------------------
    # Admin / Management Commands
    # -------------------------------------------------------------------------

    @levelroles_group.command(name="add", description="Set a role reward to be automatically granted at a level.")
    @app_commands.describe(level="The required level", role="The role to grant")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def levelroles_add(self, interaction: discord.Interaction, level: int, role: discord.Role):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        if level < 1:
            return await interaction.response.send_message("❌ Level must be 1 or higher!", ephemeral=True)

        # A level role is granted automatically and forever, so it is at least as
        # sensitive as a one-off /addrole: wiring **Administrator** to level 1
        # would hand the whole server out to anybody who chats for an hour.
        problem = permissions.role_grant_problem(role, interaction.user, interaction.guild)
        if problem:
            return await interaction.response.send_message(f"❌ {problem}", ephemeral=True)

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
                f"View all configured level roles with `/levelroles view`."
            ),
            color=0x57F287
        )
        await send_card(interaction, embed=embed)

    @levelroles_group.command(name="remove", description="Remove a role reward for a level.")
    @app_commands.describe(level="The level whose role reward should be removed")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def levelroles_remove(self, interaction: discord.Interaction, level: int):
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
        text_min_coins="Minimum coins per qualifying message (default 4)",
        text_max_coins="Maximum coins per qualifying message (default 10)",
        vc_coins_per_min=f"Coins per minute in voice, {MAX_VC_COINS_PER_MIN} max (default 2)"
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
        new_vc = current["vc_coins_per_min"] if vc_coins_per_min is None else max(
            1, min(MAX_VC_COINS_PER_MIN, vc_coins_per_min)
        )
        vc_xp = current["vc_xp_per_min"]

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

        target_ch = interaction.guild.get_channel(new_channel) if new_channel else interaction.guild.get_channel(self.BOT_CMD_CHANNEL_ID)
        ch_text = target_ch.mention if target_ch else "*💬・bot-cmd (no channel set)*"

        embed = discord.Embed(
            title="⚙️ Leveling & Reward Settings",
            description="Configuration updated successfully for this server:\n",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="System Status", value="🟢 Enabled" if new_enabled else "🔴 Disabled", inline=True)
        embed.add_field(name="Announcements Channel", value=ch_text, inline=True)
        embed.add_field(name="DM Alerts", value="✅ Enabled" if new_dm else "❌ Disabled", inline=True)
        embed.add_field(
            name="Text Rewards",
            value=(
                f"`{new_tmin} - {new_tmax}` 🪙 + {rewards.BASE_MULTIPLIER:g}×-{rewards.TOP_MULTIPLIER:g}× XP\n"
                f"*effort-scaled, spam-discounted, {rewards.REWARD_COOLDOWN_SECONDS:g}s cooldown*"
            ),
            inline=True
        )
        embed.add_field(
            name="Voice (VC) Rewards",
            value=(
                f"up to `{new_vc}` 🪙 + {vc_xp} XP / min\n"
                "*needs undeafened + recent activity — solo earns at reduced rate*"
            ),
            inline=True
        )
        embed.set_footer(text="Server Leveling System • Level up cards post to 💬・bot-cmd")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="setlevel", description="Set a member's level directly (Admin only).")
    @app_commands.describe(member="Member to modify", level="New level to set")
    @app_commands.checks.has_permissions(administrator=True)
    async def setlevel(self, interaction: discord.Interaction, member: discord.Member, level: int):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        if level < 0:
            return await interaction.response.send_message("❌ Level cannot be negative!", ephemeral=True)
        if level > MAX_LEVEL:
            return await interaction.response.send_message(
                f"❌ The maximum level is **{MAX_LEVEL}**. Setting anything higher would break the leaderboard and rank cards.",
                ephemeral=True
            )

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

        # Plain name, not a mention: this is a public message, and the member is
        # already told about a real level-up by the announcement card.
        await interaction.response.send_message(
            f"✅ Successfully set **{member.display_name}**'s level to **Level {level}** (`{total_xp:,}` XP){role_msg}."
        )

    @app_commands.command(name="givexp", description="Award bonus XP to a member (Admin only).")
    @app_commands.describe(member="Member to receive XP", amount="Amount of XP to grant")
    @app_commands.checks.has_permissions(administrator=True)
    async def givexp(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        if amount <= 0:
            return await interaction.response.send_message("❌ Amount must be greater than 0!", ephemeral=True)

        from utils.admin_limits import needs_approval, ADMIN_GIVEXP_LIMIT, request_approval

        if needs_approval(amount, ADMIN_GIVEXP_LIMIT):
            # Request owner approval
            async def execute():
                user_data = await self.get_user_data(interaction.guild.id, member.id)
                old_xp = user_data["xp"]
                old_lvl = user_data["level"]
                new_xp = clamp_xp(old_xp + amount)
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
            return await request_approval(
                interaction, member, "🪙 XP", amount, execute,
                limit=ADMIN_GIVEXP_LIMIT,
            )

        user_data = await self.get_user_data(interaction.guild.id, member.id)
        old_xp = user_data["xp"]
        old_lvl = user_data["level"]
        new_xp = clamp_xp(old_xp + amount)
        new_lvl, _, _ = calculate_level_from_xp(new_xp)
        if new_xp == old_xp:
            return await interaction.response.send_message(
                f"ℹ️ {member.mention} is already at the maximum of **Level {MAX_LEVEL}** (`{MAX_XP:,}` XP).",
                ephemeral=True
            )

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
            f"✅ Awarded **+{amount:,} XP** to **{member.display_name}**! Current Level: **{new_lvl}** (`{new_xp:,}` XP)."
        )

    @app_commands.command(name="resetlevels", description="Reset leveling stats (Server Owner only).")
    @app_commands.describe(member="Member to reset", reset_all="Reset all server member levels?")
    @app_commands.checks.has_permissions(administrator=True)
    async def resetlevels(self, interaction: discord.Interaction, member: discord.Member = None, reset_all: bool = False):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        # Same missing-gate history as `/eco-admin set`: only the owner check
        # below, which the bot owner passes in *every* server. Both gates now
        # apply, and the refusal lands visibly in the channel it was tried in.
        if not permissions.has_admin(interaction):
            return await permissions.deny_admin(interaction, "reset leveling stats")
        is_owner = (interaction.guild.owner_id == interaction.user.id) or await interaction.client.is_owner(interaction.user)
        if not is_owner:
            return await permissions.deny_admin(interaction, "reset leveling stats (Server Owner only)")

        async with aiosqlite.connect(DB_PATH) as conn:
            if reset_all:
                await conn.execute("DELETE FROM user_levels WHERE guild_id=?", (interaction.guild.id,))
                await conn.commit()
                embed = discord.Embed(
                    title="🧹 Server Leveling Reset",
                    description="✅ All member levels, XP, and activity stats have been reset to **0** by the **Server Owner**.",
                    color=0xED4245
                )
                return await send_card(interaction, embed=embed)
            elif member:
                await conn.execute("DELETE FROM user_levels WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
                await conn.commit()
                embed = discord.Embed(
                    title="🧹 Member Level Reset",
                    description=f"✅ Successfully reset leveling stats for {member.mention}.",
                    color=0xED4245
                )
                return await send_card(interaction, embed=embed)
            else:
                return await interaction.response.send_message("❌ Please specify a `member` to reset or set `reset_all: True`.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Leveling(bot))
