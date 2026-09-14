import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import asyncio
import random
import datetime
import time
from utils import db, loans, meowfee, shop
from utils.style import send_card, display_names

DB_PATH = db.DB_PATH

# Unlimited economy: players can earn and hold as much money as they want!
_bot_instance: commands.Bot | None = None

async def setup_tables():
    # Tables are now created in utils/db.setup() to avoid duplication
    pass

async def sanitize_economy_balances(bot: commands.Bot):
    """Unlimited economy balances for all members."""
    pass

async def get_eco(user_id: int, guild_id: int, is_owner: bool | None = None):
    await ensure_eco(user_id, guild_id)
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT wallet, bank, last_daily, last_weekly, last_work FROM economy WHERE user_id=? AND guild_id=?",
            (user_id, guild_id)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return {"wallet": 0, "bank": 0, "last_daily": None, "last_weekly": None, "last_work": None}

    return {"wallet": row[0], "bank": row[1], "last_daily": row[2], "last_weekly": row[3], "last_work": row[4]}

async def ensure_eco(user_id: int, guild_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO economy (user_id, guild_id) VALUES (?, ?)",
            (user_id, guild_id)
        )
        await conn.commit()

async def check_is_owner(interaction: discord.Interaction) -> bool:
    if interaction.guild and interaction.guild.owner_id == interaction.user.id:
        return True
    if await interaction.client.is_owner(interaction.user):
        return True
    return False

async def update_wallet(user_id: int, guild_id: int, amount: int, is_owner: bool | None = None):
    await ensure_eco(user_id, guild_id)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE economy SET wallet = MAX(0, wallet + ?) WHERE user_id=? AND guild_id=?",
            (amount, user_id, guild_id)
        )
        await conn.commit()

async def take_stake(user_id: int, guild_id: int, bet: int) -> bool:
    """Debit a wager, refusing if the wallet no longer covers it.

    Used by the multi-step games (blackjack, mines, high-low, tic-tac-toe) that
    hold the stake for the length of the game. A plain ``update_wallet(-bet)``
    clamps at zero, so a second bet fired at the same instant could start a
    second game without paying for it.
    """
    return await settle_bet(user_id, guild_id, bet, 0.0)


async def settle_bet(user_id: int, guild_id: int, bet: int, multiplier: float) -> bool:
    """Hold the stake and pay out in one transaction — the only safe way to
    resolve a casino bet.

    Every instant game used to apply a *net delta* to a balance it had read
    earlier (``wallet += bet`` on a win, ``-= bet`` on a loss). Because the stake
    was never actually taken, the same coins could be wagered by several
    simultaneous invocations and every win minted new coins. Here the stake is
    debited conditionally, then the payout is credited, all inside one
    ``BEGIN IMMEDIATE`` transaction, so a bet can only ever be backed by coins
    that were really there.

    ``multiplier``: 0 loses the stake, 1 is a push (stake returned), 2 doubles.
    Returns False when the stake could not be taken (the wallet changed).
    """
    await ensure_eco(user_id, guild_id)
    payout = int(bet * multiplier)

    def _settle(cur):
        cur.execute(
            "UPDATE economy SET wallet = wallet - ? WHERE user_id=? AND guild_id=? AND wallet >= ?",
            (bet, user_id, guild_id, bet),
        )
        if cur.rowcount != 1:
            return False
        if payout > 0:
            cur.execute(
                "UPDATE economy SET wallet = wallet + ? WHERE user_id=? AND guild_id=?",
                (payout, user_id, guild_id),
            )
        return True

    return await asyncio.to_thread(db.run_atomic, _settle)


ALLOWED_ECONOMY_FIELDS = {"wallet", "bank", "last_daily", "last_weekly", "last_work"}

async def set_field(user_id: int, guild_id: int, field: str, value):
    if field not in ALLOWED_ECONOMY_FIELDS:
        raise ValueError(f"Security violation: Invalid economy field {field}")
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            f"UPDATE economy SET {field}=? WHERE user_id=? AND guild_id=?",
            (value, user_id, guild_id)
        )
        await conn.commit()

WORK_JOBS = [
    ("🧑‍💻 Coding", 150, 350),
    ("🍕 Pizza Delivery", 80, 200),
    ("🎨 Graphic Design", 120, 300),
    ("🎮 Game Testing", 100, 250),
    ("📦 Warehouse Work", 90, 220),
    ("🚗 Uber Driver", 110, 270),
    ("🎵 Street Musician", 50, 180),
    ("📸 Photography", 130, 320),
]

# --------------------------------------------------------------------------- #
# /rob tuning
# --------------------------------------------------------------------------- #
# The take and the penalty come from the *same* roll, so a robbery is no longer a
# one-sided gamble: going for 2,000 coins costs you 200 if you get caught.
ROB_SUCCESS_CHANCE = 0.45       # odds of a clean getaway
ROB_FINE_RATIO = 0.10           # a failed attempt costs 10% of what you went for
ROB_MIN_FINE = 25
ROB_MAX_TAKE = 50_000
ROB_COOLDOWN_SECONDS = 30 * 60  # per user, per server
ROB_MAX_SUCCESS_CHANCE = 0.90   # a luck perk can never make a robbery certain


def parse_amount(amount_str: str, current: int) -> int | None:
    amount_str = amount_str.strip().lower()
    if amount_str in ("all", "max"):
        return current
    if amount_str.endswith("k"):
        try:
            return int(float(amount_str[:-1]) * 1_000)
        except ValueError:
            return None
    if amount_str.endswith("m"):
        try:
            return int(float(amount_str[:-1]) * 1_000_000)
        except ValueError:
            return None
    if amount_str.endswith("b"):
        try:
            return int(float(amount_str[:-1]) * 1_000_000_000)
        except ValueError:
            return None
    try:
        val = int(amount_str)
        return val if val > 0 else None
    except ValueError:
        return None

async def member_level(user_id: int, guild_id: int) -> int:
    """A member's level, or 0 when they have no row yet.

    Drives the loan credit line. A missing table (an old database that never ran
    the leveling migration) degrades to level 0 rather than taking /loan down
    with it.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT level FROM user_levels WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ) as cur:
                row = await cur.fetchone()
        return max(0, int(row[0] or 0)) if row else 0
    except aiosqlite.Error as exc:
        print(f"[LOANS] Could not read level for {user_id}: {exc}", flush=True)
        return 0


def _charge_late_fee(cur, user_id: int, guild_id: int, now: float) -> int:
    """Apply the one-time overdue penalty inside the caller's transaction.

    Returns the penalty that was charged (0 when nothing was due). The
    ``late_fee_applied = 0`` guard means it can land exactly once, and the
    surrounding ``BEGIN IMMEDIATE`` means only one writer can be in here at a
    time — so a member cannot be penalised twice by two commands racing.
    """
    cur.execute(
        """SELECT repay_amount FROM loans
            WHERE user_id=? AND guild_id=? AND late_fee_applied = 0
              AND due_ts > 0 AND due_ts < ?""",
        (user_id, guild_id, now - loans.GRACE_SECONDS),
    )
    row = cur.fetchone()
    if not row:
        return 0
    before = row[0]

    cur.execute(
        f"""UPDATE loans
               SET repay_amount = repay_amount + {loans.late_fee_sql()},
                   late_fee_applied = 1,
                   status = 'overdue'
             WHERE user_id=? AND guild_id=? AND late_fee_applied = 0""",
        (user_id, guild_id),
    )
    if cur.rowcount != 1:
        return 0

    # Report the fee the database actually stored, rather than re-deriving it in
    # Python where a rounding difference could drift from the SQL expression.
    cur.execute(
        "SELECT repay_amount FROM loans WHERE user_id=? AND guild_id=?",
        (user_id, guild_id),
    )
    after = cur.fetchone()
    return (after[0] - before) if after else 0


class Economy(commands.Cog):
    """Advanced economy system with bank, rob, shop, and leaderboard."""

    def __init__(self, bot: commands.Bot):
        global _bot_instance
        self.bot = bot
        _bot_instance = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "economy"):
                await interaction.response.send_message(
                    "⚠️ The **Economy & Casino** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True
                )
                return False
        return True

    @commands.Cog.listener()
    async def on_ready(self):
        await sanitize_economy_balances(self.bot)

    def _bal_embed(self, member: discord.Member, wallet: int, bank: int, is_owner: bool = False) -> discord.Embed:
        embed = discord.Embed(
            title=f"💰 {member.display_name}'s Balance",
            color=0xFEE75C,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="👛 Wallet", value=f"**{wallet:,}** 🪙", inline=True)
        embed.add_field(name="🏦 Bank", value=f"**{bank:,}** 🪙", inline=True)
        embed.add_field(name="💎 Net Worth", value=f"**{wallet + bank:,}** 🪙", inline=False)
        embed.set_footer(text="🐾 Meow Economy • Unlimited Wallet & Bank")
        return embed

    @app_commands.command(name="balance", description="Check your or someone's balance.")
    @app_commands.describe(member="The member to check")
    async def balance(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        is_member_owner = False
        if interaction.guild and interaction.guild.owner_id == member.id:
            is_member_owner = True
        elif await interaction.client.is_owner(member):
            is_member_owner = True

        await ensure_eco(member.id, interaction.guild.id)
        eco = await get_eco(member.id, interaction.guild.id, is_owner=is_member_owner)
        await send_card(interaction, 
            embed=self._bal_embed(member, eco["wallet"], eco["bank"], is_owner=is_member_owner)
        )

    @app_commands.command(name="daily", description="Claim your daily 500 coins.")
    async def daily(self, interaction: discord.Interaction):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        now = discord.utils.utcnow()
        if eco["last_daily"]:
            last = datetime.datetime.fromisoformat(eco["last_daily"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
            diff = now - last
            if diff.total_seconds() < 86400:
                remaining = 86400 - diff.total_seconds()
                h, m = divmod(int(remaining // 60), 60)
                embed = discord.Embed(description=f"⏰ **You've already claimed your daily!**\nCome back in **{h}h {m}m**.", color=0xED4245)
                return await send_card(interaction, embed=embed, ephemeral=True)

        bonus = random.randint(450, 600)
        # Optimistic claim: only pay out if last_daily is still exactly what we
        # read above. Two /daily fired at the same instant would otherwise both
        # pass the cooldown check and pay out twice.
        def _claim(cur):
            cur.execute(
                "UPDATE economy SET wallet = wallet + ?, last_daily = ? WHERE user_id=? AND guild_id=? AND last_daily IS ?",
                (bonus, now.isoformat(), interaction.user.id, interaction.guild.id, eco["last_daily"])
            )
            return cur.rowcount

        claimed = await asyncio.to_thread(db.run_atomic, _claim)
        if claimed != 1:
            embed = discord.Embed(description="⏰ **You've already claimed your daily!**", color=0xED4245)
            return await send_card(interaction, embed=embed, ephemeral=True)
        embed = discord.Embed(
            title="📅 Daily Reward Claimed!",
            description=f"You received **+{bonus:,}** 🪙 coins in your wallet!",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Come back in 24 hours for your next reward!")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="weekly", description="Claim your weekly 2000 coins.")
    async def weekly(self, interaction: discord.Interaction):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        now = discord.utils.utcnow()
        if eco["last_weekly"]:
            last = datetime.datetime.fromisoformat(eco["last_weekly"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
            diff = now - last
            if diff.total_seconds() < 604800:
                remaining = 604800 - diff.total_seconds()
                d, rem = divmod(int(remaining), 86400)
                h = rem // 3600
                embed = discord.Embed(description=f"⏰ **You've already claimed your weekly!**\nCome back in **{d}d {h}h**.", color=0xED4245)
                return await send_card(interaction, embed=embed, ephemeral=True)

        bonus = random.randint(1800, 2500)
        # Same optimistic claim as /daily — see the comment there.
        def _claim(cur):
            cur.execute(
                "UPDATE economy SET wallet = wallet + ?, last_weekly = ? WHERE user_id=? AND guild_id=? AND last_weekly IS ?",
                (bonus, now.isoformat(), interaction.user.id, interaction.guild.id, eco["last_weekly"])
            )
            return cur.rowcount

        claimed = await asyncio.to_thread(db.run_atomic, _claim)
        if claimed != 1:
            embed = discord.Embed(description="⏰ **You've already claimed your weekly!**", color=0xED4245)
            return await send_card(interaction, embed=embed, ephemeral=True)
        embed = discord.Embed(
            title="📆 Weekly Reward Claimed!",
            description=f"You received **+{bonus:,}** 🪙 coins in your wallet!",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Come back in 7 days for your next reward!")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="work", description="Work a job to earn coins.")
    async def work(self, interaction: discord.Interaction):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        now = discord.utils.utcnow()
        if eco["last_work"]:
            last = datetime.datetime.fromisoformat(eco["last_work"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
            diff = now - last
            if diff.total_seconds() < 3600:
                remaining = 3600 - diff.total_seconds()
                m = int(remaining // 60)
                embed = discord.Embed(description=f"⏰ **You need to rest!**\nYou can work again in **{m} minute(s)**.", color=0xED4245)
                return await send_card(interaction, embed=embed, ephemeral=True)

        job, min_pay, max_pay = random.choice(WORK_JOBS)
        earned = random.randint(min_pay, max_pay)
        # Same optimistic claim as /daily — see the comment there.
        def _claim(cur):
            cur.execute(
                "UPDATE economy SET wallet = wallet + ?, last_work = ? WHERE user_id=? AND guild_id=? AND last_work IS ?",
                (earned, now.isoformat(), interaction.user.id, interaction.guild.id, eco["last_work"])
            )
            return cur.rowcount

        claimed = await asyncio.to_thread(db.run_atomic, _claim)
        if claimed != 1:
            embed = discord.Embed(description="⏰ **You need to rest!** Try again shortly.", color=0xED4245)
            return await send_card(interaction, embed=embed, ephemeral=True)
        embed = discord.Embed(
            title="💼 Shift Completed!",
            description=f"You worked as a **{job}** and earned **+{earned:,}** 🪙!",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Shift cooldown: 1 hour")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="rob", description="Attempt to rob another member's wallet (a failed attempt costs 10% of what you went for).")
    @app_commands.describe(member="Who to rob")
    @app_commands.checks.cooldown(1, ROB_COOLDOWN_SECONDS, key=lambda i: (i.guild_id, i.user.id))
    async def rob(self, interaction: discord.Interaction, member: discord.Member):
        if member.id == interaction.user.id:
            return await interaction.response.send_message("You can't rob yourself!", ephemeral=True)
        if member.bot:
            return await interaction.response.send_message("You can't rob a bot!", ephemeral=True)

        await ensure_eco(interaction.user.id, interaction.guild.id)
        await ensure_eco(member.id, interaction.guild.id)
        robber = await get_eco(interaction.user.id, interaction.guild.id)
        victim = await get_eco(member.id, interaction.guild.id)

        # A Rob Shield now actually shields. The attempt is spent (the cooldown
        # ticks), but nothing changes hands.
        if await shop.has_shield(member.id, interaction.guild.id):
            embed = discord.Embed(
                title="🛡️ Shielded!",
                description=(
                    f"{member.mention} is behind a **Rob Shield**, so your attempt "
                    "bounced straight off.\n\n> Nothing lost — but the cooldown still counts."
                ),
                color=0x5865F2,
            )
            return await send_card(interaction, embed=embed)

        if victim["wallet"] < 100:
            embed = discord.Embed(description=f"💸 {member.mention} is broke! Not worth robbing.", color=0xFF3333)
            return await send_card(interaction, embed=embed)
        if robber["wallet"] < 200:
            embed = discord.Embed(description="❌ You need at least **200** 🪙 in your wallet to attempt a robbery (bail money).", color=0xFF3333)
            return await send_card(interaction, embed=embed, ephemeral=True)

        # One roll sets the stakes, so the reward and the risk scale together
        # instead of "risk 100-300, win up to 50,000".
        take = random.randint(int(victim["wallet"] * 0.1), int(victim["wallet"] * 0.4))
        take = max(10, min(take, ROB_MAX_TAKE))
        risk = max(ROB_MIN_FINE, int(round(take * ROB_FINE_RATIO)))

        # Luck from the shop adds straight onto the base odds, capped so no perk
        # makes a heist a certainty.
        luck = await shop.luck_points(interaction.user.id, interaction.guild.id)
        success_chance = min(ROB_MAX_SUCCESS_CHANCE, ROB_SUCCESS_CHANCE + luck / 100.0)

        # The loot is a transfer between members, so the Meow Fee applies to it
        # too — taken out of the robber's proceeds, never from the victim, who
        # loses the same amount either way. Read before the transaction for the
        # same reason as /pay.
        rob_fee = meowfee.fee_for(take) if await meowfee.is_enabled(interaction.guild.id) else 0
        payout = take - rob_fee

        if random.random() < success_chance:
            # Debit the victim conditionally inside one transaction, then credit the
            # robber and the bank — otherwise the trio could mint coins (or take the
            # victim's wallet below zero) if any wallet moved in between the reads.
            def _rob(cur):
                cur.execute(
                    "UPDATE economy SET wallet = wallet - ? WHERE user_id=? AND guild_id=? AND wallet >= ?",
                    (take, member.id, interaction.guild.id, take)
                )
                ok = cur.rowcount == 1
                if ok:
                    cur.execute(
                        "UPDATE economy SET wallet = wallet + ? WHERE user_id=? AND guild_id=?",
                        (payout, interaction.user.id, interaction.guild.id)
                    )
                    meowfee.credit(cur, interaction.guild.id, rob_fee)
                return ok

            robbed_ok = await asyncio.to_thread(db.run_atomic, _rob)
            if not robbed_ok:
                embed = discord.Embed(title="🚔 Caught!", description=f"{member.mention}'s wallet changed mid-robbery — you got nothing!", color=0xFF3333)
                return await send_card(interaction, embed=embed)
            fee_note = (
                f"> 🐾 **Meow Fee:** `{rob_fee:,}` 🪙 to Meow Bank — you keep `{payout:,}` 🪙\n"
                if rob_fee else ""
            )
            embed = discord.Embed(
                title="🦹 Successful Robbery!",
                description=(
                    f"You robbed **{take:,}** 🪙 from {member.mention}!\n\n"
                    f"> 🎯 **Take:** `{take:,}` 🪙\n"
                    f"{fee_note}"
                    f"> ⚖️ **Risked if caught:** `{risk:,}` 🪙"
                ),
                color=0x2ECC71,
            )
        else:
            fine = min(risk, robber["wallet"])  # you can't pay what you don't have
            # Same conditional pattern in reverse: the robber pays the victim.
            def _fine(cur):
                cur.execute(
                    "UPDATE economy SET wallet = wallet - ? WHERE user_id=? AND guild_id=? AND wallet >= ?",
                    (fine, interaction.user.id, interaction.guild.id, fine)
                )
                ok = cur.rowcount == 1
                if ok:
                    cur.execute(
                        "UPDATE economy SET wallet = wallet + ? WHERE user_id=? AND guild_id=?",
                        (fine, member.id, interaction.guild.id)
                    )
                return ok

            paid = await asyncio.to_thread(db.run_atomic, _fine)
            if paid:
                detail = (
                    f"You got caught robbing {member.mention} and paid a **{fine:,}** 🪙 fine!\n\n"
                    f"> 🎯 **Attempted take:** `{take:,}` 🪙\n"
                    f"> ⚖️ **Fine:** `{fine:,}` 🪙 ({int(ROB_FINE_RATIO * 100)}% of what you went for)"
                )
            else:
                detail = f"You got caught robbing {member.mention}, but your wallet had already changed — the fine couldn't be collected."
            embed = discord.Embed(title="🚔 Caught!", description=detail, color=0xFF3333)

        luck_note = f" (+{luck}% from perks)" if luck else ""
        embed.set_footer(text=f"🐾 Rob cooldown: {ROB_COOLDOWN_SECONDS // 60} minutes • Success chance: {int(success_chance * 100)}%{luck_note}")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="pay", description="Transfer coins to another member (a small Meow Fee goes to Meow Bank).")
    @app_commands.describe(member="Who to pay", amount="Amount to send")
    async def pay(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, None]):
        if member.id == interaction.user.id:
            return await interaction.response.send_message("You can't pay yourself!", ephemeral=True)
        if member.bot:
            return await interaction.response.send_message("You can't pay a bot!", ephemeral=True)

        is_sender_owner = await check_is_owner(interaction)

        await ensure_eco(interaction.user.id, interaction.guild.id)
        await ensure_eco(member.id, interaction.guild.id)
        sender_eco = await get_eco(interaction.user.id, interaction.guild.id, is_owner=is_sender_owner)

        # The Meow Fee is read *before* the transaction, because the switch lives
        # in another table and a transaction must not wait on a second
        # connection. It is charged on top of the amount, so the recipient
        # receives exactly what they were sent.
        fee = meowfee.fee_for(amount) if await meowfee.is_enabled(interaction.guild.id) else 0
        total = amount + fee

        if sender_eco["wallet"] < total:
            short = (
                f" (that is **{amount:,}** 🪙 plus the **{fee:,}** 🪙 Meow Fee)"
                if fee else ""
            )
            return await interaction.response.send_message(
                f"❌ You only have **{sender_eco['wallet']:,}** 🪙 in your wallet{short}.",
                ephemeral=True,
            )

        # Debit, credit and bank the fee inside one transaction, and only if the
        # balance still covers all of it. Two parallel /pay calls used to both
        # pass the read above and duplicate coins.
        def _pay(cur):
            cur.execute(
                "UPDATE economy SET wallet = wallet - ? WHERE user_id=? AND guild_id=? AND wallet >= ?",
                (total, interaction.user.id, interaction.guild.id, total)
            )
            moved = cur.rowcount
            if moved == 1:
                cur.execute(
                    "UPDATE economy SET wallet = wallet + ? WHERE user_id=? AND guild_id=?",
                    (amount, member.id, interaction.guild.id)
                )
                # Banked on the same cursor, so the coins cannot leave a wallet
                # without reaching the bank.
                meowfee.credit(cur, interaction.guild.id, fee)
            return moved

        paid = await asyncio.to_thread(db.run_atomic, _pay)

        if paid != 1:
            return await interaction.response.send_message(
                "❌ Your wallet changed while that was processing — please try again.", ephemeral=True
            )

        detail = f"✅ Sent **{amount:,}** 🪙 to {member.mention}!"
        if fee:
            detail += (
                f"\n\n> 🐾 **Meow Fee:** `{fee:,}` 🪙 ({meowfee.fee_label()}) "
                "to Meow Bank\n"
                f"> 💸 **You paid:** `{total:,}` 🪙"
            )
        embed = discord.Embed(description=detail, color=0x2ECC71, timestamp=discord.utils.utcnow())
        await send_card(interaction, embed=embed)

    # The handler is named ``meowfee_cmd`` on purpose: binding the class attribute
    # to ``meowfee`` would shadow the imported ``utils.meowfee`` module for the
    # rest of this class body, which is exactly how ``/eco-admin``'s handlers once
    # became unreachable dead code. A slash command's name comes from its
    # decorator, so nothing user-facing depends on the attribute name.
    @app_commands.command(
        name="meowfee",
        description="See or change the Meow Fee on transfers (admin).",
    )
    @app_commands.describe(state="Turn the fee on or off — leave empty to just look")
    @app_commands.choices(
        state=[
            app_commands.Choice(name="On", value="on"),
            app_commands.Choice(name="Off", value="off"),
        ]
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def meowfee_cmd(
        self, interaction: discord.Interaction, state: app_commands.Choice[str] | None = None
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ Meow Bank is a server's own, so this only works in a server.",
                ephemeral=True,
            )

        guild_id = interaction.guild.id

        if state is None:
            enabled = await meowfee.is_enabled(guild_id)
            bank = await meowfee.bank_balance(guild_id)
            examples = "\n".join(
                f"> Send `{amount:,}` 🪙 → you pay `{amount + meowfee.fee_for(amount):,}` 🪙, "
                f"bank takes `{meowfee.fee_for(amount):,}` 🪙"
                for amount in (1_000, 10_000, 100_000)
            )
            embed = discord.Embed(
                title="🐾 Meow Fee",
                description=(
                    f"The fee is currently **{'on' if enabled else 'off'}** in this "
                    "server.\n\n"
                    f"{meowfee.describe()}\n\n"
                    "It is charged on **sending coins** (`/pay`) and on the takings "
                    "from a successful **robbery** (`/rob`). It is *not* charged on "
                    "moving your own coins between your wallet and your bank, and "
                    "it never touches what you earn.\n\n"
                    f"**At {meowfee.fee_label()}:**\n{examples}"
                ),
                color=0x2ECC71 if enabled else 0x5865F2,
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(
                name="🏦 Meow Bank",
                value=f"Holds **{bank:,}** 🪙 of collected fees.",
                inline=False,
            )
            embed.set_footer(
                text="🐾 /meowfee state:Off to stop charging it — the coins stay in the server either way"
            )
            return await send_card(interaction, embed=embed)

        wanted = meowfee.parse_enabled(state.value)
        if wanted is None:
            return await interaction.response.send_message(
                "❌ Pick **On** or **Off**.", ephemeral=True
            )

        await meowfee.set_enabled(guild_id, wanted)
        bank = await meowfee.bank_balance(guild_id)
        embed = discord.Embed(
            title="🐾 Meow Fee " + ("On" if wanted else "Off"),
            description=(
                (
                    f"The fee is **on** again — {meowfee.describe()}"
                    if wanted
                    else "The fee is **off**. Transfers and robberies now move the "
                    "exact amount, and nothing goes to Meow Bank."
                )
                + f"\n\n> 🏦 **Meow Bank** still holds **{bank:,}** 🪙."
            ),
            color=0x2ECC71 if wanted else 0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"🐾 Changed by {interaction.user.display_name}")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="deposit", description="Deposit coins into your bank.")
    @app_commands.describe(amount="Amount to deposit ('all', '5k', '1m', '1000')")
    async def deposit(self, interaction: discord.Interaction, amount: str):
        is_owner = await check_is_owner(interaction)
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id, is_owner=is_owner)

        if eco["wallet"] <= 0:
            return await interaction.response.send_message("❌ You have no coins in your wallet to deposit.", ephemeral=True)

        amount_clean = amount.strip().lower()
        if amount_clean in ("all", "max"):
            dep = eco["wallet"]
        else:
            dep = parse_amount(amount, eco["wallet"])
            if dep is None or dep <= 0:
                return await interaction.response.send_message("❌ Please enter a valid positive number or `'all'`.", ephemeral=True)
            if dep > eco["wallet"]:
                return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙 in your wallet.", ephemeral=True)

        # Conditional + atomic: the balance is re-checked inside the statement, so
        # two deposits fired at once can't both pass the read above and push the
        # wallet negative.
        def _deposit(cur):
            cur.execute(
                "UPDATE economy SET wallet = wallet - ?, bank = bank + ? WHERE user_id=? AND guild_id=? AND wallet >= ?",
                (dep, dep, interaction.user.id, interaction.guild.id, dep)
            )
            return cur.rowcount

        moved = await asyncio.to_thread(db.run_atomic, _deposit)

        if moved != 1:
            return await interaction.response.send_message(
                "❌ Your wallet changed while that was processing — please try again.", ephemeral=True
            )

        embed = discord.Embed(
            title="🏦 Deposit Successful",
            description=f"Successfully deposited **+{dep:,}** 🪙 into your bank!",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        new_bank = eco["bank"] + dep
        embed.set_footer(text=f"🐾 Bank: {new_bank:,} 🪙 • Unlimited Capacity")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="withdraw", description="Withdraw coins from your bank.")
    @app_commands.describe(amount="Amount to withdraw ('all', '5k', '1m', '1000')")
    async def withdraw(self, interaction: discord.Interaction, amount: str):
        is_owner = await check_is_owner(interaction)
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id, is_owner=is_owner)

        if eco["bank"] <= 0:
            return await interaction.response.send_message("❌ You have no coins in your bank to withdraw.", ephemeral=True)

        amount_clean = amount.strip().lower()
        if amount_clean in ("all", "max"):
            amt = eco["bank"]
        else:
            amt = parse_amount(amount, eco["bank"])
            if amt is None or amt <= 0:
                return await interaction.response.send_message("❌ Please enter a valid positive number or `'all'`.", ephemeral=True)
            if amt > eco["bank"]:
                return await interaction.response.send_message(f"❌ You only have **{eco['bank']:,}** 🪙 in your bank.", ephemeral=True)

        # Conditional + atomic, mirroring /deposit.
        def _withdraw(cur):
            cur.execute(
                "UPDATE economy SET bank = bank - ?, wallet = wallet + ? WHERE user_id=? AND guild_id=? AND bank >= ?",
                (amt, amt, interaction.user.id, interaction.guild.id, amt)
            )
            return cur.rowcount

        moved = await asyncio.to_thread(db.run_atomic, _withdraw)

        if moved != 1:
            return await interaction.response.send_message(
                "❌ Your bank changed while that was processing — please try again.", ephemeral=True
            )

        embed = discord.Embed(
            title="💸 Withdrawal Successful",
            description=f"Successfully withdrew **+{amt:,}** 🪙 from your bank into your wallet!",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        new_wallet = eco["wallet"] + amt
        embed.set_footer(text=f"🐾 Wallet: {new_wallet:,} 🪙 • Unlimited Capacity")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="richlist", description="Top 10 richest members in the server.")
    async def richlist(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT user_id, wallet+bank as net FROM economy WHERE guild_id=? ORDER BY net DESC LIMIT 10",
                (interaction.guild.id,)
            ) as cur:
                rows = await cur.fetchall()

        embed = discord.Embed(title="🏆 Server Rich List", color=0xFFD700, timestamp=discord.utils.utcnow())
        medals = ["👑", "💎", "⭐"]
        # Plain resolved names, not mentions: a mention in a V2 card pings, and
        # guild.get_member is empty without the Members intent (raw "User 123…").
        names = await display_names(interaction.guild, [uid for uid, _ in rows], client=interaction.client)
        desc = []
        for i, ((uid, net), name) in enumerate(zip(rows, names)):
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            desc.append(f"{medal} **{name}** — {net:,} 🪙")
        embed.description = "\n".join(desc) if desc else "No data yet."
        await send_card(interaction, embed=embed)

    eco_admin = app_commands.Group(name="eco-admin", description="Admin & Owner economy management")

    @eco_admin.command(name="add", description="Give coins to a member (Owner has unlimited power, Admins have strict limits).")
    @app_commands.describe(member="Target member", amount="Amount to give")
    @app_commands.checks.has_permissions(administrator=True)
    async def addmoney(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if amount <= 0:
            return await interaction.response.send_message("❌ Amount must be greater than 0.", ephemeral=True)

        is_owner = await check_is_owner(interaction)
        target_is_owner = (interaction.guild and interaction.guild.owner_id == member.id)

        if not is_owner:
            # 1. Admins cannot give money to themselves
            if member.id == interaction.user.id:
                return await interaction.response.send_message(
                    "❌ **Permission Denied:** Admins cannot give coins to themselves! Only the **Server Owner** has that privilege.",
                    ephemeral=True
                )

            # 2. Strict gift limit for Admins (Max 50,000 per command)
            ADMIN_MAX_GIFT = 50_000
            if amount > ADMIN_MAX_GIFT:
                return await interaction.response.send_message(
                    f"❌ **Limit Exceeded:** Admins can only grant up to **{ADMIN_MAX_GIFT:,}** 🪙 at a time.\nOnly the **Server Owner** can grant larger amounts.",
                    ephemeral=True
                )

            # 3. Target balance cap for Admins (cannot pump anyone past 1,000,000)
            target_eco = await get_eco(member.id, interaction.guild.id, is_owner=target_is_owner)
            if (target_eco["wallet"] + target_eco["bank"]) >= 1_000_000:
                return await interaction.response.send_message(
                    f"❌ **Cap Reached:** {member.mention} already has **{target_eco['wallet'] + target_eco['bank']:,}** 🪙.\nAdmins cannot add coins to members who have 1,000,000+ coins. Only the **Server Owner** can bypass this.",
                    ephemeral=True
                )

        await update_wallet(member.id, interaction.guild.id, amount, is_owner=target_is_owner or is_owner)
        embed = discord.Embed(
            description=f"✅ Successfully gave **{amount:,}** 🪙 to {member.mention}.",
            color=0x2ECC71,
            timestamp=discord.utils.utcnow()
        )
        if is_owner:
            embed.set_footer(text="👑 Server Owner Override • Unlimited Power")
        else:
            embed.set_footer(text=f"🛡️ Admin Grant by {interaction.user.display_name}")
        await send_card(interaction, embed=embed)

    @eco_admin.command(name="remove", description="Remove coins from a member (Admin / Owner).")
    @app_commands.describe(member="Target member", amount="Amount to remove")
    @app_commands.checks.has_permissions(administrator=True)
    async def removemoney(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if amount <= 0:
            return await interaction.response.send_message("❌ Amount must be greater than 0.", ephemeral=True)

        is_owner = await check_is_owner(interaction)
        if not is_owner and amount > 100_000:
            return await interaction.response.send_message("❌ Admins can only remove up to **100,000** 🪙 at once. Server Owner has no limit.", ephemeral=True)

        eco = await get_eco(member.id, interaction.guild.id)
        available = max(0, int(eco["wallet"]))
        if available == 0:
            return await interaction.response.send_message(
                f"❌ {member.display_name} has no coins in their wallet to remove.", ephemeral=True
            )
        removed = min(amount, available)
        # Debit exactly the coins that are there. ``update_wallet(-amount)`` clamps
        # at zero, so this confirmation used to claim it removed 10,000 coins from
        # a wallet holding 50 — and two simultaneous removals could both "succeed".
        if not await settle_bet(member.id, interaction.guild.id, removed, 0.0):
            return await interaction.response.send_message(
                "❌ Could not remove coins — that wallet changed. Please try again.", ephemeral=True
            )
        embed = discord.Embed(
            description=f"✅ Removed **{removed:,}** 🪙 from {member.mention}.",
            color=0xFF3333,
            timestamp=discord.utils.utcnow()
        )
        if removed < amount:
            embed.add_field(
                name="Partial removal",
                value=f"They only held **{available:,}** 🪙, so that is all that was removed.",
                inline=False,
            )
        if is_owner:
            embed.set_footer(text="👑 Server Owner Override")
        await send_card(interaction, embed=embed)

    @eco_admin.command(name="set", description="Set a member's exact wallet and bank balance (Owner only).")
    @app_commands.describe(member="Target member", wallet="New wallet balance", bank="New bank balance (optional)")
    async def setmoney(self, interaction: discord.Interaction, member: discord.Member, wallet: int, bank: int = None):
        if not await check_is_owner(interaction):
            return await interaction.response.send_message(
                "❌ **Permission Denied:** Only the **Server Owner** can directly set balances!",
                ephemeral=True
            )

        wallet = max(0, wallet)
        if bank is not None:
            bank = max(0, bank)

        await ensure_eco(member.id, interaction.guild.id)
        async with aiosqlite.connect(DB_PATH) as conn:
            if bank is not None:
                await conn.execute("UPDATE economy SET wallet=?, bank=? WHERE user_id=? AND guild_id=?",
                                   (wallet, bank, member.id, interaction.guild.id))
            else:
                await conn.execute("UPDATE economy SET wallet=? WHERE user_id=? AND guild_id=?",
                                   (wallet, member.id, interaction.guild.id))
            await conn.commit()

        embed = discord.Embed(
            title="👑 Balance Set by Server Owner",
            description=f"Updated balances for {member.mention}:\n👛 **Wallet:** `{wallet:,}` 🪙" + (f"\n🏦 **Bank:** `{bank:,}` 🪙" if bank is not None else ""),
            color=0xF1C40F,
            timestamp=discord.utils.utcnow()
        )
        await send_card(interaction, embed=embed)

    @eco_admin.command(name="reset", description="Reset economy balances back to 0 (Owner only).")
    @app_commands.describe(member="Member to reset", reset_all="Reset all server balances?")
    async def resetmoney(self, interaction: discord.Interaction, member: discord.Member = None, reset_all: bool = False):
        if not await check_is_owner(interaction):
            return await interaction.response.send_message(
                "❌ **Permission Denied:** Only the **Server Owner** can reset balances!",
                ephemeral=True
            )

        async with aiosqlite.connect(DB_PATH) as conn:
            if reset_all:
                await conn.execute("UPDATE economy SET wallet=0, bank=0 WHERE guild_id=?", (interaction.guild.id,))
                await conn.commit()
                embed = discord.Embed(
                    title="🧹 Server Economy Reset",
                    description="✅ The entire server economy has been reset to **0** 🪙 by the **Server Owner**.",
                    color=0xED4245,
                    timestamp=discord.utils.utcnow()
                )
            elif member:
                await conn.execute("UPDATE economy SET wallet=0, bank=0 WHERE user_id=? AND guild_id=?", (member.id, interaction.guild.id))
                await conn.commit()
                embed = discord.Embed(
                    title="🧹 Member Balance Reset",
                    description=f"✅ Successfully reset balance for {member.mention} to **0** 🪙.",
                    color=0xED4245,
                    timestamp=discord.utils.utcnow()
                )
            else:
                return await interaction.response.send_message("❌ Please choose a `member` to reset or set `reset_all: True`.", ephemeral=True)

        await send_card(interaction, embed=embed)

    @app_commands.command(name="shop", description="Browse items in the community shop.")
    async def shop(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        embed = discord.Embed(
            title="🛒 Community Shop",
            description=(
                "Buy anything with `/buy <item>` — the perk starts the moment you pay.\n"
                "The catalog runs in five price tiers, and the tiers are what the "
                "perks are scaled to: higher tiers run longer and hit harder.\n"
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )

        for tier in shop.tiers():
            items = shop.items_in_tier(tier)
            # A price *range*, because the tiers are anchored on round numbers and
            # therefore overlap: tier 2 tops out above where tier 3 starts.
            embed.add_field(
                name=(
                    f"{shop.TIER_NAMES[tier]} • "
                    f"`{items[0].price:,}`-`{items[-1].price:,}` 🪙"
                ),
                value="\n".join(shop.shop_line(item) for item in items),
                inline=False,
            )

        embed.set_footer(text="🐾 Meow Shop • /inventory shows your items and active perks")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="buy", description="Purchase an item from the community shop.")
    @app_commands.describe(item_name="Name of the item to buy")
    async def buy(self, interaction: discord.Interaction, item_name: str):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        # A purchase rate limit: it stops a double-click buying the same item
        # twice and a script hammering the command. Checked before any work, and
        # only *successful* purchases start the clock, so a typo cannot lock a
        # member out of the shop.
        waiting = shop.purchase_cooldown_remaining(interaction.user.id, interaction.guild.id)
        if waiting > 0.05:
            return await interaction.response.send_message(
                f"⏳ One purchase at a time — you can buy again in **{waiting:.1f}s**.",
                ephemeral=True,
            )

        matches = shop.find(item_name)
        if not matches:
            return await interaction.response.send_message(
                f"❌ Nothing in the shop is called **{item_name}**. Use `/shop` for the catalog.",
                ephemeral=True
            )
        if len(matches) > 1:
            # Substring matching used to silently buy the first hit, so "/buy coin"
            # could sell a Coin Charm when the Coin Doubler was meant.
            names = ", ".join(f"`{item.name}`" for item in matches[:8])
            return await interaction.response.send_message(
                f"❓ **{item_name}** matches more than one item: {names}\n"
                "Try a longer name.",
                ephemeral=True
            )
        matched = matches[0]
        price = matched.price

        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)

        if eco["wallet"] < price:
            return await interaction.response.send_message(
                f"❌ You need **{price:,}** 🪙, but you only have **{eco['wallet']:,}** 🪙 in your wallet.",
                ephemeral=True
            )

        now = time.time()
        payout = shop.roll_payout(matched)

        # Pay, record and apply the perk in ONE transaction: the charge, the
        # inventory row, the timed effect (or the instant payout) all land
        # together or not at all, so no purchase can take coins without granting
        # the item, and two simultaneous buyers cannot both be served from
        # wallet that only covers one.
        def _buy(cur):
            if matched.unique:
                cur.execute(
                    "SELECT COUNT(*) FROM inventory WHERE user_id=? AND guild_id=? AND item_name=?",
                    (interaction.user.id, interaction.guild.id, matched.name)
                )
                if cur.fetchone()[0]:
                    return "owned"
            cur.execute(
                "UPDATE economy SET wallet = wallet - ? WHERE user_id = ? AND guild_id = ? AND wallet >= ?",
                (price, interaction.user.id, interaction.guild.id, price)
            )
            if cur.rowcount != 1:
                return "funds"
            cur.execute(
                "INSERT INTO inventory (user_id, guild_id, item_name) VALUES (?, ?, ?)",
                (interaction.user.id, interaction.guild.id, matched.name)
            )
            shop.grant_on(cur, interaction.user.id, interaction.guild.id, matched, now)
            if payout:
                cur.execute(
                    "UPDATE economy SET wallet = wallet + ? WHERE user_id=? AND guild_id=?",
                    (payout, interaction.user.id, interaction.guild.id)
                )
            return "ok"

        outcome = await asyncio.to_thread(db.run_atomic, _buy)

        if outcome == "owned":
            return await interaction.response.send_message(
                f"🎒 You already own **{matched.name}** — it is a permanent badge, "
                "so a second one would do nothing.",
                ephemeral=True
            )
        if outcome != "ok":
            return await interaction.response.send_message(
                "❌ Your wallet changed while that was processing — please try again.", ephemeral=True
            )

        # The leveling listener caches multipliers for a few seconds; a fresh
        # purchase must not sit behind a stale cache.
        shop.invalidate(interaction.user.id, interaction.guild.id)
        shop.note_purchase(interaction.user.id, interaction.guild.id, now)

        if payout:
            perk = f"\n> 🎟️ **Payout:** `+{payout:,}` 🪙"
        elif matched.effect.badge:
            perk = f"\n> ♾️ **Permanent:** {shop.describe_effect(matched)}"
        else:
            # What is *running*, not what the item's label says. Buying a weaker
            # item while a stronger one is up converts the time it adds, so the
            # label alone would over-state what the member just got.
            running = ""
            try:
                running = await shop.active_summary(interaction.user.id, interaction.guild.id, now)
            except Exception as exc:  # a read failure must not lose the receipt
                print(f"[SHOP] Could not read active perks after a purchase: {exc}")
            perk = (
                f"\n> ⚡ **Active now:**\n{running}"
                if running
                else f"\n> ⚡ **Active now:** {shop.describe_effect(matched)}"
            )

        embed = discord.Embed(
            title="🛍️ Purchase Successful!",
            description=(
                f"You purchased **{matched.name}** for **{price:,}** 🪙!{perk}\n\n"
                f"🎒 Check your items and perks anytime using `/inventory`."
            ),
            color=0x23a55a,
            timestamp=discord.utils.utcnow()
        )
        await send_card(interaction, embed=embed)

    @app_commands.command(name="inventory", description="View your purchased items and badges.")
    @app_commands.describe(member="Member whose inventory to view")
    async def inventory(self, interaction: discord.Interaction, member: discord.Member = None):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        target = member or interaction.user
        items = []
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute("SELECT item_name FROM inventory WHERE user_id=? AND guild_id=?",
                                   (target.id, interaction.guild.id)) as cur:
                async for row in cur:
                    items.append(row[0])

        embed = discord.Embed(
            title=f"🎒 {target.display_name}'s Inventory",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        if items:
            # Count item quantities
            from collections import Counter
            counts = Counter(items)
            desc_lines = [f"• **{name}** ×`{count}`" for name, count in counts.items()]
            embed.description = "\n".join(desc_lines)
        else:
            embed.description = "*Inventory is empty! Visit `/shop` to purchase items.*"

        # What the items are actually doing right now. Until this existed, buying
        # a "Rob Shield" or an "XP Booster" put a line in the list above and
        # changed nothing at all.
        now = time.time()
        effects = await shop.active_effects(target.id, interaction.guild.id, now)
        owned = await shop.owned_items(target.id, interaction.guild.id)

        if effects:
            embed.add_field(
                name="⚡ Active Perks",
                value="\n".join(
                    shop.describe_active(key, value, expires, now)
                    for key, (value, expires) in sorted(effects.items())
                ),
                inline=False,
            )
        badges = [item for item in owned if item.effect.badge]
        if badges:
            embed.add_field(
                name="👑 Permanent Badges",
                value="\n".join(shop.badge_line(item) for item in badges),
                inline=False,
            )
        if effects or badges:
            totals = await shop.multipliers(target.id, interaction.guild.id, now)
            embed.add_field(
                name="📈 Right Now",
                value=(
                    f"• Activity earnings: **×{totals.coins:g}** coins, **×{totals.xp:g}** XP\n"
                    f"• Rob luck: **+{totals.luck}**{'' if not totals.shield else ' • 🛡️ **shielded**'}"
                ),
                inline=False,
            )

        embed.set_footer(text="🐾 Meow Economy • /shop to browse")
        await send_card(interaction, embed=embed)

    # -------------------------------------------------------------------------
    # Admin Economy Commands
    # -------------------------------------------------------------------------
    #
    # These handlers must not share a name with the /eco-admin group's methods
    # (addmoney / removemoney / setmoney). Two definitions with the same name in
    # one class body silently re-bind the attribute, so `/eco-admin add`, `remove`
    # and `set` were registered but pointing at the wrong functions — and because
    # only the last definition survives, the group's own implementations were
    # unreachable dead code. Hence the _cmd suffix; the command name the user
    # sees comes from the decorator, not the method name.

    @app_commands.command(name="addmoney", description="Add money to a member's wallet (Admin only).")
    @app_commands.describe(member="The member to give money to", amount="Amount to add")
    # A real check rather than a hand-rolled string compare: discord.py refuses
    # the member before the callback runs, and the handler answers with the same
    # "permission denied" card it uses everywhere else. `utils/permissions.py`
    # stamps the picker visibility, and a test asserts the two agree.
    @app_commands.checks.has_permissions(administrator=True)
    async def addmoney_cmd(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if amount <= 0:
            return await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)

        from utils.admin_limits import needs_approval, ADMIN_ADDMONEY_LIMIT, request_approval

        if needs_approval(amount, ADMIN_ADDMONEY_LIMIT):
            # Request owner approval
            async def execute():
                await ensure_eco(member.id, interaction.guild.id)
                await update_wallet(member.id, interaction.guild.id, amount)
            return await request_approval(
                interaction, member, "💰 coins", amount, execute,
                limit=ADMIN_ADDMONEY_LIMIT,
            )

        await ensure_eco(member.id, interaction.guild.id)
        await update_wallet(member.id, interaction.guild.id, amount)
        
        embed = discord.Embed(
            title="💰 Money Added",
            description=f"Added **{amount:,}** 🪙 to {member.mention}'s wallet.",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Admin action by {interaction.user.display_name}")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="removemoney", description="Remove money from a member's wallet (Admin only).")
    @app_commands.describe(member="The member to remove money from", amount="Amount to remove")
    @app_commands.checks.has_permissions(administrator=True)
    async def removemoney_cmd(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if amount <= 0:
            return await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)

        from utils.admin_limits import needs_approval, ADMIN_ADDMONEY_LIMIT, request_approval

        if needs_approval(amount, ADMIN_ADDMONEY_LIMIT):
            async def execute():
                await ensure_eco(member.id, interaction.guild.id)
                await update_wallet(member.id, interaction.guild.id, -amount)
            return await request_approval(
                interaction, member, "💰 coins (removal)", amount, execute,
                limit=ADMIN_ADDMONEY_LIMIT,
            )

        await ensure_eco(member.id, interaction.guild.id)
        await update_wallet(member.id, interaction.guild.id, -amount)
        
        embed = discord.Embed(
            title="💸 Money Removed",
            description=f"Removed **{amount:,}** 🪙 from {member.mention}'s wallet.",
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Admin action by {interaction.user.display_name}")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="setmoney", description="Set a member's wallet to a specific amount (Admin only).")
    @app_commands.describe(member="The member to set money for", amount="New wallet amount")
    @app_commands.checks.has_permissions(administrator=True)
    async def setmoney_cmd(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if amount < 0:
            return await interaction.response.send_message("❌ Amount cannot be negative.", ephemeral=True)

        from utils.admin_limits import needs_approval, ADMIN_SETMONEY_LIMIT, request_approval

        if needs_approval(amount, ADMIN_SETMONEY_LIMIT):
            async def execute():
                await ensure_eco(member.id, interaction.guild.id)
                async with aiosqlite.connect(DB_PATH) as conn:
                    await conn.execute(
                        "UPDATE economy SET wallet = ? WHERE user_id=? AND guild_id=?",
                        (amount, member.id, interaction.guild.id)
                    )
                    await conn.commit()
            return await request_approval(
                interaction, member, "💰 coins (set)", amount, execute,
                limit=ADMIN_SETMONEY_LIMIT,
            )

        await ensure_eco(member.id, interaction.guild.id)
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE economy SET wallet = ? WHERE user_id=? AND guild_id=?",
                (amount, member.id, interaction.guild.id)
            )
            await conn.commit()
        
        embed = discord.Embed(
            title="💰 Wallet Set",
            description=f"Set {member.mention}'s wallet to **{amount:,}** 🪙.",
            color=0xF1C40F,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Admin action by {interaction.user.display_name}")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="addbank", description="Add money to a member's bank (Admin only).")
    @app_commands.describe(member="The member to give bank money to", amount="Amount to add")
    @app_commands.checks.has_permissions(administrator=True)
    async def addbank(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if amount <= 0:
            return await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
        
        await ensure_eco(member.id, interaction.guild.id)
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE economy SET bank = bank + ? WHERE user_id=? AND guild_id=?",
                (amount, member.id, interaction.guild.id)
            )
            await conn.commit()
        
        embed = discord.Embed(
            title="🏦 Bank Money Added",
            description=f"Added **{amount:,}** 🪙 to {member.mention}'s bank.",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Admin action by {interaction.user.display_name}")
        await send_card(interaction, embed=embed)

    # -------------------------------------------------------------------------
    # Bank Loan System — the credit line grows with your level, and each plan
    # trades the fee against the size of the loan and the length of the term.
    # -------------------------------------------------------------------------

    LOAN_PLAN_CHOICES = [
        app_commands.Choice(
            name=f"{p.emoji} {p.name} • {int(p.rate * 100)}% • {p.term_days}d",
            value=p.id,
        )
        for p in loans.PLANS
    ]

    def _loan_center_embed(self, interaction, level: int, row, now: float) -> discord.Embed:
        """The `/loan` card: credit line, plans, and any active debt."""
        rating_name, rating_emoji = loans.rating(level)
        base = loans.credit_line(level)
        nxt = loans.next_credit_line(level)

        lines: list[str] = []
        if row:
            borrowed, debt, due_ts, stored_plan, status = row
            selected = loans.plan(stored_plan)
            secs = loans.secs_left(due_ts, now)
            if status == "overdue" or secs < 0:
                state = "🔴 **Overdue** — the penalty is already on the balance."
            elif secs < 86400:
                state = f"🟠 Due in ~**{max(1, int(secs // 3600))}h** — don't get caught out."
            else:
                state = f"🟢 In good standing • **{int(secs // 86400)}d** left"
            lines.append(
                f"### 💳 Active loan — {selected.emoji} {selected.name}\n"
                f"> 🪙 **Borrowed:** {borrowed:,}\n"
                f"> 📉 **Outstanding:** **{debt:,}** 🪙 (fee included)\n"
                f"> 📅 **Due:** <t:{int(due_ts)}:R>\n"
                f"> {state}\n\n"
                f"Clear it with `/repay` — partial payments are fine."
            )
        else:
            lines.append("### 🎉 No active loans\nYou are debt-free. Borrow whenever you like.")

        credit_lines = [
            f"### 📈 Credit line — {rating_emoji} {rating_name}",
            f"> 🏅 **Level {level}** keeps **{base:,}** 🪙 of credit open for you",
        ]
        credit_lines.append(
            f"> ⏭️ Level up for **{nxt:,}** 🪙 at level {level + 1}"
            if nxt else "> 🏛️ You are at the maximum credit line"
        )
        credit_lines.append("")
        credit_lines.extend(loans.format_plan_line(p, level) for p in loans.PLANS)
        credit_lines.append("")
        credit_lines.append("Borrow with `/loan amount:<coins> plan:<plan>`.")
        lines.append("\n".join(credit_lines))

        embed = discord.Embed(
            title="🏦 Meow National Bank — Loan Center",
            description="\n\n".join(lines),
            color=0x3498DB,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="🐾 Missed loans add a 25% penalty and block new borrowing.")
        return embed

    @app_commands.command(name="loan", description="Borrow coins from Meow Bank — your credit line grows with your level.")
    @app_commands.describe(
        amount="Coins to borrow (leave blank to see your credit line and active loan)",
        plan="Which plan to borrow on",
    )
    @app_commands.choices(plan=LOAN_PLAN_CHOICES)
    async def loan(
        self,
        interaction: discord.Interaction,
        amount: int = None,
        plan: app_commands.Choice[str] = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)

        user_id = interaction.user.id
        guild_id = interaction.guild.id
        plan_id = plan.value if plan else loans.DEFAULT_PLAN
        selected = loans.plan(plan_id)
        now = time.time()

        await ensure_eco(user_id, guild_id)
        level = await member_level(user_id, guild_id)
        limit = loans.credit_limit(plan_id, level)

        if amount is None:
            # Charge any overdue penalty first, so the card shows the real debt.
            await asyncio.to_thread(
                db.run_atomic,
                lambda cur: _charge_late_fee(cur, user_id, guild_id, now),
            )
            async with aiosqlite.connect(DB_PATH) as conn:
                async with conn.execute(
                    "SELECT amount, repay_amount, due_ts, plan, status FROM loans WHERE user_id=? AND guild_id=?",
                    (user_id, guild_id)
                ) as cur:
                    active_loan = await cur.fetchone()

            return await send_card(
                interaction, embed=self._loan_center_embed(interaction, level, active_loan, now)
            )

        # Taking out a new loan
        if amount < loans.MIN_LOAN:
            return await interaction.response.send_message(
                f"❌ The smallest loan the bank writes is **{loans.MIN_LOAN:,}** 🪙.", ephemeral=True
            )
        if amount > limit:
            return await interaction.response.send_message(
                f"❌ Your **{selected.name}** limit is **{limit:,}** 🪙 at **Level {level}**.\n"
                "> Level up to raise your credit line, or choose a plan with a smaller limit.\n"
                "> Run `/loan` with no amount to see every plan.",
                ephemeral=True
            )

        fee = loans.interest(amount, plan_id)
        owed = amount + fee
        due_time = loans.due_at(now, plan_id)

        # Insert and credit in one transaction. Two simultaneous borrows used to
        # pass the same "no active loan" read and then both hit the wallet, and a
        # duplicate insert raised an unhandled IntegrityError. BEGIN IMMEDIATE
        # plus the PRIMARY KEY now makes the second caller see the first one.
        def _borrow(cur):
            _charge_late_fee(cur, user_id, guild_id, now)
            cur.execute(
                "SELECT 1 FROM loans WHERE user_id=? AND guild_id=?", (user_id, guild_id)
            )
            if cur.fetchone():
                return "exists"
            cur.execute(
                """INSERT INTO loans
                       (user_id, guild_id, amount, repay_amount, due_ts, plan, status, taken_ts, late_fee_applied)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, 0)""",
                (user_id, guild_id, amount, owed, due_time, selected.id, now),
            )
            cur.execute(
                "UPDATE economy SET wallet = wallet + ? WHERE user_id=? AND guild_id=?",
                (amount, user_id, guild_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("economy row vanished during loan disbursement")
            return "ok"

        outcome = await asyncio.to_thread(db.run_atomic, _borrow)
        if outcome == "exists":
            return await interaction.response.send_message(
                "❌ You already have an active loan! Clear it with `/repay` before borrowing again.",
                ephemeral=True
            )

        embed = discord.Embed(
            title="🤝 Loan Approved!",
            description=(
                f"### {selected.emoji} Funds Disbursed: +**{amount:,}** 🪙\n\n"
                f"> 💵 **Credited to:** Wallet\n"
                f"> 📈 **Total to Repay:** **{owed:,}** 🪙 ({int(selected.rate * 100)}% fee)\n"
                f"> 📅 **Due in {selected.term_days} day(s):** <t:{int(due_time)}:R>\n\n"
                f"Use `/repay` whenever you're ready to pay it back!"
            ),
            color=0x2ECC71,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"🐾 Meow Bank • {selected.name} • pay on time or eat a 25% penalty")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="repay", description="Repay your bank loan — in full or a bit at a time.")
    @app_commands.describe(
        amount="Coins to repay (leave blank to clear the whole debt)",
        from_bank="Pay straight out of your bank instead of your wallet",
    )
    async def repay(
        self,
        interaction: discord.Interaction,
        amount: int = None,
        from_bank: bool = False,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)

        user_id = interaction.user.id
        guild_id = interaction.guild.id
        now = time.time()
        await ensure_eco(user_id, guild_id)

        source = "bank" if from_bank else "wallet"

        def _repay(cur):
            fee = _charge_late_fee(cur, user_id, guild_id, now)
            cur.execute(
                "SELECT amount, repay_amount, due_ts, plan FROM loans WHERE user_id=? AND guild_id=?",
                (user_id, guild_id),
            )
            row = cur.fetchone()
            if not row:
                return {"status": "none", "fee": fee}
            borrowed, debt, due_ts, stored_plan = row
            pay = debt if amount is None else min(int(amount), debt)
            if pay <= 0:
                return {"status": "invalid", "debt": debt, "fee": fee}

            # Conditional debit. A plain `wallet - ?` clamps at zero, so two
            # simultaneous repayments could both "pay" coins that only existed
            # once and the second one would clear the loan for free.
            cur.execute(
                f"UPDATE economy SET {source} = {source} - ? "
                f"WHERE user_id=? AND guild_id=? AND {source} >= ?",
                (pay, user_id, guild_id, pay),
            )
            if cur.rowcount != 1:
                cur.execute(
                    "SELECT wallet, bank FROM economy WHERE user_id=? AND guild_id=?",
                    (user_id, guild_id),
                )
                funds = cur.fetchone() or (0, 0)
                return {
                    "status": "insufficient",
                    "debt": debt,
                    "pay": pay,
                    "fee": fee,
                    "source": source,
                    "wallet": funds[0],
                    "bank": funds[1],
                }

            remaining = debt - pay
            if remaining <= 0:
                cur.execute("DELETE FROM loans WHERE user_id=? AND guild_id=?", (user_id, guild_id))
            else:
                cur.execute(
                    "UPDATE loans SET repay_amount=?, status='active' WHERE user_id=? AND guild_id=?",
                    (remaining, user_id, guild_id),
                )
            return {
                "status": "paid",
                "pay": pay,
                "remaining": remaining,
                "borrowed": borrowed,
                "plan": loans.plan(stored_plan),
                "fee": fee,
                "source": source,
            }

        result = await asyncio.to_thread(db.run_atomic, _repay)
        outcome = result.get("status")

        if outcome == "none":
            return await interaction.response.send_message(
                "🎉 You have no outstanding loans to repay!", ephemeral=True
            )
        if outcome == "invalid":
            return await interaction.response.send_message(
                f"❌ Repay amount must be greater than 0. You owe **{result['debt']:,}** 🪙.",
                ephemeral=True,
            )
        if outcome == "insufficient":
            have = result["bank"] if result["source"] == "bank" else result["wallet"]
            hint = (
                "Try `/repay from_bank:True` to use your bank, or `/withdraw` first."
                if result["source"] == "wallet"
                else "Deposit some coins with `/deposit`, or repay from your wallet instead."
            )
            return await interaction.response.send_message(
                f"❌ Insufficient funds in your **{result['source']}**! You need **{result['pay']:,}** 🪙 "
                f"but only have **{have:,}** 🪙.\n> {hint}",
                ephemeral=True,
            )

        pay_amount = result["pay"]
        new_debt = result["remaining"]
        selected = result["plan"]
        fee_line = ""
        if result["fee"]:
            fee_line = f"\n> ⚠️ A late fee of **{result['fee']:,}** 🪙 was added for missing the due date."

        if new_debt <= 0:
            embed = discord.Embed(
                title="🎉 Loan Fully Repaid!",
                description=(
                    f"### Congratulations {interaction.user.display_name}!\n\n"
                    f"You paid **{pay_amount:,}** 🪙 and **fully cleared your {selected.name}**!\n"
                    f"> 🌟 **Your credit score is pristine.**\n"
                    f"> 🏦 You are eligible to borrow again anytime with `/loan`."
                    + fee_line
                ),
                color=0x57F287,
                timestamp=discord.utils.utcnow()
            )
        else:
            embed = discord.Embed(
                title="💳 Partial Loan Payment Made",
                description=(
                    f"### Payment Accepted: -**{pay_amount:,}** 🪙\n\n"
                    f"> 📉 **Remaining Debt:** **{new_debt:,}** 🪙\n"
                    f"> 📄 **Plan:** {selected.emoji} {selected.name}\n"
                    f"Pay the rest whenever you're ready with `/repay`!"
                    + fee_line
                ),
                color=0xF1C40F,
                timestamp=discord.utils.utcnow()
            )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="🐾 Meow Banking Services")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="beg", description="Beg the streets for spare coins. Dignity optional.")
    @app_commands.checks.cooldown(1, 120.0, key=lambda i: (i.guild_id, i.user.id))
    async def beg(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Begging works best in a server.", ephemeral=True)

        await ensure_eco(interaction.user.id, interaction.guild.id)

        roll = random.random()
        if roll < 0.06:
            # The whale: rare enough to feel great, small enough to not matter.
            payout = 100
            line = "A passing whale felt sorry for you and dropped **100** 🪙 without looking back. 🐋"
        elif roll < 0.22:
            payout = random.randint(1, 5)
            line = random.choice([
                "A stranger flicked a couple of coins at you without making eye contact.",
                "Someone paid you to go away. It worked.",
                "You found it in the couch cushions. Not yours, but nobody's counting.",
            ])
        elif roll < 0.34:
            payout = 0
            line = random.choice([
                "Everyone walked past. One person filmed you. 📲",
                "A kid gave you a button. It is not worth anything.",
                "You got ignored so hard the pigeons felt bad for you.",
                "Someone said 'get a job' and closed the door. 🚪",
            ])
        else:
            payout = random.randint(5, 40)
            line = random.choice([
                "You held up an honest sign and the crowd delivered.",
                "Three people, three coins, zero questions asked.",
                "Somebody actually respects the hustle. Respect.",
                "You did a little dance. It paid off. 🕺",
            ])

        if payout:
            await update_wallet(interaction.user.id, interaction.guild.id, payout)

        embed = discord.Embed(
            title="🥺 Begging Results",
            description=(
                f"### {interaction.user.display_name} went begging\n\n"
                f"> {line}\n"
                f"> 🪙 **Earned:** **{payout:,}** coins"
            ),
            color=0x95A5A6,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="🐾 Beg cooldown: 2 minutes • /work pays better, you know")
        await send_card(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
