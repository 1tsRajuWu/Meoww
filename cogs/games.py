import discord
from discord import app_commands
from discord.ext import commands
import random
import aiosqlite
from utils import db
from cogs.economy import get_eco, update_wallet, ensure_eco

TRIVIA_QUESTIONS = [
    ("What is the capital of France?", "paris", ["London", "Berlin", "Paris", "Madrid"]),
    ("What is 12 × 12?", "144", ["132", "144", "156", "168"]),
    ("What planet is closest to the Sun?", "mercury", ["Venus", "Earth", "Mercury", "Mars"]),
    ("How many sides does a hexagon have?", "6", ["5", "6", "7", "8"]),
    ("What is the largest ocean?", "pacific", ["Atlantic", "Indian", "Pacific", "Arctic"]),
    ("Who wrote Harry Potter?", "j.k. rowling", ["Stephen King", "J.K. Rowling", "Tolkien", "R.R. Martin"]),
    ("What is the fastest land animal?", "cheetah", ["Lion", "Horse", "Cheetah", "Leopard"]),
    ("How many colors are in a rainbow?", "7", ["5", "6", "7", "8"]),
    ("What is the chemical symbol for gold?", "au", ["Go", "Gd", "Au", "Ag"]),
    ("What year did World War 2 end?", "1945", ["1943", "1944", "1945", "1946"]),
    ("What is the square root of 144?", "12", ["11", "12", "13", "14"]),
    ("Which planet has rings?", "saturn", ["Jupiter", "Saturn", "Uranus", "Neptune"]),
]

SLOT_SYMBOLS = ["🍒", "🍋", "🍊", "🍇", "💎", "7️⃣", "⭐"]
SLOT_MULTIPLIERS = {"🍒": 2, "🍋": 2.5, "🍊": 3, "🍇": 4, "⭐": 5, "💎": 10, "7️⃣": 20}


class BlackjackView(discord.ui.View):
    def __init__(self, player_hand, dealer_hand, deck, bet, user_id, guild_id):
        super().__init__(timeout=45)
        self.player_hand = player_hand
        self.dealer_hand = dealer_hand
        self.deck = deck
        self.bet = bet
        self.user_id = user_id
        self.guild_id = guild_id
        self.finished = False

    def hand_value(self, hand):
        value = 0
        aces = 0
        for card in hand:
            if card in ["J", "Q", "K"]:
                value += 10
            elif card == "A":
                aces += 1
                value += 11
            else:
                value += int(card)
        while value > 21 and aces:
            value -= 10
            aces -= 1
        return value

    def format_hand(self, hand, hide_second=False):
        if hide_second:
            return f"`[{hand[0]}]` `[ ? ]`"
        return " ".join(f"`[{c}]`" for c in hand)

    def make_embed(self, result=None):
        pv = self.hand_value(self.player_hand)
        dv = self.hand_value(self.dealer_hand)
        hide = result is None

        colors = {
            "win": 0x57F287,
            "lose": 0xED4245,
            "push": 0xFEE75C,
            "blackjack": 0x57F287,
        }
        color = colors.get(result, 0x5865F2)

        embed = discord.Embed(
            title="🃏 Blackjack Table",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name=f"👤 Your Hand ({pv})",
            value=self.format_hand(self.player_hand),
            inline=False
        )
        embed.add_field(
            name=f"🤖 Dealer's Hand ({'?' if hide else dv})",
            value=self.format_hand(self.dealer_hand, hide_second=hide),
            inline=False
        )
        embed.add_field(name="💰 Bet Placed", value=f"**{self.bet:,}** 🪙", inline=True)

        if result == "win":
            embed.description = f"🎉 **You beat the dealer and won `{self.bet * 2:,}` 🪙!**"
        elif result == "lose":
            embed.description = f"💀 **Bust / Dealer won! You lost your `{self.bet:,}` 🪙 bet.**"
        elif result == "push":
            embed.description = f"🤝 **It's a push (tie)! Your `{self.bet:,}` 🪙 bet was refunded.**"
        elif result == "blackjack":
            payout = int(self.bet * 2.5)
            embed.description = f"🔥 **NATURAL BLACKJACK! You won `{payout:,}` 🪙!**"
        else:
            embed.description = "Choose whether to **Hit** or **Stand** below:"
            
        embed.set_footer(text="🐾 Meow Casino • 45s turn timer")
        return embed

    async def on_timeout(self):
        if not self.finished:
            self.finished = True
            self.clear_items()

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.success, emoji="🃏")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id or self.finished:
            return await interaction.response.send_message("❌ This is not your game!", ephemeral=True)
        if not self.deck:
            self.deck = [str(i) for i in range(2, 11)] + ["J", "Q", "K", "A"]
            random.shuffle(self.deck)
        self.player_hand.append(self.deck.pop())
        pv = self.hand_value(self.player_hand)
        if pv > 21:
            self.finished = True
            self.clear_items()
            await interaction.response.edit_message(embed=self.make_embed("lose"), view=self)
        elif pv == 21:
            await self.do_stand(interaction)
        else:
            await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.danger, emoji="✋")
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id or self.finished:
            return await interaction.response.send_message("❌ This is not your game!", ephemeral=True)
        await self.do_stand(interaction)

    async def do_stand(self, interaction):
        self.finished = True
        while self.hand_value(self.dealer_hand) < 17:
            if not self.deck:
                break
            self.dealer_hand.append(self.deck.pop())
        pv = self.hand_value(self.player_hand)
        dv = self.hand_value(self.dealer_hand)
        self.clear_items()
        if dv > 21 or pv > dv:
            await update_wallet(self.user_id, self.guild_id, self.bet * 2)
            result = "win"
        elif pv == dv:
            await update_wallet(self.user_id, self.guild_id, self.bet)
            result = "push"
        else:
            result = "lose"
        await interaction.response.edit_message(embed=self.make_embed(result), view=self)


class Games(commands.Cog):
    """Casino-style games with coin betting."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def new_deck(self):
        cards = [str(i) for i in range(2, 11)] + ["J", "Q", "K", "A"]
        deck = cards * 4
        random.shuffle(deck)
        return deck

    @app_commands.command(name="slots", description="Spin the slot machine!")
    @app_commands.describe(bet="Amount to bet")
    async def slots(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, 50000]):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        # Deduct bet upfront
        await update_wallet(interaction.user.id, interaction.guild.id, -bet)

        reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
        display = (
            f"╔═══════════════════╗\n"
            f"║   {reels[0]}  ┃  {reels[1]}  ┃  {reels[2]}   ║\n"
            f"╚═══════════════════╝"
        )

        if reels[0] == reels[1] == reels[2]:
            mult = SLOT_MULTIPLIERS[reels[0]]
            payout = int(bet * mult)
            await update_wallet(interaction.user.id, interaction.guild.id, payout)
            result = f"🎉 **JACKPOT!** All 3 matched! Won **+{payout:,}** 🪙! (`x{mult}`)"
            color = 0xFEE75C
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            payout = int(bet * 0.5)
            await update_wallet(interaction.user.id, interaction.guild.id, payout)
            result = f"✨ **2 Symbols Matched!** Refunded half your bet: **+{payout:,}** 🪙."
            color = 0x5865F2
        else:
            result = f"💀 **No match!** You lost your **{bet:,}** 🪙 bet."
            color = 0xED4245

        embed = discord.Embed(title="🎰 Lucky Cat Slot Machine", color=color, timestamp=discord.utils.utcnow())
        embed.description = f"```\n{display}\n```\n{result}"
        embed.add_field(name="💰 Bet", value=f"**{bet:,}** 🪙", inline=True)
        embed.set_footer(text="🐾 Meow Casino")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="coinflip", description="Bet on heads or tails.")
    @app_commands.describe(choice="heads or tails", bet="Amount to bet")
    @app_commands.choices(choice=[
        app_commands.Choice(name="Heads", value="heads"),
        app_commands.Choice(name="Tails", value="tails"),
    ])
    async def coinflip(self, interaction: discord.Interaction, choice: str, bet: app_commands.Range[int, 10, 50000]):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        result = random.choice(["heads", "tails"])
        won = result == choice
        await update_wallet(interaction.user.id, interaction.guild.id, bet if won else -bet)

        embed = discord.Embed(
            title="🪙 Coin Flip",
            description=f"The coin landed on **{result.capitalize()}**!\n{'🎉 You **won** ' + str(bet) + ' 🪙!' if won else '💀 You **lost** ' + str(bet) + ' 🪙!'}",
            color=0x2ECC71 if won else 0xFF3333,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Your Pick", value=choice.capitalize(), inline=True)
        embed.add_field(name="Result", value=result.capitalize(), inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="dice", description="Roll dice against the bot. Higher roll wins.")
    @app_commands.describe(bet="Amount to bet")
    async def dice(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, 50000]):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        player_roll = random.randint(1, 6)
        bot_roll = random.randint(1, 6)

        if player_roll > bot_roll:
            await update_wallet(interaction.user.id, interaction.guild.id, bet)
            result = f"🎉 You win **{bet:,}** 🪙!"
            color = 0x2ECC71
        elif player_roll < bot_roll:
            await update_wallet(interaction.user.id, interaction.guild.id, -bet)
            result = f"💀 You lose **{bet:,}** 🪙!"
            color = 0xFF3333
        else:
            result = "🤝 It's a tie! Bet returned."
            color = 0xFFD700

        embed = discord.Embed(title="🎲 Dice Roll", color=color, timestamp=discord.utils.utcnow())
        embed.add_field(name=f"Your Roll", value=f"🎲 **{player_roll}**", inline=True)
        embed.add_field(name="Bot's Roll", value=f"🎲 **{bot_roll}**", inline=True)
        embed.add_field(name="Outcome", value=result, inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="rps", description="Rock Paper Scissors vs the bot (with a bet).")
    @app_commands.describe(choice="Your choice", bet="Amount to bet")
    @app_commands.choices(choice=[
        app_commands.Choice(name="🪨 Rock", value="rock"),
        app_commands.Choice(name="📄 Paper", value="paper"),
        app_commands.Choice(name="✂️ Scissors", value="scissors"),
    ])
    async def rps(self, interaction: discord.Interaction, choice: str, bet: app_commands.Range[int, 10, 50000]):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        bot_choice = random.choice(["rock", "paper", "scissors"])
        emoji = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
        wins = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

        if choice == bot_choice:
            result, color = "🤝 It's a tie! Bet returned.", 0xFFD700
        elif wins[choice] == bot_choice:
            await update_wallet(interaction.user.id, interaction.guild.id, bet)
            result, color = f"🎉 You win **{bet:,}** 🪙!", 0x2ECC71
        else:
            await update_wallet(interaction.user.id, interaction.guild.id, -bet)
            result, color = f"💀 You lose **{bet:,}** 🪙!", 0xFF3333

        embed = discord.Embed(title="✂️ Rock Paper Scissors", color=color, timestamp=discord.utils.utcnow())
        embed.add_field(name="Your Pick", value=f"{emoji[choice]} {choice.capitalize()}", inline=True)
        embed.add_field(name="Bot's Pick", value=f"{emoji[bot_choice]} {bot_choice.capitalize()}", inline=True)
        embed.add_field(name="Outcome", value=result, inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="blackjack", description="Play blackjack against the bot.")
    @app_commands.describe(bet="Amount to bet")
    async def blackjack(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, 50000]):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        # Deduct bet upfront to prevent timeout exploits
        await update_wallet(interaction.user.id, interaction.guild.id, -bet)

        deck = self.new_deck()
        player = [deck.pop(), deck.pop()]
        dealer = [deck.pop(), deck.pop()]
        view = BlackjackView(player, dealer, deck, bet, interaction.user.id, interaction.guild.id)

        pv = view.hand_value(player)
        if pv == 21:
            payout = int(bet * 2.5)
            await update_wallet(interaction.user.id, interaction.guild.id, payout)
            view.finished = True
            view.clear_items()
            return await interaction.response.send_message(embed=view.make_embed("blackjack"), view=view)

        await interaction.response.send_message(embed=view.make_embed(), view=view)

    @app_commands.command(name="trivia", description="Answer a trivia question for coins!")
    async def trivia(self, interaction: discord.Interaction):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        question, answer, options = random.choice(TRIVIA_QUESTIONS)
        shuffled = options.copy()
        random.shuffle(shuffled)
        reward = random.randint(150, 350)

        class TriviaView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=20)
                self.answered = False
                for opt in shuffled:
                    btn = discord.ui.Button(label=opt, style=discord.ButtonStyle.secondary)
                    btn.callback = self.make_callback(opt)
                    self.add_item(btn)

            def make_callback(self, opt):
                async def callback(btn_interaction: discord.Interaction):
                    if btn_interaction.user.id != interaction.user.id:
                        return await btn_interaction.response.send_message("❌ This is not your trivia game!", ephemeral=True)
                    if self.answered:
                        return
                    self.answered = True
                    self.clear_items()
                    if opt.lower() == answer.lower():
                        await update_wallet(interaction.user.id, interaction.guild.id, reward)
                        embed = discord.Embed(
                            title="🧠 Trivia — Correct!",
                            description=f"🎉 **Awesome!** You earned **{reward:,}** 🪙!\n\n> **Question:** {question}\n> **Answer:** **{opt}**",
                            color=0x57F287,
                            timestamp=discord.utils.utcnow()
                        )
                    else:
                        embed = discord.Embed(
                            title="🧠 Trivia — Incorrect",
                            description=f"💀 **Oops, wrong answer!**\n\n> **Question:** {question}\n> **Correct Answer:** **{answer.capitalize()}**",
                            color=0xED4245,
                            timestamp=discord.utils.utcnow()
                        )
                    embed.set_footer(text=f"Played by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
                    await btn_interaction.response.edit_message(embed=embed, view=self)
                return callback

        embed = discord.Embed(
            title="🧠 Trivia Time!",
            description=f"**{question}**\n\nAnswer correctly within **20 seconds** to win **{reward:,}** 🪙!",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="🐾 Meow Trivia")
        await interaction.response.send_message(embed=embed, view=TriviaView())

    @app_commands.command(name="8ball", description="Ask the magic 8ball a question.")
    @app_commands.describe(question="Your question")
    async def eightball(self, interaction: discord.Interaction, question: str):
        responses = [
            ("It is certain! ✅", 0x57F287), ("Without a doubt! ✅", 0x57F287),
            ("Yes, definitely! ✅", 0x57F287), ("Signs point to yes ✅", 0x57F287),
            ("Ask again later 🤔", 0xFEE75C), ("Cannot predict right now 🤔", 0xFEE75C),
            ("Don't count on it ❌", 0xED4245), ("My sources say no ❌", 0xED4245),
            ("Very doubtful ❌", 0xED4245), ("Outlook not so good ❌", 0xED4245),
            ("Meow says yes! 🐾", 0x57F287), ("The cat knocked the ball over... no. 🐾", 0xED4245),
        ]
        answer, color = random.choice(responses)
        embed = discord.Embed(
            title="🎱 Magic 8-Ball",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="❓ Question", value=f"> {question}", inline=False)
        embed.add_field(name="🔮 Answer", value=f"**{answer}**", inline=False)
        embed.set_footer(text=f"Asked by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))
