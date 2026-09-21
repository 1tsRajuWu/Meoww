import discord
from discord import app_commands
from discord.ext import commands
import random
import asyncio
import aiosqlite
from utils import db, gifs
from cogs.economy import get_eco, update_wallet, ensure_eco, take_stake
from utils.style import send_card, display_name, pings

DB_PATH = db.DB_PATH

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

# /hunt hands out free coins and animals, so it needs a cooldown or it can be
# spammed indefinitely.
HUNT_COOLDOWN_SECONDS = 10 * 60

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
# ANIME ACTION CAPTIONS (and what each action looks like)
# -------------------------------------------------------------
# The pictures are deliberately **not** here. Every entry used to carry a list of
# hand-typed ``media.tenor.com`` links, and every one of them answered 404 — which
# the bot cannot notice and Discord draws as an empty box, the "no gif" report.
# ``utils/gifs`` resolves and verifies an image at the moment it is used instead,
# so a deleted file is a missing picture rather than a broken one.
ACTIONS_DATA = {
    "bonk": {
        "title": "🔨 BONK!",
        "color": 0xFEE75C,
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
        "lines": [
            "{author} slapped some sense into {target}! 👋",
            "The echo of {author}'s slap against {target}'s cheek resonated across the whole server! 💥",
            "{author} delivered a 5-star slap across {target}'s face! ✨",
        ]
    },
    "hug": {
        "title": "🤗 Warm Hug!",
        "color": 0x57F287,
        "lines": [
            "{author} wrapped their arms around {target} in a super warm and cozy hug! ❤️",
            "{author} glomped {target} tightly and refuses to let go! 🥰",
            "{author} gave {target} the biggest, warmest embrace ever! 🌸",
        ]
    },
    "kiss": {
        "title": "💋 Sweet Kiss!",
        "color": 0xEB459E,
        "lines": [
            "{author} leaned in and gave {target} a sweet kiss on the cheek! 🌸",
            "*smooch* {author} kissed {target}! Look at them blushing red! 🥰",
            "{author} gave {target} a tender, romantic kiss! ✨",
        ]
    },
    "pat": {
        "title": "🐾 Headpats!",
        "color": 0x5865F2,
        "lines": [
            "{author} gently patted {target} on the head. Good boy/girl! 🐾",
            "{author} gave {target} soft, affectionate headpats! *purrrrr* 🐱",
            "{author} strokes {target}'s hair tenderly. There, there! 💫",
        ]
    },
    "punch": {
        "title": "🥊 K.O. Punch!",
        "color": 0xE74C3C,
        "lines": [
            "{author} hit {target} with an anime knockout punch! Critical hit! 💥",
            "ONE PUNCH! {author} sent {target} flying into orbit! 🚀",
            "{author} socked {target} right in the jaw! K.O.! 🥊",
        ]
    },
    "bite": {
        "title": "🦷 Playful Chomp!",
        "color": 0xE67E22,
        "lines": [
            "{author} took a playful little chomp out of {target}! *nom nom* 😋",
            "{target} got bitten by {author}! Ow, watch the teeth! 😼",
            "{author} gnaws on {target}'s ear affectionately! 🐾",
        ]
    },
    "cuddle": {
        "title": "🧸 Cozy Cuddle!",
        "color": 0x9B59B6,
        "lines": [
            "{author} snuggled up and cuddled with {target} under a fluffy blanket! 🧸",
            "{author} pulled {target} close for the coziest snuggle ever! 💖",
        ]
    },
    "poke": {
        "title": "👉 Boop / Poke!",
        "color": 0x1ABC9C,
        "lines": [
            "{author} poked {target}'s squishy cheek repeatedly! *poke poke* 👉",
            "{author} booped {target} right on the nose! Boop! 🐽",
        ]
    },
    "feed": {
        "title": "🍰 Feeding Time!",
        "color": 0xF39C12,
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
        # Verified or absent: the hand-typed tenor link that used to be here
        # answered 404, so the wedding card was drawn with a broken image.
        wedding_gif = await gifs.action_gif("wedding")
        if wedding_gif:
            embed.set_image(url=wedding_gif)
        embed.set_footer(text="Meow Love & Marriage")
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
    @app_commands.checks.cooldown(1, HUNT_COOLDOWN_SECONDS, key=lambda i: (i.guild_id, i.user.id))
    async def hunt(self, interaction: discord.Interaction):
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
            description="You ventured deep into the wilderness and brought back:\n",
            color=TIER_COLORS[highest_tier],
            timestamp=discord.utils.utcnow()
        )

        for tier, emoji, name, val in catches:
            embed.description += f"• {emoji} **{name}** — {TIER_NAMES[tier]} *(Value: {val:,} 🪙)*\n"

        embed.description += f"\n🪙 You also scavenged **+{bonus:,}** coins from the wild!"
        embed.set_footer(text="Tip: Run /zoo to inspect your collection or /sellanimals to cash out!")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="zoo", description="View your or another member's caught animals collection.")
    @app_commands.describe(member="Member's zoo to view")
    async def zoo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT animal_name, emoji, tier, count FROM user_animals WHERE guild_id=? AND user_id=? ORDER BY count DESC",
                (interaction.guild.id, member.id)
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            embed = discord.Embed(
                title=f"{member.display_name}'s Zoo is Empty!",
                description="No animals caught yet! Run `/hunt` to begin your wilderness safari.",
                color=0x95A5A6
            )
            return await send_card(interaction, embed=embed)

        # Organize by tier
        tier_buckets = {"legendary": [], "mythic": [], "epic": [], "rare": [], "uncommon": [], "common": []}
        total_animals = 0
        total_value = 0

        # Lookup price map
        price_map = {}
        for list_a in ANIMALS.values():
            for _emoji, name, price in list_a:
                price_map[name] = price

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

        embed.set_footer(text="Meow Zoo • Run /sellanimals to exchange animals for coins")
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
        price_map = {}
        for list_a in ANIMALS.values():
            for _emoji, name, price in list_a:
                price_map[name] = price

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

        await ensure_eco(interaction.user.id, interaction.guild.id)

        def _sell(cur):
            """Read, delete and pay inside one transaction.

            The rows were read on one connection and the coins credited on
            another, so two simultaneous sales of the same animals both computed
            a price from the same rows — the first deleted them, the second
            deleted nothing and was still paid.
            """
            cur.execute(query, (interaction.guild.id, interaction.user.id))
            rows = cur.fetchall()
            if not rows:
                return None
            total_coins = 0
            total_count = 0
            for name, count in rows:
                total_coins += price_map.get(name, 50) * count
                total_count += count
            cur.execute(delete_query, (interaction.guild.id, interaction.user.id))
            if cur.rowcount != len(rows):
                raise RuntimeError(
                    f"animal rows changed during sale ({cur.rowcount} of {len(rows)} removed)"
                )
            cur.execute(
                "UPDATE economy SET wallet = wallet + ? WHERE user_id=? AND guild_id=?",
                (total_coins, interaction.user.id, interaction.guild.id),
            )
            return total_coins, total_count

        sold = await asyncio.to_thread(db.run_atomic, _sell)
        if not sold:
            return await interaction.response.send_message(
                "❌ You don't have any animals in that category to sell!", ephemeral=True
            )
        total_coins, total_count = sold

        embed = discord.Embed(
            title="💰 Animals Sold!",
            description=f"✅ You sold **{total_count:,}** animals and received **+{total_coins:,}** 🪙 in your wallet!",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="Meow Market")
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

        # Hold the stake atomically. A plain update_wallet(-bet) clamps at zero,
        # so two /crash games fired at the same instant could each start without
        # really paying for themselves.
        if not await take_stake(interaction.user.id, interaction.guild.id, bet):
            return await interaction.response.send_message(
                "❌ You don't have enough coins to cover that bet.", ephemeral=True
            )

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
            except Exception as e:
                # The card can no longer be updated (deleted, or the API refused).
                # The round cannot be played, so end it and refund the stake. The
                # old code returned here with the buttons still live, which let a
                # player cash out at a multiplier the rocket may never have had.
                print(f"[CRASH] Could not update the game card: {e}")
                view.ended = True
                view.clear_items()
                await update_wallet(interaction.user.id, interaction.guild.id, bet)
                void_embed = discord.Embed(
                    title="🚀 Launch Aborted",
                    description=(
                        "The game card could not be updated, so the round was cancelled.\n"
                        f"Your **{bet:,}** 🪙 bet has been **refunded**."
                    ),
                    color=0xFEE75C,
                    timestamp=discord.utils.utcnow(),
                )
                try:
                    await msg.edit(embed=void_embed, view=view)
                except Exception:
                    pass
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
            except Exception:
                pass

    # ---------------------------------------------------------
    # 3. SOCIAL ROLEPLAY ACTIONS
    # ---------------------------------------------------------
    async def _handle_action(self, interaction: discord.Interaction, action: str, member: discord.Member):
        if member.id == interaction.user.id:
            return await interaction.response.send_message(f"❌ You can't {action} yourself!", ephemeral=True)

        # The picture is fetched and verified first, so the reply is deferred up
        # front rather than answering twice on a slow provider.
        await interaction.response.defer()

        data = ACTIONS_DATA[action]
        line = random.choice(data["lines"]).format(author=interaction.user.mention, target=member.mention)

        embed = discord.Embed(
            title=data["title"],
            description=line,
            color=data["color"],
            timestamp=discord.utils.utcnow()
        )

        # Verified, or nothing: an unconfirmed link used to be drawn as an empty
        # placeholder, which looks like the bot is broken (see utils/gifs).
        gif = await gifs.action_gif(action)
        if gif:
            embed.set_image(url=gif)

        embed.set_footer(text=f"{interaction.user.display_name} → {member.display_name}")

        # Cards suppress mentions by default, and this one *is* about somebody who
        # is supposed to be told. The target is the only person notified: pinging
        # the author back at themselves is noise nobody asked for.
        await send_card(interaction, embed=embed, allowed_mentions=pings(member))

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
        # Same verified-or-absent rule as the actions: this was another dead
        # tenor link. Full width rather than the thumbnail it used to be, because
        # this is the card people screenshot and a 60px corner was the part of it
        # nobody could see.
        ring_gif = await gifs.action_gif("propose")
        if ring_gif:
            embed.set_image(url=ring_gif)
        view = MarriageView(interaction.user, member)
        # A legacy send with the mention in ``content``, which is what actually
        # notifies — the proposal is useless if the person proposed to misses it.
        await interaction.response.send_message(content=member.mention, embed=embed, view=view)

    @app_commands.command(name="divorce", description="Divorce your current spouse.")
    async def divorce(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute("SELECT user2_id FROM marriages WHERE guild_id=? AND user1_id=?", (interaction.guild.id, interaction.user.id)) as cur:
                row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message("❌ You are not married to anyone!", ephemeral=True)

            spouse_id = row[0]
            await conn.execute("DELETE FROM marriages WHERE guild_id=? AND (user1_id=? OR user2_id=?)", (interaction.guild.id, interaction.user.id, interaction.user.id))
            await conn.commit()

        # Plain name, not a mention: a mention inside a V2 card notifies, and an
        # uncached spouse would otherwise render as a raw "<@id>".
        s_name = await display_name(interaction.guild, spouse_id, client=interaction.client)

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
        # The text is member-supplied and sent as plain content, so without this
        # a member could make the bot ping @everyone or a role for them.
        await interaction.response.send_message(
            f"> {result}", allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Social(bot))
