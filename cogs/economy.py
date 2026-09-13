import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import asyncio
import random
import datetime
from utils import db
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

        if random.random() < ROB_SUCCESS_CHANCE:
            # Debit the victim conditionally inside one transaction, then credit the
            # robber — otherwise the pair could mint coins (or take the victim's
            # wallet below zero) if both wallets moved in between the reads above.
            def _rob(cur):
                cur.execute(
                    "UPDATE economy SET wallet = wallet - ? WHERE user_id=? AND guild_id=? AND wallet >= ?",
                    (take, member.id, interaction.guild.id, take)
                )
                ok = cur.rowcount == 1
                if ok:
                    cur.execute(
                        "UPDATE economy SET wallet = wallet + ? WHERE user_id=? AND guild_id=?",
                        (take, interaction.user.id, interaction.guild.id)
                    )
                return ok

            robbed_ok = await asyncio.to_thread(db.run_atomic, _rob)
            if not robbed_ok:
                embed = discord.Embed(title="🚔 Caught!", description=f"{member.mention}'s wallet changed mid-robbery — you got nothing!", color=0xFF3333)
                return await send_card(interaction, embed=embed)
            embed = discord.Embed(
                title="🦹 Successful Robbery!",
                description=(
                    f"You robbed **{take:,}** 🪙 from {member.mention}!\n\n"
                    f"> 🎯 **Take:** `{take:,}` 🪙\n"
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

        embed.set_footer(text=f"🐾 Rob cooldown: {ROB_COOLDOWN_SECONDS // 60} minutes • Success chance: {int(ROB_SUCCESS_CHANCE * 100)}%")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="pay", description="Transfer coins to another member.")
    @app_commands.describe(member="Who to pay", amount="Amount to send")
    async def pay(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, None]):
        if member.id == interaction.user.id:
            return await interaction.response.send_message("You can't pay yourself!", ephemeral=True)
        if member.bot:
            return await interaction.response.send_message("You can't pay a bot!", ephemeral=True)

        is_sender_owner = await check_is_owner(interaction)
        is_target_owner = (interaction.guild and interaction.guild.owner_id == member.id)

        await ensure_eco(interaction.user.id, interaction.guild.id)
        await ensure_eco(member.id, interaction.guild.id)
        sender_eco = await get_eco(interaction.user.id, interaction.guild.id, is_owner=is_sender_owner)

        if sender_eco["wallet"] < amount:
            return await interaction.response.send_message(f"❌ You only have **{sender_eco['wallet']:,}** 🪙 in your wallet.", ephemeral=True)

        # Debit and credit inside one transaction, and only if the balance still
        # covers it. Two parallel /pay calls used to both pass the read above and
        # duplicate coins.
        def _pay(cur):
            cur.execute(
                "UPDATE economy SET wallet = wallet - ? WHERE user_id=? AND guild_id=? AND wallet >= ?",
                (amount, interaction.user.id, interaction.guild.id, amount)
            )
            moved = cur.rowcount
            if moved == 1:
                cur.execute(
                    "UPDATE economy SET wallet = wallet + ? WHERE user_id=? AND guild_id=?",
                    (amount, member.id, interaction.guild.id)
                )
            return moved

        paid = await asyncio.to_thread(db.run_atomic, _pay)

        if paid != 1:
            return await interaction.response.send_message(
                "❌ Your wallet changed while that was processing — please try again.", ephemeral=True
            )

        embed = discord.Embed(description=f"✅ Sent **{amount:,}** 🪙 to {member.mention}!", color=0x2ECC71, timestamp=discord.utils.utcnow())
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

    DEFAULT_SHOP_ITEMS = [
        {"name": "🍀 Lucky Clover", "price": 500, "desc": "A lucky four-leaf clover for good fortune."},
        {"name": "🛡️ Rob Shield", "price": 1500, "desc": "Protects your wallet from robbers."},
        {"name": "⚡ XP Booster", "price": 3000, "desc": "Supercharge your rank progression."},
        {"name": "💎 VIP Pass", "price": 10000, "desc": "Exclusive VIP badge on your profile."},
        {"name": "👑 Royal Crown", "price": 50000, "desc": "The ultimate prestige luxury crown."}
    ]

    @app_commands.command(name="shop", description="Browse items in the community shop.")
    async def shop(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        embed = discord.Embed(
            title="🛒 Community Shop",
            description="Use `/buy <item_name>` to purchase any of the items below:\n",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )

        for item in self.DEFAULT_SHOP_ITEMS:
            embed.add_field(
                name=f"{item['name']} — `{item['price']:,}` 🪙",
                value=item["desc"],
                inline=False
            )

        embed.set_footer(text="🐾 Meow Shop • Use /inventory to view your items")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="buy", description="Purchase an item from the community shop.")
    @app_commands.describe(item_name="Name of the item to buy")
    async def buy(self, interaction: discord.Interaction, item_name: str):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        # Match item
        matched = None
        for item in self.DEFAULT_SHOP_ITEMS:
            if item_name.lower() in item["name"].lower():
                matched = item
                break

        if not matched:
            valid_names = ", ".join([f"`{i['name']}`" for i in self.DEFAULT_SHOP_ITEMS])
            return await interaction.response.send_message(f"❌ Item not found! Available items: {valid_names}", ephemeral=True)

        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)

        if eco["wallet"] < matched["price"]:
            return await interaction.response.send_message(
                f"❌ You need **{matched['price']:,}** 🪙, but you only have **{eco['wallet']:,}** 🪙 in your wallet.",
                ephemeral=True
            )

        # Charge only if the wallet still covers the price, and only insert the
        # item when the charge actually happened (no free purchases under load).
        def _buy(cur):
            cur.execute(
                "UPDATE economy SET wallet = wallet - ? WHERE user_id = ? AND guild_id = ? AND wallet >= ?",
                (matched["price"], interaction.user.id, interaction.guild.id, matched["price"])
            )
            charged = cur.rowcount
            if charged == 1:
                cur.execute("INSERT INTO inventory (user_id, guild_id, item_name) VALUES (?, ?, ?)",
                            (interaction.user.id, interaction.guild.id, matched["name"]))
            return charged

        bought = await asyncio.to_thread(db.run_atomic, _buy)

        if bought != 1:
            return await interaction.response.send_message(
                "❌ Your wallet changed while that was processing — please try again.", ephemeral=True
            )

        embed = discord.Embed(
            title="🛍️ Purchase Successful!",
            description=(
                f"You purchased **{matched['name']}** for **{matched['price']:,}** 🪙!\n\n"
                f"🎒 Check your items anytime using `/inventory`."
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

        embed.set_footer(text="🐾 Meow Economy • /shop to browse")
        await send_card(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await setup_tables()
    await bot.add_cog(Economy(bot))
