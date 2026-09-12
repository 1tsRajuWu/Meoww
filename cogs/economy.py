import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import random
import datetime
from utils import db

DB_PATH = db.DB_PATH

# Unlimited economy: players can earn and hold as much money as they want!
_bot_instance: commands.Bot | None = None

async def setup_tables():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""CREATE TABLE IF NOT EXISTS economy (
            user_id INTEGER,
            guild_id INTEGER,
            wallet INTEGER DEFAULT 0,
            bank INTEGER DEFAULT 0,
            last_daily TEXT,
            last_weekly TEXT,
            last_work TEXT,
            PRIMARY KEY (user_id, guild_id)
        )""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            guild_id INTEGER,
            item_name TEXT
        )""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS shop (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            item_name TEXT,
            price INTEGER,
            role_id INTEGER
        )""")
        await conn.commit()

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

async def set_field(user_id: int, guild_id: int, field: str, value):
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
        await interaction.response.send_message(
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
                return await interaction.response.send_message(embed=embed, ephemeral=True)

        bonus = random.randint(450, 600)
        await update_wallet(interaction.user.id, interaction.guild.id, bonus)
        await set_field(interaction.user.id, interaction.guild.id, "last_daily", now.isoformat())
        embed = discord.Embed(
            title="📅 Daily Reward Claimed!",
            description=f"You received **+{bonus:,}** 🪙 coins in your wallet!",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Come back in 24 hours for your next reward!")
        await interaction.response.send_message(embed=embed)

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
                return await interaction.response.send_message(embed=embed, ephemeral=True)

        bonus = random.randint(1800, 2500)
        await update_wallet(interaction.user.id, interaction.guild.id, bonus)
        await set_field(interaction.user.id, interaction.guild.id, "last_weekly", now.isoformat())
        embed = discord.Embed(
            title="📆 Weekly Reward Claimed!",
            description=f"You received **+{bonus:,}** 🪙 coins in your wallet!",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Come back in 7 days for your next reward!")
        await interaction.response.send_message(embed=embed)

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
                return await interaction.response.send_message(embed=embed, ephemeral=True)

        job, min_pay, max_pay = random.choice(WORK_JOBS)
        earned = random.randint(min_pay, max_pay)
        await update_wallet(interaction.user.id, interaction.guild.id, earned)
        await set_field(interaction.user.id, interaction.guild.id, "last_work", now.isoformat())
        embed = discord.Embed(
            title="💼 Shift Completed!",
            description=f"You worked as a **{job}** and earned **+{earned:,}** 🪙!",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Shift cooldown: 1 hour")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="rob", description="Attempt to rob another member's wallet.")
    @app_commands.describe(member="Who to rob")
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
            return await interaction.response.send_message(embed=embed)
        if robber["wallet"] < 200:
            embed = discord.Embed(description="❌ You need at least **200** 🪙 in your wallet to attempt a robbery (bail money).", color=0xFF3333)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        success = random.random() < 0.45  # 45% success rate
        if success:
            stolen = random.randint(int(victim["wallet"] * 0.1), int(victim["wallet"] * 0.4))
            stolen = min(stolen, 50_000)
            stolen = max(stolen, 10)
            await update_wallet(interaction.user.id, interaction.guild.id, stolen)
            await update_wallet(member.id, interaction.guild.id, -stolen)
            embed = discord.Embed(title="🦹 Successful Robbery!", description=f"You robbed **{stolen:,}** 🪙 from {member.mention}!", color=0x2ECC71)
        else:
            fine = random.randint(100, 300)
            fine = min(fine, robber["wallet"])
            await update_wallet(interaction.user.id, interaction.guild.id, -fine)
            await update_wallet(member.id, interaction.guild.id, fine)
            embed = discord.Embed(title="🚔 Caught!", description=f"You got caught robbing {member.mention} and paid a **{fine:,}** 🪙 fine!", color=0xFF3333)
        await interaction.response.send_message(embed=embed)

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
        target_eco = await get_eco(member.id, interaction.guild.id, is_owner=is_target_owner)

        if sender_eco["wallet"] < amount:
            return await interaction.response.send_message(f"❌ You only have **{sender_eco['wallet']:,}** 🪙 in your wallet.", ephemeral=True)

        await update_wallet(interaction.user.id, interaction.guild.id, -amount, is_owner=is_sender_owner)
        await update_wallet(member.id, interaction.guild.id, amount, is_owner=is_target_owner)
        embed = discord.Embed(description=f"✅ Sent **{amount:,}** 🪙 to {member.mention}!", color=0x2ECC71, timestamp=discord.utils.utcnow())
        await interaction.response.send_message(embed=embed)

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

        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE economy SET wallet = wallet - ?, bank = bank + ? WHERE user_id=? AND guild_id=?",
                (dep, dep, interaction.user.id, interaction.guild.id)
            )
            await conn.commit()

        embed = discord.Embed(
            title="🏦 Deposit Successful",
            description=f"Successfully deposited **+{dep:,}** 🪙 into your bank!",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        new_bank = eco["bank"] + dep
        embed.set_footer(text=f"🐾 Bank: {new_bank:,} 🪙 • Unlimited Capacity")
        await interaction.response.send_message(embed=embed)

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

        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE economy SET bank = bank - ?, wallet = wallet + ? WHERE user_id=? AND guild_id=?",
                (amt, amt, interaction.user.id, interaction.guild.id)
            )
            await conn.commit()

        embed = discord.Embed(
            title="💸 Withdrawal Successful",
            description=f"Successfully withdrew **+{amt:,}** 🪙 from your bank into your wallet!",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        new_wallet = eco["wallet"] + amt
        embed.set_footer(text=f"🐾 Wallet: {new_wallet:,} 🪙 • Unlimited Capacity")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="richlist", description="Top 10 richest members in the server.")
    async def richlist(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await sanitize_economy_balances(self.bot)
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT user_id, wallet+bank as net FROM economy WHERE guild_id=? ORDER BY net DESC LIMIT 10",
                (interaction.guild.id,)
            ) as cur:
                rows = await cur.fetchall()

        embed = discord.Embed(title="🏆 Server Rich List", color=0xFFD700, timestamp=discord.utils.utcnow())
        medals = ["🥇", "🥈", "🥉"]
        desc = []
        for i, (uid, net) in enumerate(rows):
            member = interaction.guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            desc.append(f"{medal} **{name}** — {net:,} 🪙")
        embed.description = "\n".join(desc) if desc else "No data yet."
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="addmoney", description="Give coins to a member (Owner has unlimited power, Admins have strict limits).")
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
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="removemoney", description="Remove coins from a member (Admin / Owner).")
    @app_commands.describe(member="Target member", amount="Amount to remove")
    @app_commands.checks.has_permissions(administrator=True)
    async def removemoney(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if amount <= 0:
            return await interaction.response.send_message("❌ Amount must be greater than 0.", ephemeral=True)

        is_owner = await check_is_owner(interaction)
        if not is_owner and amount > 100_000:
            return await interaction.response.send_message("❌ Admins can only remove up to **100,000** 🪙 at once. Server Owner has no limit.", ephemeral=True)

        await update_wallet(member.id, interaction.guild.id, -amount)
        embed = discord.Embed(
            description=f"✅ Removed **{amount:,}** 🪙 from {member.mention}.",
            color=0xFF3333,
            timestamp=discord.utils.utcnow()
        )
        if is_owner:
            embed.set_footer(text="👑 Server Owner Override")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="setmoney", description="Set a member's exact wallet and bank balance (Owner only).")
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
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="resetmoney", description="Reset economy balances back to 0 (Owner only).")
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

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await setup_tables()
    await bot.add_cog(Economy(bot))
