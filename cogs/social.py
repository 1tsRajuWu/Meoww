import discord
from discord import app_commands
from discord.ext import commands
import random
import asyncio
import aiosqlite
import datetime
from utils import db
from cogs.economy import get_eco, update_wallet, ensure_eco, check_is_owner
from utils.style import send_card

DB_PATH = db.DB_PATH

# -------------------------------------------------------------
# DATABASE SETUP
# -------------------------------------------------------------
async def setup_social_tables():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""CREATE TABLE IF NOT EXISTS user_animals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            animal_name TEXT,
            emoji TEXT,
            tier TEXT,
            count INTEGER DEFAULT 1,
            UNIQUE(guild_id, user_id, animal_name)
        )""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS marriages (
            guild_id INTEGER,
            user1_id INTEGER,
            user2_id INTEGER,
            married_at TEXT,
            PRIMARY KEY (guild_id, user1_id)
        )""")
        await conn.commit()

# -------------------------------------------------------------
# ANIMAL DATA (Tiers: Common, Uncommon, Rare, Epic, Mythic, Legendary)
# -------------------------------------------------------------
ANIMALS = {
    "common": [
        ("🐭", "Mouse", 50),
        ("🐸", "Frog", 50),
        ("🐥", "Baby Chick", 50),
        ("🐹", "Hamster", 50),
        ("🐰", "Bunny", 50),
        ("🦇", "Bat", 50),
        ("🐿️", "Chipmunk", 50),
        ("🐦", "Sparrow", 50),
        ("🐌", "Snail", 50),
        ("🐛", "Caterpillar", 50),
    ],
    "uncommon": [
        ("🦊", "Red Fox", 150),
        ("🐺", "Dire Wolf", 150),
        ("🐗", "Wild Boar", 150),
        ("🦝", "Raccoon", 150),
        ("🐒", "Monkey", 150),
        ("🦨", "Skunk", 150),
        ("🦔", "Hedgehog", 150),
        ("🦉", "Barn Owl", 150),
        ("🦆", "Mallard Duck", 150),
        ("🦦", "River Otter", 150),
    ],
    "rare": [
        ("🐼", "Giant Panda", 500),
        ("🐯", "Bengal Tiger", 500),
        ("🦁", "King Lion", 500),
        ("🐻", "Grizzly Bear", 500),
        ("🐬", "Dolphin", 500),
        ("🦈", "Mako Shark", 500),
        ("🐊", "Alligator", 500),
        ("🦚", "Royal Peacock", 500),
        ("🦅", "Bald Eagle", 500),
        ("🐆", "Snow Leopard", 500),
    ],
    "epic": [
        ("🦄", "Mystic Unicorn", 2_000),
        ("🦅", "Fire Phoenix", 2_000),
        ("🐙", "Deepsea Kraken", 2_000),
        ("🦕", "Brachiosaurus", 2_000),
        ("🐉", "Emerald Wyvern", 2_000),
        ("🦭", "Celestial Seal", 2_000),
        ("🦩", "Solar Flamingo", 2_000),
    ],
    "mythic": [
        ("🐲", "Ancient Dragon", 10_000),
        ("👑", "Golden Griffin", 10_000),
        ("🌌", "Celestial Fox", 10_000),
        ("⚡", "Thunder Bird", 10_000),
        ("🌋", "Infernal Behemoth", 10_000),
    ],
    "legendary": [
        ("✨", "Cosmic Kitty", 50_000),
        ("💫", "Eternal Leviathan", 50_000),
        ("🌟", "God Cat", 50_000),
        ("🔮", "Void Phantom", 50_000),
    ],
}

TIER_COLORS = {
    "common": 0x95A5A6,
    "uncommon": 0x2ECC71,
    "rare": 0x3498DB,
    "epic": 0x9B59B6,
    "mythic": 0xF1C40F,
    "legendary": 0xE91E63,
}

TIER_NAMES = {
    "common": "⚪ Common",
    "uncommon": "🟢 Uncommon",
    "rare": "🔵 Rare",
    "epic": "🟣 Epic",
    "mythic": "🟡 Mythic",
    "legendary": "💎 Legendary",
}

# -------------------------------------------------------------
# ANIME ACTION GIFS & CAPTIONS
# -------------------------------------------------------------
ACTIONS_DATA = {
    "bonk": {
        "title": "🔨 BONK!",
        "color": 0xFEE75C,
        "gifs": [
            "https://media.tenor.com/P_Vf4q6c0rQAAAAC/cheems-bonk.gif",
            "https://media.tenor.com/2s_nK2pZhyMAAAAC/anime-bonk.gif",
            "https://media.tenor.com/wOozmK-TtNsAAAAC/bonk-doge.gif",
            "https://media.tenor.com/4qK49d4g88gAAAAC/anime-slap-bonk.gif",
        ],
        "lines": [
            "{author} pulled out a 100-ton squeaky cartoon hammer and **BONKED** {target}! 💥",
            "*BONK!* Go directly to horny jail, {target}! Do not pass GO! 🚨",
            "{author} delivered a devastating tactical bonk straight to {target}'s forehead! 🔨",
            "{target} got bonked into the next dimension by {author}! Ouch! 💫",
        ]
    },
    "slap": {
        "title": "👋 SLAP!",
        "color": 0xED4245,
        "gifs": [
            "https://media.tenor.com/Ws6Dm1ZW_vMAAAAC/girl-slap.gif",
            "https://media.tenor.com/CvBTA0GyrogAAAAC/anime-slap.gif",
            "https://media.tenor.com/XiYuCeAUhTIAAAAC/anime-slap-angry.gif",
        ],
        "lines": [
            "{author} slapped some sense into {target}! 👋",
            "The echo of {author}'s slap against {target}'s cheek resonated across the whole server! 💥",
            "{author} delivered a 5-star slap across {target}'s face! ✨",
        ]
    },
    "hug": {
        "title": "🤗 Warm Hug!",
        "color": 0x57F287,
        "gifs": [
            "https://media.tenor.com/kCZvsN3U9VUAAAAC/anime-hug.gif",
            "https://media.tenor.com/7oAptNOZ0d8AAAAC/hug-anime.gif",
            "https://media.tenor.com/wUQH5C6KlSMAAAAC/anime-cuddle.gif",
        ],
        "lines": [
            "{author} wrapped their arms around {target} in a super warm and cozy hug! ❤️",
            "{author} glomped {target} tightly and refuses to let go! 🥰",
            "{author} gave {target} the biggest, warmest embrace ever! 🌸",
        ]
    },
    "kiss": {
        "title": "💋 Sweet Kiss!",
        "color": 0xEB459E,
        "gifs": [
            "https://media.tenor.com/F02Ep3b2qJgAAAAC/anime-kiss.gif",
            "https://media.tenor.com/d_T8l9d_qLAAAAAC/kiss-anime.gif",
            "https://media.tenor.com/I83WJ7-2ttgAAAAC/anime-kissing.gif",
        ],
        "lines": [
            "{author} leaned in and gave {target} a sweet kiss on the cheek! 🌸",
            "*smooch* {author} kissed {target}! Look at them blushing red! 🥰",
            "{author} gave {target} a tender, romantic kiss! ✨",
        ]
    },
    "pat": {
        "title": "🐾 Headpats!",
        "color": 0x5865F2,
        "gifs": [
            "https://media.tenor.com/E6fWJRrwP_AAAAAC/anime-pat.gif",
            "https://media.tenor.com/kM1m3Ff1i7UAAAAC/headpat-anime.gif",
            "https://media.tenor.com/Y7x9Yj8qKzAAAAAC/pat-anime.gif",
        ],
        "lines": [
            "{author} gently patted {target} on the head. Good boy/girl! 🐾",
            "{author} gave {target} soft, affectionate headpats! *purrrrr* 🐱",
            "{author} strokes {target}'s hair tenderly. There, there! 💫",
        ]
    },
    "punch": {
        "title": "🥊 K.O. Punch!",
        "color": 0xE74C3C,
        "gifs": [
            "https://media.tenor.com/Ev1H7K4J5fEAAAAC/anime-punch.gif",
            "https://media.tenor.com/UH8J05CXEBoAAAAC/anime-punch-fight.gif",
        ],
        "lines": [
            "{author} hit {target} with an anime knockout punch! Critical hit! 💥",
            "ONE PUNCH! {author} sent {target} flying into orbit! 🚀",
            "{author} socked {target} right in the jaw! K.O.! 🥊",
        ]
    },
    "bite": {
        "title": "🦷 Playful Chomp!",
        "color": 0xE67E22,
        "gifs": [
            "https://media.tenor.com/eT5zK6h1n2YAAAAC/anime-bite.gif",
            "https://media.tenor.com/No9Z4J_4hWAAAAAC/bite-anime.gif",
        ],
        "lines": [
            "{author} took a playful little chomp out of {target}! *nom nom* 😋",
            "{target} got bitten by {author}! Ow, watch the teeth! 😼",
            "{author} gnaws on {target}'s ear affectionately! 🐾",
        ]
    },
    "cuddle": {
        "title": "🧸 Cozy Cuddle!",
        "color": 0x9B59B6,
        "gifs": [
            "https://media.tenor.com/DV7M8iO5_lAAAAAC/anime-cuddle.gif",
            "https://media.tenor.com/S6M_pSjVlWwAAAAC/cuddle-anime.gif",
        ],
        "lines": [
            "{author} snuggled up and cuddled with {target} under a fluffy blanket! 🧸",
            "{author} pulled {target} close for the coziest snuggle ever! 💖",
        ]
    },
    "poke": {
        "title": "👉 Boop / Poke!",
        "color": 0x1ABC9C,
        "gifs": [
            "https://media.tenor.com/3dK8nN2WqSIAAAAC/anime-poke.gif",
            "https://media.tenor.com/y4vK0yB3pG8AAAAC/poke-anime.gif",
        ],
        "lines": [
            "{author} poked {target}'s squishy cheek repeatedly! *poke poke* 👉",
            "{author} booped {target} right on the nose! Boop! 🐽",
        ]
    },
    "feed": {
        "title": "🍰 Feeding Time!",
        "color": 0xF39C12,
        "gifs": [
            "https://media.tenor.com/I84yvWc_V84AAAAC/anime-feed.gif",
            "https://media.tenor.com/M6L2yY-hT68AAAAC/feed-anime.gif",
        ],
        "lines": [
            "{author} feeds {target} a delicious sweet treat! Say 'aaahhh~' 🍓🍰",
            "{author} fed {target} yummy snacks! *nom nom nom* 🍪",
        ]
    }
}

# -------------------------------------------------------------
# CRASH GAME VIEW (Interactive Rocket Multiplier)
# -------------------------------------------------------------
class CrashView(discord.ui.View):
    def __init__(self, user_id: int, guild_id: int, bet: int, crash_point: float):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.guild_id = guild_id
        self.bet = bet
        self.crash_point = crash_point
        self.cashed_out = False
        self.multiplier = 1.00
        self.ended = False

    @discord.ui.button(label="🛑 Cash Out (+0 🪙)", style=discord.ButtonStyle.success)
    async def cash_out_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This is not your rocket game!", ephemeral=True)
        if self.ended or self.cashed_out:
            return await interaction.response.send_message("❌ Game has already ended.", ephemeral=True)

        self.cashed_out = True
        self.ended = True
        winnings = int(self.bet * self.multiplier)
        await update_wallet(self.user_id, self.guild_id, winnings)

        self.clear_items()
        embed = discord.Embed(
            title="🚀 CRASH — Cashed Out!",
            description=(
                f"🎉 **Spectacular! You cashed out in time!**\n\n"
                f"• **Exit Multiplier:** `🚀 {self.multiplier:.2f}x`\n"
                f"• **Initial Bet:** `{self.bet:,}` 🪙\n"
                f"• **Total Payout:** `+{winnings:,}` 🪙\n"
                f"• **Net Profit:** `+{winnings - self.bet:,}` 🪙"
            ),
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Played by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        if not self.ended:
            self.ended = True
            self.clear_items()


# -------------------------------------------------------------
# MARRIAGE VIEW (Accept / Decline Proposal)
# -------------------------------------------------------------
class MarriageView(discord.ui.View):
    def __init__(self, proposer: discord.Member, target: discord.Member):
        super().__init__(timeout=60)
        self.proposer = proposer
        self.target = target
        self.responded = False

    async def on_timeout(self):
        if not self.responded:
            self.responded = True
            self.clear_items()

    @discord.ui.button(label="💍 Accept Proposal", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("❌ This proposal is not for you!", ephemeral=True)
        if self.responded:
            return
        self.responded = True
        self.clear_items()

        now = discord.utils.utcnow().strftime("%B %d, %Y")
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO marriages (guild_id, user1_id, user2_id, married_at) VALUES (?, ?, ?, ?)",
                (interaction.guild.id, self.proposer.id, self.target.id, now)
            )
            await conn.execute(
                "INSERT OR REPLACE INTO marriages (guild_id, user1_id, user2_id, married_at) VALUES (?, ?, ?, ?)",
                (interaction.guild.id, self.target.id, self.proposer.id, now)
            )
            await conn.commit()

        embed = discord.Embed(
            title="💒 JUST MARRIED! 💍",
            description=(
                f"🎉 **{self.target.mention} said YES to {self.proposer.mention}!**\n\n"
                f"🔔 The church bells are ringing across the server!\n"
                f"May your love be eternal and your coin balance never hit zero! ✨\n\n"
                f"📅 **Wedding Date:** `{now}`"
            ),
            color=0xEB459E,
            timestamp=discord.utils.utcnow()
        )
        embed.set_image(url="https://media.tenor.com/E8w9k1nJ7W0AAAAC/anime-wedding.gif")
        embed.set_footer(text="🐾 Meow Love & Marriage")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="💔 Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("❌ This proposal is not for you!", ephemeral=True)
        if self.responded:
            return
        self.responded = True
        self.clear_items()

        embed = discord.Embed(
            title="💔 Proposal Rejected",
            description=f"Oof... {self.target.mention} turned down {self.proposer.mention}'s marriage proposal.\n*F in the chat for our fallen soldier.* 🪦",
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        await interaction.response.edit_message(embed=embed, view=self)


# -------------------------------------------------------------
# SOCIAL & SAFARI COG IMPLEMENTATION
# -------------------------------------------------------------
class Social(commands.Cog):
    """Pet safari hunting, zoo collection, rocket crash game, social roleplay actions, and marriage system!"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            cmd_name = interaction.command.name if interaction.command else None
            if cmd_name == "crash":
                for mod in ["economy", "games"]:
                    if not self.bot.is_module_enabled(interaction.guild_id, mod):
                        await interaction.response.send_message(
                            f"⚠️ The **{mod.title()}** system is currently **turned off** on this server.\n"
                            "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                            ephemeral=True
                        )
                        return False
            elif cmd_name == "sellanimals":
                for mod in ["social", "economy"]:
                    if not self.bot.is_module_enabled(interaction.guild_id, mod):
                        await interaction.response.send_message(
                            f"⚠️ The **{mod.title()}** system is currently **turned off** on this server.\n"
                            "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                            ephemeral=True
                        )
                        return False
            else:
                if not self.bot.is_module_enabled(interaction.guild_id, "social"):
                    await interaction.response.send_message(
                        "⚠️ The **Social Roleplay** system is currently **turned off** on this server.\n"
                        "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                        ephemeral=True
                    )
                    return False
        return True

    # ---------------------------------------------------------
    # 1. ANIMAL HUNTING & ZOO
    # ---------------------------------------------------------
    @app_commands.command(name="hunt", description="Go hunting in the wild and catch rare animals for your zoo!")
    async def hunt(self, interaction: discord.Interaction):
        await setup_social_tables()
        await ensure_eco(interaction.user.id, interaction.guild.id)

        # Catch 1 to 3 animals per hunt
        num_catches = random.choices([1, 2, 3], weights=[0.40, 0.45, 0.15])[0]
        catches = []

        tier_weights = {
            "common": 0.52,
            "uncommon": 0.26,
            "rare": 0.14,
            "epic": 0.055,
            "mythic": 0.02,
            "legendary": 0.005,
        }
        tiers = list(tier_weights.keys())
        weights = list(tier_weights.values())

        async with aiosqlite.connect(DB_PATH) as conn:
            for _ in range(num_catches):
                chosen_tier = random.choices(tiers, weights=weights)[0]
                emoji, name, val = random.choice(ANIMALS[chosen_tier])
                catches.append((chosen_tier, emoji, name, val))

                await conn.execute("""
                    INSERT INTO user_animals (guild_id, user_id, animal_name, emoji, tier, count)
                    VALUES (?, ?, ?, ?, ?, 1)
                    ON CONFLICT(guild_id, user_id, animal_name)
                    DO UPDATE SET count = count + 1
                """, (interaction.guild.id, interaction.user.id, name, emoji, chosen_tier))
            await conn.commit()

        # Bonus pocket coins found on hunt
        bonus = random.randint(40, 220)
        await update_wallet(interaction.user.id, interaction.guild.id, bonus)

        # Highest tier caught determines embed color
        highest_tier = "common"
        for t, _, _, _ in catches:
            if list(ANIMALS.keys()).index(t) > list(ANIMALS.keys()).index(highest_tier):
                highest_tier = t

        embed = discord.Embed(
            title="🌲 Hunting Expedition Results",
            description=f"You ventured deep into the wilderness and brought back:\n",
            color=TIER_COLORS[highest_tier],
            timestamp=discord.utils.utcnow()
        )

        for tier, emoji, name, val in catches:
            embed.description += f"• {emoji} **{name}** — {TIER_NAMES[tier]} *(Value: {val:,} 🪙)*\n"

        embed.description += f"\n🪙 You also scavenged **+{bonus:,}** coins from the wild!"
        embed.set_footer(text="🐾 Tip: Run /zoo to inspect your collection or /sellanimals to cash out!")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="zoo", description="View your or another member's caught animals collection.")
    @app_commands.describe(member="Member's zoo to view")
    async def zoo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        await setup_social_tables()

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT animal_name, emoji, tier, count FROM user_animals WHERE guild_id=? AND user_id=? ORDER BY count DESC",
                (interaction.guild.id, member.id)
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            embed = discord.Embed(
                title=f"🐾 {member.display_name}'s Zoo is Empty!",
                description=f"No animals caught yet! Run `/hunt` to begin your wilderness safari.",
                color=0x95A5A6
            )
            return await send_card(interaction, embed=embed)

        # Organize by tier
        tier_buckets = {"legendary": [], "mythic": [], "epic": [], "rare": [], "uncommon": [], "common": []}
        total_animals = 0
        total_value = 0

        # Lookup price map
        price_map = {}
        for t, list_a in ANIMALS.items():
            for e, n, p in list_a:
                price_map[n] = p

        for name, emoji, tier, count in rows:
            total_animals += count
            total_value += count * price_map.get(name, 50)
            tier_buckets.get(tier, []).append(f"{emoji} {name} `x{count}`")

        embed = discord.Embed(
            title=f"🦁 {member.display_name}'s Wildlife Zoo",
            description=f"**Total Animals Caught:** `{total_animals:,}` | **Est. Value:** `{total_value:,}` 🪙\n",
            color=0x2ECC71,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        for tier in ["legendary", "mythic", "epic", "rare", "uncommon", "common"]:
            items = tier_buckets[tier]
            if items:
                embed.add_field(
                    name=f"{TIER_NAMES[tier]} ({len(items)} species)",
                    value=" • ".join(items[:15]) + ("..." if len(items) > 15 else ""),
                    inline=False
                )

        embed.set_footer(text="🐾 Meow Zoo • Run /sellanimals to exchange animals for coins")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="sellanimals", description="Sell caught zoo animals for coins.")
    @app_commands.describe(tier="Which tier to sell")
    @app_commands.choices(tier=[
        app_commands.Choice(name="All Common Animals", value="common"),
        app_commands.Choice(name="All Uncommon Animals", value="uncommon"),
        app_commands.Choice(name="All Rare Animals", value="rare"),
        app_commands.Choice(name="Common & Uncommon (Keep Rare+)", value="common_uncommon"),
        app_commands.Choice(name="Sell Everything (Full Liquidation)", value="all"),
    ])
    async def sellanimals(self, interaction: discord.Interaction, tier: str):
        await setup_social_tables()

        price_map = {}
        for t, list_a in ANIMALS.items():
            for e, n, p in list_a:
                price_map[n] = p

        async with aiosqlite.connect(DB_PATH) as conn:
            if tier == "common":
                query = "SELECT animal_name, count FROM user_animals WHERE guild_id=? AND user_id=? AND tier='common'"
                delete_query = "DELETE FROM user_animals WHERE guild_id=? AND user_id=? AND tier='common'"
            elif tier == "uncommon":
                query = "SELECT animal_name, count FROM user_animals WHERE guild_id=? AND user_id=? AND tier='uncommon'"
                delete_query = "DELETE FROM user_animals WHERE guild_id=? AND user_id=? AND tier='uncommon'"
            elif tier == "rare":
                query = "SELECT animal_name, count FROM user_animals WHERE guild_id=? AND user_id=? AND tier='rare'"
                delete_query = "DELETE FROM user_animals WHERE guild_id=? AND user_id=? AND tier='rare'"
            elif tier == "common_uncommon":
                query = "SELECT animal_name, count FROM user_animals WHERE guild_id=? AND user_id=? AND tier IN ('common', 'uncommon')"
                delete_query = "DELETE FROM user_animals WHERE guild_id=? AND user_id=? AND tier IN ('common', 'uncommon')"
            else:
                query = "SELECT animal_name, count FROM user_animals WHERE guild_id=? AND user_id=?"
                delete_query = "DELETE FROM user_animals WHERE guild_id=? AND user_id=?"

            async with conn.execute(query, (interaction.guild.id, interaction.user.id)) as cur:
                rows = await cur.fetchall()

            if not rows:
                return await interaction.response.send_message("❌ You don't have any animals in that category to sell!", ephemeral=True)

            total_coins = 0
            total_count = 0
            for name, count in rows:
                p = price_map.get(name, 50)
                total_coins += p * count
                total_count += count

            await conn.execute(delete_query, (interaction.guild.id, interaction.user.id))
            await conn.commit()

        await update_wallet(interaction.user.id, interaction.guild.id, total_coins)

        embed = discord.Embed(
            title="💰 Animals Sold!",
            description=f"✅ You sold **{total_count:,}** animals and received **+{total_coins:,}** 🪙 in your wallet!",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Meow Market")
        await send_card(interaction, embed=embed)

    # ---------------------------------------------------------
    # 2. HIGH-STAKES CRASH GAME
    # ---------------------------------------------------------
    @app_commands.command(name="crash", description="High-stakes rocket crash game! Cash out before the rocket explodes!")
    @app_commands.describe(bet="Amount of coins to bet")
    async def crash(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, None]):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙 in your wallet.", ephemeral=True)

        # Deduct bet upfront
        await update_wallet(interaction.user.id, interaction.guild.id, -bet)

        # Realistic crash curve
        if random.random() < 0.08:
            crash_point = 1.00  # 8% instant bust
        else:
            crash_point = round(1.0 + random.expovariate(0.65), 2)
            crash_point = max(1.02, min(crash_point, 50.00))

        view = CrashView(interaction.user.id, interaction.guild.id, bet, crash_point)

        embed = discord.Embed(
            title="🚀 Rocket Crash Game",
            description=(
                f"**The rocket is preparing for launch!**\n\n"
                f"• **Bet Placed:** `{bet:,}` 🪙\n"
                f"• **Current Multiplier:** `🚀 1.00x`\n"
                f"• **Potential Win:** `{bet:,}` 🪙\n\n"
                f"*Click **Cash Out** before the rocket explodes!*"
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()

        # Step progression
        current = 1.00
        step = 0.15
        while current < crash_point:
            await asyncio.sleep(1.2)
            if view.cashed_out:
                return  # Handled by view button callback

            current = round(current + step, 2)
            step += 0.05
            if current >= crash_point:
                break

            view.multiplier = current
            view.children[0].label = f"🛑 Cash Out (+{int(bet * current):,} 🪙)"
            embed.description = (
                f"**The rocket is soaring higher into space!**\n\n"
                f"• **Bet Placed:** `{bet:,}` 🪙\n"
                f"• **Current Multiplier:** `🚀 {current:.2f}x`\n"
                f"• **Current Value:** `+{int(bet * current):,}` 🪙\n\n"
                f"*Click **Cash Out** before the rocket explodes!*"
            )
            try:
                await msg.edit(embed=embed, view=view)
            except:
                return

        # If reached here without cashing out, rocket exploded
        if not view.cashed_out:
            view.ended = True
            view.clear_items()
            crash_embed = discord.Embed(
                title="💥 ROCKET EXPLODED!",
                description=(
                    f"💀 **BOOM! The rocket crashed at `{crash_point:.2f}x`!**\n\n"
                    f"You did not cash out in time and lost your **{bet:,}** 🪙 bet.\n"
                    f"*Better luck on the next launch!*"
                ),
                color=0xED4245,
                timestamp=discord.utils.utcnow()
            )
            crash_embed.set_footer(text=f"Played by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            try:
                await msg.edit(embed=crash_embed, view=view)
            except:
                pass

    # ---------------------------------------------------------
    # 3. SOCIAL ROLEPLAY ACTIONS
    # ---------------------------------------------------------
    async def _handle_action(self, interaction: discord.Interaction, action: str, member: discord.Member):
        if member.id == interaction.user.id:
            return await interaction.response.send_message(f"❌ You can't {action} yourself!", ephemeral=True)

        data = ACTIONS_DATA[action]
        gif = random.choice(data["gifs"])
        line = random.choice(data["lines"]).format(author=interaction.user.mention, target=member.mention)

        embed = discord.Embed(
            title=data["title"],
            description=line,
            color=data["color"],
            timestamp=discord.utils.utcnow()
        )
        embed.set_image(url=gif)
        await send_card(interaction, embed=embed)

    @app_commands.command(name="action", description="Perform an anime/social roleplay action with another member!")
    @app_commands.describe(action="The roleplay action to perform", member="The member to interact with")
    @app_commands.choices(action=[
        app_commands.Choice(name="🔨 Bonk", value="bonk"),
        app_commands.Choice(name="👋 Slap", value="slap"),
        app_commands.Choice(name="🤗 Hug", value="hug"),
        app_commands.Choice(name="💋 Kiss", value="kiss"),
        app_commands.Choice(name="✨ Headpat", value="pat"),
        app_commands.Choice(name="👊 Punch", value="punch"),
        app_commands.Choice(name="🦷 Bite", value="bite"),
        app_commands.Choice(name="🧸 Cuddle", value="cuddle"),
        app_commands.Choice(name="👉 Poke", value="poke"),
        app_commands.Choice(name="🍰 Feed", value="feed"),
    ])
    async def action(self, interaction: discord.Interaction, action: app_commands.Choice[str], member: discord.Member):
        await self._handle_action(interaction, action.value, member)

    # ---------------------------------------------------------
    # 4. MARRIAGE & LOVE SYSTEM
    # ---------------------------------------------------------
    @app_commands.command(name="marry", description="Propose marriage to another member!")
    @app_commands.describe(member="The lucky member to propose to")
    async def marry(self, interaction: discord.Interaction, member: discord.Member):
        await setup_social_tables()
        if member.id == interaction.user.id:
            return await interaction.response.send_message("❌ You can't marry yourself!", ephemeral=True)
        if member.bot:
            return await interaction.response.send_message("❌ Bots cannot sign marriage certificates!", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute("SELECT user2_id FROM marriages WHERE guild_id=? AND user1_id=?", (interaction.guild.id, interaction.user.id)) as cur:
                if await cur.fetchone():
                    return await interaction.response.send_message("❌ You are already married! Run `/divorce` first.", ephemeral=True)
            async with conn.execute("SELECT user2_id FROM marriages WHERE guild_id=? AND user1_id=?", (interaction.guild.id, member.id)) as cur:
                if await cur.fetchone():
                    return await interaction.response.send_message(f"❌ {member.mention} is already married to someone else!", ephemeral=True)

        embed = discord.Embed(
            title="💍 Marriage Proposal",
            description=f"💖 {interaction.user.mention} has fallen to one knee and proposed to {member.mention}!\n\n*{member.mention}, will you take their hand in holy matrimony?*",
            color=0xEB459E,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url="https://media.tenor.com/7Ie9b4Wc3zUAAAAC/ring-anime.gif")
        view = MarriageView(interaction.user, member)
        await interaction.response.send_message(content=member.mention, embed=embed, view=view)

    @app_commands.command(name="divorce", description="Divorce your current spouse.")
    async def divorce(self, interaction: discord.Interaction):
        await setup_social_tables()
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute("SELECT user2_id, married_at FROM marriages WHERE guild_id=? AND user1_id=?", (interaction.guild.id, interaction.user.id)) as cur:
                row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message("❌ You are not married to anyone!", ephemeral=True)

            spouse_id, date = row
            await conn.execute("DELETE FROM marriages WHERE guild_id=? AND (user1_id=? OR user2_id=?)", (interaction.guild.id, interaction.user.id, interaction.user.id))
            await conn.commit()

        spouse = interaction.guild.get_member(spouse_id)
        s_name = spouse.mention if spouse else f"<@{spouse_id}>"

        embed = discord.Embed(
            title="📄 Divorce Finalized",
            description=f"💔 {interaction.user.mention} and {s_name} are officially divorced.\n*All good things come to an end...*",
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        await send_card(interaction, embed=embed)

    @app_commands.command(name="ship", description="Calculate the romantic compatibility between two members!")
    @app_commands.describe(user1="First member", user2="Second member")
    async def ship(self, interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
        # Deterministic match based on user IDs
        seed = (user1.id * 31 + user2.id * 17) % 101
        percent = seed

        filled = int(percent / 10)
        empty = 10 - filled
        bar = "❤️" * filled + "🖤" * empty

        # Blend names for ship name
        len1 = len(user1.display_name) // 2
        len2 = len(user2.display_name) // 2
        ship_name = user1.display_name[:max(2, len1)] + user2.display_name[max(2, len2):]

        if percent >= 90:
            verdict = "💍 **True Soulmates!** Start planning the wedding right now! 💒"
            color = 0xEB459E
        elif percent >= 75:
            verdict = "💖 **Match Made in Heaven!** The chemistry is off the charts! ✨"
            color = 0x57F287
        elif percent >= 50:
            verdict = "✨ **Cute Couple!** There is definitely a spark here! 🥰"
            color = 0xFEE75C
        elif percent >= 25:
            verdict = "🤔 **Friendzone Territory!** It could work, but requires heavy lifting! 🧗"
            color = 0xE67E22
        else:
            verdict = "💀 **Toxic Biohazard!** Keep at least 500 meters away from each other! ☣️"
            color = 0xED4245

        embed = discord.Embed(
            title="💘 Love Compatibility Scanner",
            description=(
                f"**Ship:** `{user1.display_name}` × `{user2.display_name}`\n"
                f"**Ship Name:** `{ship_name}`\n\n"
                f"**Compatibility:** `{percent}%`\n"
                f"`{bar}`\n\n"
                f"> {verdict}"
            ),
            color=color,
            timestamp=discord.utils.utcnow()
        )
        await send_card(interaction, embed=embed)

    # ---------------------------------------------------------
    # 5. AESTHETIC UWU TEXT TRANSLATOR
    # ---------------------------------------------------------
    @app_commands.command(name="uwu", description="Translate any sentence into cute aesthetic anime speak!")
    @app_commands.describe(text="The message to translate")
    async def uwu(self, interaction: discord.Interaction, text: str):
        # Basic substitution
        uwu_text = text.replace("r", "w").replace("l", "w").replace("R", "W").replace("L", "W")
        uwu_text = uwu_text.replace("ove", "uv").replace("OVE", "UV")

        faces = [
            "(◕‿◕✿)", "(˶ᵔ ᵕ ᵔ˶)", "(⁄ ⁄>⁄ ▽ ⁄<⁄ ⁄)", "✨", "UwU", "nya~",
            "(⑅˘͈ ᵕ ˘͈ )", "(っ˘з(˘⌣˘ )", "(*^ω^*)"
        ]
        face = random.choice(faces)

        # Stuttering
        words = uwu_text.split()
        for i in range(len(words)):
            if len(words[i]) > 3 and random.random() < 0.25:
                words[i] = f"{words[i][0]}-{words[i]}"

        result = " ".join(words) + f" {face}"
        await interaction.response.send_message(f"> {result}")


async def setup(bot: commands.Bot):
    await setup_social_tables()
    await bot.add_cog(Social(bot))
