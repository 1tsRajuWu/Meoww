import discord
from discord import app_commands
from discord.ext import commands
import random
import asyncio
from utils import db
from cogs.economy import get_eco, update_wallet, ensure_eco, settle_bet, take_stake
from utils.style import send_card

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

# /trivia pays out without a stake, so it needs a cooldown or it can be farmed.
TRIVIA_COOLDOWN_SECONDS = 3 * 60

SLOT_SYMBOLS = ["🍒", "🍋", "🍊", "🍇", "💎", "7️⃣", "⭐"]
SLOT_MULTIPLIERS = {"🍒": 2, "🍋": 2.5, "🍊": 3, "🍇": 4, "⭐": 5, "💎": 10, "7️⃣": 20}

ROULETTE_NUMBERS = {
    0: "green",
    1: "red", 2: "black", 3: "red", 4: "black", 5: "red", 6: "black",
    7: "red", 8: "black", 9: "red", 10: "black", 11: "black", 12: "red",
    13: "black", 14: "red", 15: "black", 16: "red", 17: "black", 18: "red",
    19: "red", 20: "black", 21: "red", 22: "black", 23: "red", 24: "black",
    25: "red", 26: "black", 27: "red", 28: "black", 29: "black", 30: "red",
    31: "black", 32: "red", 33: "black", 34: "red", 35: "black", 36: "red"
}

CARD_VALUES = {
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
    "J": 11, "Q": 12, "K": 13, "A": 14
}
SUITS = ["♠️", "♥️", "♦️", "♣️"]


# -----------------------------------------------------------------------------
# 1. Blackjack View
# -----------------------------------------------------------------------------

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
            
        embed.set_footer(text="Meow Casino • 45s turn timer")
        return embed

    async def _finish_hand(self) -> str | None:
        """Settle once. Returns result or None if already finished.

        Shared by the Stand button and the turn timer. Resolving the hand on
        timeout matters because the stake is taken when the game is dealt: the
        timer used to end the game without settling it, so a player who walked
        away silently lost their bet with no message and no result.
        Set ``finished`` *before* any payout await so Stand+timeout cannot
        double-pay.
        """
        if self.finished:
            return None
        self.finished = True
        while self.hand_value(self.dealer_hand) < 17:
            if not self.deck:
                break
            self.dealer_hand.append(self.deck.pop())
        pv = self.hand_value(self.player_hand)
        dv = self.hand_value(self.dealer_hand)
        if dv > 21 or pv > dv:
            # stake already taken: credit 2x stake (return + win)
            await update_wallet(self.user_id, self.guild_id, self.bet * 2)
            return "win"
        if pv == dv:
            await update_wallet(self.user_id, self.guild_id, self.bet)
            return "push"
        return "lose"

    async def on_timeout(self):
        result = await self._finish_hand()
        if result is None:
            return
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
        result = await self._finish_hand()
        if result is None:
            return
        self.clear_items()
        await interaction.response.edit_message(embed=self.make_embed(result), view=self)


# -----------------------------------------------------------------------------
# 2. Mines Casino View
# -----------------------------------------------------------------------------

class MinesTileButton(discord.ui.Button):
    def __init__(self, index: int, row: int):
        super().__init__(style=discord.ButtonStyle.secondary, emoji="⬛", row=row)
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_tile_click(interaction, self.index)


class MinesView(discord.ui.View):
    def __init__(self, user_id: int, guild_id: int, bet: int, num_mines: int):
        super().__init__(timeout=90)
        self.user_id = user_id
        self.guild_id = guild_id
        self.bet = bet
        self.num_mines = num_mines
        self.total_tiles = 16  # 4x4 interactive grid
        self.total_gems = self.total_tiles - num_mines
        self.revealed_gems = 0
        self.current_mult = 1.0
        self.finished = False

        self.mine_indices = set(random.sample(range(self.total_tiles), num_mines))
        self.revealed_indices = set()

        self.tile_buttons = []
        for i in range(self.total_tiles):
            btn = MinesTileButton(i, row=i // 4)
            self.tile_buttons.append(btn)
            self.add_item(btn)

        self.cashout_button = discord.ui.Button(
            label="💰 Cash Out (1.00x • 0 🪙)",
            style=discord.ButtonStyle.success,
            disabled=True,
            row=4
        )
        self.cashout_button.callback = self.handle_cashout
        self.add_item(self.cashout_button)

    def calc_next_multiplier(self, gems_found: int) -> float:
        if gems_found <= 0:
            return 1.0
        mult = 0.96
        for i in range(gems_found):
            mult *= (self.total_tiles - i) / (self.total_tiles - self.num_mines - i)
        return round(mult, 2)

    def make_embed(self, status="playing"):
        colors = {
            "playing": 0x5865F2,
            "won": 0x57F287,
            "lost": 0xED4245,
            "cashed_out": 0xFEE75C
        }
        embed = discord.Embed(
            title=f"💣 Mines Casino — {self.num_mines} Mines",
            color=colors.get(status, 0x5865F2),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="💰 Initial Bet", value=f"**{self.bet:,}** 🪙", inline=True)
        embed.add_field(name="💎 Gems Found", value=f"**{self.revealed_gems}** / {self.total_gems}", inline=True)

        payout = int(self.bet * self.current_mult)
        embed.add_field(name="📈 Multiplier", value=f"**{self.current_mult:.2f}x** (`+{payout:,}` 🪙)", inline=True)

        if status == "playing":
            embed.description = "Click any dark tile `⬛` to uncover gems. Avoid the hidden bombs!"
        elif status == "lost":
            embed.description = f"💥 **BOOM! You detonated a mine and lost your `{self.bet:,}` 🪙 bet!**"
        elif status == "cashed_out":
            embed.description = f"🎉 **CASHED OUT!** You safely secured **+{payout:,}** 🪙 at **{self.current_mult:.2f}x**!"
        elif status == "won":
            embed.description = f"👑 **CLEARED ALL GEMS!** Insane Jackpot! Won **+{payout:,}** 🪙 at **{self.current_mult:.2f}x**!"

        embed.set_footer(text="Meow Mines • 90s game timer")
        return embed

    async def handle_tile_click(self, interaction: discord.Interaction, index: int):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This is not your game!", ephemeral=True)
        if self.finished or index in self.revealed_indices:
            return

        self.revealed_indices.add(index)
        btn = self.tile_buttons[index]

        if index in self.mine_indices:
            self.finished = True
            btn.emoji = "💥"
            btn.style = discord.ButtonStyle.danger

            for i, b in enumerate(self.tile_buttons):
                b.disabled = True
                if i in self.mine_indices:
                    if i != index:
                        b.emoji = "💣"
                        b.style = discord.ButtonStyle.danger
                else:
                    if i in self.revealed_indices:
                        b.emoji = "💎"
                        b.style = discord.ButtonStyle.success
                    else:
                        b.emoji = "✨"
                        b.style = discord.ButtonStyle.secondary

            self.cashout_button.disabled = True
            self.cashout_button.label = "💀 BUSTED!"
            self.cashout_button.style = discord.ButtonStyle.secondary
            await interaction.response.edit_message(embed=self.make_embed("lost"), view=self)
            return

        self.revealed_gems += 1
        btn.emoji = "💎"
        btn.style = discord.ButtonStyle.success
        btn.disabled = True

        self.current_mult = self.calc_next_multiplier(self.revealed_gems)
        payout = int(self.bet * self.current_mult)

        if self.revealed_gems == self.total_gems:
            self.finished = True
            await update_wallet(self.user_id, self.guild_id, payout)
            for i, b in enumerate(self.tile_buttons):
                b.disabled = True
                if i in self.mine_indices:
                    b.emoji = "💣"
            self.cashout_button.disabled = True
            self.cashout_button.label = f"👑 CLEARED! ({self.current_mult:.2f}x)"
            await interaction.response.edit_message(embed=self.make_embed("won"), view=self)
            return

        self.cashout_button.disabled = False
        self.cashout_button.label = f"💰 Cash Out ({self.current_mult:.2f}x • +{payout:,} 🪙)"
        await interaction.response.edit_message(embed=self.make_embed("playing"), view=self)

    async def handle_cashout(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This is not your game!", ephemeral=True)
        if self.finished or self.revealed_gems == 0:
            return

        self.finished = True
        payout = int(self.bet * self.current_mult)
        await update_wallet(self.user_id, self.guild_id, payout)

        for i, b in enumerate(self.tile_buttons):
            b.disabled = True
            if i in self.mine_indices:
                b.emoji = "💣"
            elif i not in self.revealed_indices:
                b.emoji = "✨"

        self.cashout_button.disabled = True
        self.cashout_button.label = f"✅ Cashed Out: +{payout:,} 🪙"
        await interaction.response.edit_message(embed=self.make_embed("cashed_out"), view=self)

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        if self.revealed_gems > 0:
            payout = int(self.bet * self.current_mult)
            await update_wallet(self.user_id, self.guild_id, payout)
        else:
            await update_wallet(self.user_id, self.guild_id, self.bet)


# -----------------------------------------------------------------------------
# 3. High-Low Card Ladder View
# -----------------------------------------------------------------------------

class HighLowView(discord.ui.View):
    def __init__(self, user_id: int, guild_id: int, bet: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.guild_id = guild_id
        self.bet = bet
        self.streak = 0
        self.current_mult = 1.0
        self.finished = False

        self.deck = [(rank, suit) for rank in CARD_VALUES.keys() for suit in SUITS]
        random.shuffle(self.deck)
        self.current_card = self.deck.pop()

    def calc_multiplier_gain(self, choice: str) -> float:
        val = CARD_VALUES[self.current_card[0]]
        if choice == "higher":
            higher_cnt = max(1, 14 - val)
            return round(1.0 + (13 - higher_cnt) * 0.15 + 0.15, 2)
        elif choice == "lower":
            lower_cnt = max(1, val - 2)
            return round(1.0 + (13 - lower_cnt) * 0.15 + 0.15, 2)
        else:
            return 8.0

    def make_embed(self, status="playing", next_card=None):
        rank, suit = self.current_card
        payout = int(self.bet * self.current_mult)
        colors = {"playing": 0x5865F2, "won": 0x57F287, "lost": 0xED4245, "cashed_out": 0xFEE75C}

        embed = discord.Embed(
            title="🃏 High-Low Card Ladder",
            color=colors.get(status, 0x5865F2),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="💰 Bet", value=f"**{self.bet:,}** 🪙", inline=True)
        embed.add_field(name="🔥 Streak", value=f"**{self.streak}** / 5", inline=True)
        embed.add_field(name="📈 Multiplier", value=f"**{self.current_mult:.2f}x** (`+{payout:,}` 🪙)", inline=True)

        card_display = f"```\n┌─────────┐\n│ {rank:<2}      │\n│         │\n│    {suit}    │\n│         │\n│       {rank:>2}│\n└─────────┘\n```"
        embed.description = f"**Current Card:**\n{card_display}\n"

        if status == "playing":
            embed.description += "Will the next card be **Higher**, **Lower**, or **Equal**?"
        elif status == "lost":
            nr, ns = next_card
            embed.description += f"💀 **BUSTED! Next card was `{nr} {ns}`! You lost your `{self.bet:,}` 🪙 bet.**"
        elif status == "cashed_out":
            embed.description += f"🎉 **CASHED OUT! You took home `+{payout:,}` 🪙 at `{self.current_mult:.2f}x`!**"
        elif status == "won":
            embed.description += f"👑 **MAX STREAK (5/5)! JACKPOT! You won `+{payout:,}` 🪙 at `{self.current_mult:.2f}x`!**"

        embed.set_footer(text="Meow High-Low • 60s turn timer")
        return embed

    async def handle_guess(self, interaction: discord.Interaction, choice: str):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This is not your game!", ephemeral=True)
        if self.finished:
            return

        if not self.deck:
            self.deck = [(rank, suit) for rank in CARD_VALUES.keys() for suit in SUITS]
            random.shuffle(self.deck)

        next_card = self.deck.pop()
        cur_val = CARD_VALUES[self.current_card[0]]
        next_val = CARD_VALUES[next_card[0]]

        won = False
        if choice == "higher" and next_val > cur_val:
            won = True
        elif choice == "lower" and next_val < cur_val:
            won = True
        elif choice == "equal" and next_val == cur_val:
            won = True

        if not won:
            self.finished = True
            self.clear_items()
            return await interaction.response.edit_message(embed=self.make_embed("lost", next_card), view=self)

        self.streak += 1
        gain = self.calc_multiplier_gain(choice)
        self.current_mult = round(self.current_mult * gain, 2)
        self.current_card = next_card

        if self.streak >= 5:
            self.finished = True
            self.clear_items()
            payout = int(self.bet * self.current_mult)
            await update_wallet(self.user_id, self.guild_id, payout)
            return await interaction.response.edit_message(embed=self.make_embed("won"), view=self)

        payout = int(self.bet * self.current_mult)
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == "cashout":
                item.disabled = False
                item.label = f"💰 Cash Out ({self.current_mult:.2f}x • +{payout:,} 🪙)"

        await interaction.response.edit_message(embed=self.make_embed("playing"), view=self)

    @discord.ui.button(label="⬆️ Higher", style=discord.ButtonStyle.primary, row=0)
    async def higher_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_guess(interaction, "higher")

    @discord.ui.button(label="⬇️ Lower", style=discord.ButtonStyle.primary, row=0)
    async def lower_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_guess(interaction, "lower")

    @discord.ui.button(label="🎯 Equal (8x)", style=discord.ButtonStyle.secondary, row=0)
    async def equal_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_guess(interaction, "equal")

    @discord.ui.button(label="💰 Cash Out (1.00x)", style=discord.ButtonStyle.success, disabled=True, row=1, custom_id="cashout")
    async def cashout_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This is not your game!", ephemeral=True)
        if self.finished or self.streak == 0:
            return
        self.finished = True
        self.clear_items()
        payout = int(self.bet * self.current_mult)
        await update_wallet(self.user_id, self.guild_id, payout)
        await interaction.response.edit_message(embed=self.make_embed("cashed_out"), view=self)

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        if self.streak > 0:
            payout = int(self.bet * self.current_mult)
            await update_wallet(self.user_id, self.guild_id, payout)
        else:
            await update_wallet(self.user_id, self.guild_id, self.bet)


# -----------------------------------------------------------------------------
# 4. Tic-Tac-Toe Showdown View
# -----------------------------------------------------------------------------

class TicTacToeButton(discord.ui.Button):
    def __init__(self, x: int, y: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=y)
        self.x = x
        self.y = y

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_move(interaction, self.x, self.y)


class TicTacToeInviteView(discord.ui.View):
    def __init__(self, p1: discord.Member, p2: discord.Member, guild_id: int, bet: int):
        super().__init__(timeout=45)
        self.p1 = p1
        self.p2 = p2
        self.guild_id = guild_id
        self.bet = bet
        self.responded = False

    def _take_both_stakes(self, cur):
        """Take both wagers inside one holding transaction (see the caller)."""
        cur.execute(
            "SELECT COUNT(*) FROM economy WHERE guild_id=? AND user_id IN (?, ?) AND wallet >= ?",
            (self.guild_id, self.p1.id, self.p2.id, self.bet),
        )
        if cur.fetchone()[0] != 2:
            return False
        cur.execute(
            "UPDATE economy SET wallet = wallet - ? WHERE guild_id=? AND user_id IN (?, ?)",
            (self.bet, self.guild_id, self.p1.id, self.p2.id),
        )
        if cur.rowcount != 2:
            raise RuntimeError("stake update touched an unexpected number of rows")
        return True

    @discord.ui.button(label="⚔️ Accept Challenge", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.p2.id:
            return await interaction.response.send_message("❌ Only the challenged opponent can accept this!", ephemeral=True)
        if self.responded:
            return
        self.responded = True

        if self.bet > 0:
            await ensure_eco(self.p1.id, self.guild_id)
            await ensure_eco(self.p2.id, self.guild_id)
            # Both stakes move in one transaction. Debiting them one after the
            # other meant a failing second debit (a wallet that moved in between)
            # still started the match, with only one player having paid into the pot.
            both_paid = await asyncio.to_thread(db.run_atomic, self._take_both_stakes)
            if not both_paid:
                return await interaction.response.send_message(
                    f"❌ One of the wallets no longer covers the **{self.bet:,}** 🪙 bet. Please send the challenge again.",
                    ephemeral=True,
                )

        game_view = TicTacToeView(self.p1, self.p2, self.guild_id, self.bet)
        embed = game_view.make_embed()
        await interaction.response.edit_message(embed=embed, view=game_view)

    @discord.ui.button(label="🏳️ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.p2.id and interaction.user.id != self.p1.id:
            return await interaction.response.send_message("❌ This is not your challenge!", ephemeral=True)
        self.responded = True
        self.clear_items()
        embed = discord.Embed(
            title="⚔️ Tic-Tac-Toe Challenge Declined",
            description=f"{self.p2.mention} declined the match against {self.p1.mention}.",
            color=0xED4245
        )
        await interaction.response.edit_message(embed=embed, view=self)


class TicTacToeView(discord.ui.View):
    def __init__(self, p1: discord.Member, p2: discord.Member | None, guild_id: int, bet: int):
        super().__init__(timeout=60)
        self.p1 = p1
        self.p2 = p2
        self.guild_id = guild_id
        self.bet = bet
        self.current_turn = p1
        self.board = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
        self.finished = False
        self.buttons: dict[tuple[int, int], TicTacToeButton] = {}

        for y in range(3):
            for x in range(3):
                btn = TicTacToeButton(x, y)
                self.buttons[(x, y)] = btn
                self.add_item(btn)

    def check_winner(self) -> int:
        b = self.board
        for r in range(3):
            if b[r][0] == b[r][1] == b[r][2] != 0:
                return b[r][0]
        for c in range(3):
            if b[0][c] == b[1][c] == b[2][c] != 0:
                return b[0][c]
        if b[0][0] == b[1][1] == b[2][2] != 0:
            return b[0][0]
        if b[0][2] == b[1][1] == b[2][0] != 0:
            return b[0][2]
        return 0

    def is_full(self) -> bool:
        return all(self.board[y][x] != 0 for y in range(3) for x in range(3))

    def make_embed(self, status="turn"):
        colors = {"turn": 0x5865F2, "p1": 0x57F287, "p2": 0xED4245, "tie": 0xFEE75C}
        color = colors.get(status, 0x5865F2)

        opponent_name = self.p2.display_name if self.p2 else "🤖 Bot AI"
        embed = discord.Embed(
            title="⭕ Tic-Tac-Toe Showdown ❌",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="❌ Player 1", value=self.p1.mention, inline=True)
        embed.add_field(name="⭕ Player 2", value=self.p2.mention if self.p2 else "🤖 Bot AI", inline=True)
        if self.bet > 0:
            embed.add_field(name="💰 Prize Pool", value=f"**{self.bet * 2:,}** 🪙", inline=True)

        if status == "turn":
            embed.description = f"👉 Current Turn: **{self.current_turn.mention if self.current_turn else opponent_name}**"
        elif status == "p1":
            prize_str = f" and claimed the **{self.bet * 2:,}** 🪙 pot!" if self.bet > 0 else "!"
            embed.description = f"🎉 **{self.p1.mention} (❌) won the match{prize_str}**"
        elif status == "p2":
            p2_name = self.p2.mention if self.p2 else "🤖 Bot AI"
            prize_str = f" and claimed the **{self.bet * 2:,}** 🪙 pot!" if self.bet > 0 and self.p2 else "!"
            embed.description = f"🎉 **{p2_name} (⭕) won the match{prize_str}**"
        elif status == "tie":
            embed.description = "🤝 **It's a draw! Both players matched evenly.**" + (" Bets refunded." if self.bet > 0 else "")

        embed.set_footer(text="Meow Tic-Tac-Toe • 60s turn timer")
        return embed

    def bot_find_move(self) -> tuple[int, int]:
        b = self.board
        for y in range(3):
            for x in range(3):
                if b[y][x] == 0:
                    b[y][x] = 2
                    if self.check_winner() == 2:
                        b[y][x] = 0
                        return (x, y)
                    b[y][x] = 0
        for y in range(3):
            for x in range(3):
                if b[y][x] == 0:
                    b[y][x] = 1
                    if self.check_winner() == 1:
                        b[y][x] = 0
                        return (x, y)
                    b[y][x] = 0
        if b[1][1] == 0:
            return (1, 1)
        corners = [(0, 0), (0, 2), (2, 0), (2, 2)]
        avail_corners = [c for c in corners if b[c[1]][c[0]] == 0]
        if avail_corners:
            return random.choice(avail_corners)
        edges = [(0, 1), (1, 0), (1, 2), (2, 1)]
        avail_edges = [e for e in edges if b[e[1]][e[0]] == 0]
        if avail_edges:
            return random.choice(avail_edges)
        return (0, 0)

    async def handle_move(self, interaction: discord.Interaction, x: int, y: int):
        if self.finished:
            return
        if self.current_turn is not None and interaction.user.id != self.current_turn.id:
            return await interaction.response.send_message("❌ It is not your turn!", ephemeral=True)
        if self.board[y][x] != 0:
            return await interaction.response.send_message("❌ That spot is already taken!", ephemeral=True)

        is_p1 = (interaction.user.id == self.p1.id)
        mark_val = 1 if is_p1 else 2
        mark_sym = "❌" if is_p1 else "⭕"
        mark_style = discord.ButtonStyle.primary if is_p1 else discord.ButtonStyle.danger

        self.board[y][x] = mark_val
        btn = self.buttons[(x, y)]
        btn.label = mark_sym
        btn.style = mark_style
        btn.disabled = True

        winner = self.check_winner()
        if winner != 0:
            self.finished = True
            for b in self.buttons.values():
                b.disabled = True
            if winner == 1:
                if self.bet > 0:
                    await update_wallet(self.p1.id, self.guild_id, self.bet * 2)
                return await interaction.response.edit_message(embed=self.make_embed("p1"), view=self)
            else:
                if self.bet > 0 and self.p2:
                    await update_wallet(self.p2.id, self.guild_id, self.bet * 2)
                return await interaction.response.edit_message(embed=self.make_embed("p2"), view=self)

        if self.is_full():
            self.finished = True
            for b in self.buttons.values():
                b.disabled = True
            if self.bet > 0:
                await update_wallet(self.p1.id, self.guild_id, self.bet)
                if self.p2:
                    await update_wallet(self.p2.id, self.guild_id, self.bet)
            return await interaction.response.edit_message(embed=self.make_embed("tie"), view=self)

        if self.p2 is not None:
            self.current_turn = self.p2 if is_p1 else self.p1
            await interaction.response.edit_message(embed=self.make_embed("turn"), view=self)
        else:
            self.current_turn = None
            bx, by = self.bot_find_move()
            self.board[by][bx] = 2
            b_btn = self.buttons[(bx, by)]
            b_btn.label = "⭕"
            b_btn.style = discord.ButtonStyle.danger
            b_btn.disabled = True

            b_winner = self.check_winner()
            if b_winner == 2:
                self.finished = True
                for b in self.buttons.values():
                    b.disabled = True
                return await interaction.response.edit_message(embed=self.make_embed("p2"), view=self)

            if self.is_full():
                self.finished = True
                for b in self.buttons.values():
                    b.disabled = True
                if self.bet > 0:
                    await update_wallet(self.p1.id, self.guild_id, self.bet)
                return await interaction.response.edit_message(embed=self.make_embed("tie"), view=self)

            self.current_turn = self.p1
            await interaction.response.edit_message(embed=self.make_embed("turn"), view=self)

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        for b in self.buttons.values():
            b.disabled = True
        if self.bet > 0:
            await update_wallet(self.p1.id, self.guild_id, self.bet)
            if self.p2:
                await update_wallet(self.p2.id, self.guild_id, self.bet)


# -----------------------------------------------------------------------------
# Games Cog Class
# -----------------------------------------------------------------------------

class Games(commands.Cog):
    """Casino-style and interactive games with coin betting."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            cmd_name = interaction.command.name if interaction.command else None
            if cmd_name in ["slots", "coinflip", "dice", "blackjack", "mines", "roulette", "highlow", "gamble"]:
                for mod in ["economy", "games"]:
                    if not self.bot.is_module_enabled(interaction.guild_id, mod):
                        await interaction.response.send_message(
                            f"⚠️ The **{mod.title()}** system is currently **turned off** on this server.\n"
                            "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                            ephemeral=True
                        )
                        return False
            elif cmd_name == "8ball":
                if not self.bot.is_module_enabled(interaction.guild_id, "fun"):
                    await interaction.response.send_message(
                        "⚠️ The **Fun** system is currently **turned off** on this server.\n"
                        "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                        ephemeral=True
                    )
                    return False
            else:
                if not self.bot.is_module_enabled(interaction.guild_id, "games"):
                    await interaction.response.send_message(
                        "⚠️ The **Mini-Games Hub** is currently **turned off** on this server.\n"
                        "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                        ephemeral=True
                    )
                    return False
        return True

    def new_deck(self):
        cards = [str(i) for i in range(2, 11)] + ["J", "Q", "K", "A"]
        deck = cards * 4
        random.shuffle(deck)
        return deck

    # -------------------------------------------------------------------------
    # Classic Games
    # -------------------------------------------------------------------------

    @app_commands.command(name="slots", description="Spin the slot machine!")
    @app_commands.describe(bet="Amount to bet")
    async def slots(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, None]):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
        display = (
            f"╔═══════════════════╗\n"
            f"║   {reels[0]}  ┃  {reels[1]}  ┃  {reels[2]}   ║\n"
            f"╚═══════════════════╝"
        )

        if reels[0] == reels[1] == reels[2]:
            mult = SLOT_MULTIPLIERS[reels[0]]
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            mult = 0.5
        else:
            mult = 0.0

        # The stake is taken and the payout credited in one transaction, so the
        # coins wagered here cannot be wagered again by a second invocation.
        if not await settle_bet(interaction.user.id, interaction.guild.id, bet, mult):
            return await interaction.response.send_message(
                "❌ Your wallet changed while that was spinning — please try again.", ephemeral=True
            )

        payout = int(bet * mult)
        net = payout - bet
        new_wallet = eco["wallet"] + net
        if mult >= 2:
            result = f"🎉 **JACKPOT!** All 3 matched! Won **+{payout:,}** 🪙! (`x{mult}`)"
            color = 0xFEE75C
            outcome_field = ("💰 Profit", f"**+{net:,}** 🪙")
        elif mult > 0:
            result = f"✨ **2 Symbols Matched!** Refunded half your bet: **+{payout:,}** 🪙."
            color = 0x5865F2
            outcome_field = ("💸 Partial Loss", f"**{net:,}** 🪙")
        else:
            result = f"💀 **No match!** You lost your **{bet:,}** 🪙 bet."
            color = 0xED4245
            outcome_field = ("💸 Lost", f"**-{bet:,}** 🪙")

        embed = discord.Embed(title="🎰 Lucky Cat Slot Machine", color=color, timestamp=discord.utils.utcnow())
        embed.description = f"```\n{display}\n```\n{result}"
        embed.add_field(name="💰 Bet", value=f"**{bet:,}** 🪙", inline=True)
        embed.add_field(name=outcome_field[0], value=outcome_field[1], inline=True)
        embed.add_field(name="💳 New Wallet", value=f"**{new_wallet:,}** 🪙", inline=True)
        embed.set_footer(text="Meow Casino")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="coinflip", description="Bet on heads or tails.")
    @app_commands.describe(choice="heads or tails", bet="Amount to bet")
    @app_commands.choices(choice=[
        app_commands.Choice(name="Heads", value="heads"),
        app_commands.Choice(name="Tails", value="tails"),
    ])
    async def coinflip(self, interaction: discord.Interaction, choice: str, bet: app_commands.Range[int, 1, None]):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        result = random.choice(["heads", "tails"])
        won = result == choice
        if not await settle_bet(interaction.user.id, interaction.guild.id, bet, 2.0 if won else 0.0):
            return await interaction.response.send_message(
                "❌ Your wallet changed while that was flipping — please try again.", ephemeral=True
            )
        change = bet if won else -bet
        new_wallet = eco["wallet"] + change

        embed = discord.Embed(
            title=f"🪙 Coin Flip — {'YOU WON! 🎉' if won else 'YOU LOST! 💀'}",
            description=(
                f"The coin landed on **{result.capitalize()}**!\n\n"
                f"• **Your Pick:** `{choice.capitalize()}`\n"
                f"• **Result:** `{result.capitalize()}`\n\n"
                f"{'💰 **Profit:** `+' + f'{bet:,}' + '` 🪙' if won else '💸 **Lost:** `-' + f'{bet:,}' + '` 🪙'}\n"
                f"💳 **New Wallet:** `{new_wallet:,}` 🪙"
            ),
            color=0x23a55a if won else 0xf23f43,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="Meow Casino • High Stakes" if won else "Meow Casino • Better luck next flip!")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="dice", description="Roll dice against the bot. Higher roll wins.")
    @app_commands.describe(bet="Amount to bet")
    async def dice(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, None]):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        player_roll = random.randint(1, 6)
        bot_roll = random.randint(1, 6)

        mult = 2.0 if player_roll > bot_roll else (0.0 if player_roll < bot_roll else 1.0)
        if not await settle_bet(interaction.user.id, interaction.guild.id, bet, mult):
            return await interaction.response.send_message(
                "❌ Your wallet changed while the dice were rolling — please try again.", ephemeral=True
            )

        if mult == 2.0:
            new_wallet = eco["wallet"] + bet
            result = f"🎉 **You win!**\n💰 **Profit:** `+{bet:,}` 🪙\n💳 **New Wallet:** `{new_wallet:,}` 🪙"
            color = 0x23a55a
        elif mult == 0.0:
            new_wallet = eco["wallet"] - bet
            result = f"💀 **You lose!**\n💸 **Lost:** `-{bet:,}` 🪙\n💳 **New Wallet:** `{new_wallet:,}` 🪙"
            color = 0xf23f43
        else:
            result = f"🤝 **It's a tie!** Bet returned.\n💳 **Wallet:** `{eco['wallet']:,}` 🪙"
            color = 0xFEE75C

        embed = discord.Embed(title="🎲 Dice Roll Challenge", color=color, timestamp=discord.utils.utcnow())
        embed.add_field(name="Your Roll", value=f"🎲 **{player_roll}**", inline=True)
        embed.add_field(name="Bot's Roll", value=f"🎲 **{bot_roll}**", inline=True)
        embed.add_field(name="Outcome", value=result, inline=False)
        await send_card(interaction, embed=embed)

    @app_commands.command(name="rps", description="Rock Paper Scissors vs the bot (with a bet).")
    @app_commands.describe(choice="Your choice", bet="Amount to bet")
    @app_commands.choices(choice=[
        app_commands.Choice(name="🪨 Rock", value="rock"),
        app_commands.Choice(name="📄 Paper", value="paper"),
        app_commands.Choice(name="✂️ Scissors", value="scissors"),
    ])
    async def rps(self, interaction: discord.Interaction, choice: str, bet: app_commands.Range[int, 1, None]):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        bot_choice = random.choice(["rock", "paper", "scissors"])
        emoji = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
        wins = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

        mult = 1.0 if choice == bot_choice else (2.0 if wins[choice] == bot_choice else 0.0)
        if not await settle_bet(interaction.user.id, interaction.guild.id, bet, mult):
            return await interaction.response.send_message(
                "❌ Your wallet changed while that was playing — please try again.", ephemeral=True
            )

        if mult == 1.0:
            result, color = f"🤝 **It's a tie!** Bet returned.\n💳 **Wallet:** `{eco['wallet']:,}` 🪙", 0xFEE75C
        elif mult == 2.0:
            new_wallet = eco["wallet"] + bet
            result, color = f"🎉 **You win!**\n💰 **Profit:** `+{bet:,}` 🪙\n💳 **New Wallet:** `{new_wallet:,}` 🪙", 0x23a55a
        else:
            new_wallet = eco["wallet"] - bet
            result, color = f"💀 **You lose!**\n💸 **Lost:** `-{bet:,}` 🪙\n💳 **New Wallet:** `{new_wallet:,}` 🪙", 0xf23f43

        embed = discord.Embed(title="✂️ Rock Paper Scissors", color=color, timestamp=discord.utils.utcnow())
        embed.add_field(name="Your Pick", value=f"{emoji[choice]} {choice.capitalize()}", inline=True)
        embed.add_field(name="Bot's Pick", value=f"{emoji[bot_choice]} {bot_choice.capitalize()}", inline=True)
        embed.add_field(name="Outcome", value=result, inline=False)
        await send_card(interaction, embed=embed)

    @app_commands.command(name="blackjack", description="Play blackjack against the bot.")
    @app_commands.describe(bet="Amount to bet")
    async def blackjack(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, None]):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        # Hold the stake up front, and refuse when the wallet no longer covers it:
        # a plain debit clamps at zero, so a second game fired at the same instant
        # could start without being paid for.
        if not await take_stake(interaction.user.id, interaction.guild.id, bet):
            return await interaction.response.send_message(
                "❌ Your wallet changed while dealing — please try again.", ephemeral=True
            )

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
    @app_commands.checks.cooldown(1, TRIVIA_COOLDOWN_SECONDS, key=lambda i: (i.guild_id, i.user.id))
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
        embed.set_footer(text="Meow Trivia")
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
        await send_card(interaction, embed=embed)

    # -------------------------------------------------------------------------
    # NEW ADVANCE GAMES
    # -------------------------------------------------------------------------

    @app_commands.command(name="mines", description="Play Mines casino! Uncover gems, dodge bombs, and cash out!")
    @app_commands.describe(
        bet="Amount to bet",
        mines="Number of hidden mines (1 to 8, default 3)"
    )
    async def mines(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, 1, None],
        mines: app_commands.Range[int, 1, 8] = 3
    ):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        if not await take_stake(interaction.user.id, interaction.guild.id, bet):
            return await interaction.response.send_message(
                "❌ Your wallet changed before the game started — please try again.", ephemeral=True
            )

        view = MinesView(interaction.user.id, interaction.guild.id, bet, mines)
        await interaction.response.send_message(embed=view.make_embed(), view=view)

    @app_commands.command(name="roulette", description="Play European Roulette! Bet on colors, even/odd, dozens, or single numbers.")
    @app_commands.describe(
        bet="Amount to bet",
        bet_on="What to bet on",
        number="Single number (0-36) if betting on 'Single Number (36x)'"
    )
    @app_commands.choices(bet_on=[
        app_commands.Choice(name="🔴 Red (2x)", value="red"),
        app_commands.Choice(name="⚫ Black (2x)", value="black"),
        app_commands.Choice(name="⚖️ Even (2x)", value="even"),
        app_commands.Choice(name="⚡ Odd (2x)", value="odd"),
        app_commands.Choice(name="📉 Low 1-18 (2x)", value="low"),
        app_commands.Choice(name="📈 High 19-36 (2x)", value="high"),
        app_commands.Choice(name="1️⃣ 1st Dozen 1-12 (3x)", value="dozen1"),
        app_commands.Choice(name="2️⃣ 2nd Dozen 13-24 (3x)", value="dozen2"),
        app_commands.Choice(name="3️⃣ 3rd Dozen 25-36 (3x)", value="dozen3"),
        app_commands.Choice(name="🎯 Single Number (36x JACKPOT!)", value="number"),
    ])
    async def roulette(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, 1, None],
        bet_on: str,
        number: app_commands.Range[int, 0, 36] = None
    ):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        if bet_on == "number" and number is None:
            return await interaction.response.send_message("❌ Please specify a `number` between 0 and 36 when betting on Single Number!", ephemeral=True)

        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        landed_num = random.randint(0, 36)
        landed_color = ROULETTE_NUMBERS[landed_num]
        color_emoji = "🟢" if landed_color == "green" else ("🔴" if landed_color == "red" else "⚫")

        won = False
        mult = 0
        if bet_on == "red" and landed_color == "red":
            won = True
            mult = 2
        elif bet_on == "black" and landed_color == "black":
            won = True
            mult = 2
        elif bet_on == "even" and landed_num > 0 and landed_num % 2 == 0:
            won = True
            mult = 2
        elif bet_on == "odd" and landed_num % 2 == 1:
            won = True
            mult = 2
        elif bet_on == "low" and 1 <= landed_num <= 18:
            won = True
            mult = 2
        elif bet_on == "high" and 19 <= landed_num <= 36:
            won = True
            mult = 2
        elif bet_on == "dozen1" and 1 <= landed_num <= 12:
            won = True
            mult = 3
        elif bet_on == "dozen2" and 13 <= landed_num <= 24:
            won = True
            mult = 3
        elif bet_on == "dozen3" and 25 <= landed_num <= 36:
            won = True
            mult = 3
        elif bet_on == "number" and landed_num == number:
            won = True
            mult = 36

        wheel_strip = [random.randint(0, 36) for _ in range(2)] + [landed_num] + [random.randint(0, 36) for _ in range(2)]
        strip_display = "  ".join(
            f"▶ **[{ROULETTE_NUMBERS[n].upper()[:1]} {n}]** ◀" if n == landed_num else f"`[{ROULETTE_NUMBERS[n].upper()[:1]} {n}]`"
            for n in wheel_strip
        )

        payout = bet * mult
        if not await settle_bet(interaction.user.id, interaction.guild.id, bet, float(mult)):
            return await interaction.response.send_message(
                "❌ Your wallet changed while the wheel was spinning — please try again.", ephemeral=True
            )
        if won:
            net = payout - bet
            new_wallet = eco["wallet"] + net
            res_text = f"🎉 **YOU WON!** Multiplier: **{mult}x** ➔ **+{net:,}** 🪙 profit!"
            color = 0x57F287
            outcome_field = ("💰 Profit", f"**+{net:,}** 🪙")
        else:
            net = -bet
            new_wallet = eco["wallet"] + net
            res_text = f"💀 **YOU LOST!** The ball landed on {color_emoji} **{landed_num}**."
            color = 0xED4245
            outcome_field = ("💸 Lost", f"**-{bet:,}** 🪙")

        embed = discord.Embed(
            title="🎡 European Roulette Wheel",
            description=(
                f"```\n{strip_display}\n```\n\n"
                f"🎯 **Result:** The ball stopped on **{color_emoji} {landed_num} ({landed_color.upper()})**!\n\n"
                f"{res_text}"
            ),
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="💰 Bet", value=f"**{bet:,}** 🪙", inline=True)
        embed.add_field(name="🎯 Bet Choice", value=f"**{bet_on.upper()}" + (f" ({number})" if number is not None else "") + "**", inline=True)
        embed.add_field(name=outcome_field[0], value=outcome_field[1], inline=True)
        embed.add_field(name="💳 New Wallet", value=f"**{new_wallet:,}** 🪙", inline=True)
        embed.set_footer(text=f"Played by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await send_card(interaction, embed=embed)

    @app_commands.command(name="highlow", description="Climb the Card Ladder! Guess if the next card is Higher, Lower, or Equal.")
    @app_commands.describe(bet="Amount to bet")
    async def highlow(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, 1, None]
    ):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙.", ephemeral=True)

        if not await take_stake(interaction.user.id, interaction.guild.id, bet):
            return await interaction.response.send_message(
                "❌ Your wallet changed before the game started — please try again.", ephemeral=True
            )

        view = HighLowView(interaction.user.id, interaction.guild.id, bet)
        await interaction.response.send_message(embed=view.make_embed(), view=view)

    @app_commands.command(name="tictactoe", description="Play Tic-Tac-Toe against a friend or vs Bot AI with coin stakes!")
    @app_commands.describe(
        opponent="Member to challenge (leave empty to play vs Bot AI)",
        bet="Coins to wager on the match (default 0)"
    )
    async def tictactoe(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member = None,
        bet: app_commands.Range[int, 0, None] = 0
    ):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        if opponent and opponent.id == interaction.user.id:
            return await interaction.response.send_message("❌ You cannot challenge yourself!", ephemeral=True)

        if opponent and opponent.bot:
            return await interaction.response.send_message("❌ To play vs Bot AI, leave the `opponent` field empty!", ephemeral=True)

        if bet > 0:
            await ensure_eco(interaction.user.id, interaction.guild.id)
            eco1 = await get_eco(interaction.user.id, interaction.guild.id)
            if eco1["wallet"] < bet:
                return await interaction.response.send_message(f"❌ You only have **{eco1['wallet']:,}** 🪙 for the bet!", ephemeral=True)

        if opponent is not None:
            if bet > 0:
                await ensure_eco(opponent.id, interaction.guild.id)
                eco2 = await get_eco(opponent.id, interaction.guild.id)
                if eco2["wallet"] < bet:
                    return await interaction.response.send_message(f"❌ {opponent.mention} only has **{eco2['wallet']:,}** 🪙 and cannot match your bet!", ephemeral=True)

            invite_view = TicTacToeInviteView(interaction.user, opponent, interaction.guild.id, bet)
            bet_str = f" with a **{bet:,}** 🪙 wager on the line" if bet > 0 else ""
            embed = discord.Embed(
                title="⚔️ Tic-Tac-Toe Challenge!",
                description=(
                    f"### {interaction.user.mention} challenged {opponent.mention} to a Tic-Tac-Toe match{bet_str}!\n\n"
                    f"{opponent.mention}, do you accept this duel?"
                ),
                color=0x5865F2,
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text="Match invitation expires in 45s")
            return await interaction.response.send_message(embed=embed, view=invite_view)

        if bet > 0 and not await take_stake(interaction.user.id, interaction.guild.id, bet):
            return await interaction.response.send_message(
                "❌ Your wallet changed before the match started — please try again.", ephemeral=True
            )

        game_view = TicTacToeView(interaction.user, None, interaction.guild.id, bet)
        embed = game_view.make_embed()
        await interaction.response.send_message(embed=embed, view=game_view)

    @app_commands.command(name="gamble", description="Roll the high-stakes 50/50 dice to double your coins!")
    @app_commands.describe(bet="Coins to wager on the roll")
    async def gamble(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, None]):
        if not interaction.guild:
            return await interaction.response.send_message("❌ This command can only be used in a server!", ephemeral=True)

        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙 in your wallet.", ephemeral=True)

        user_roll = random.randint(1, 100)
        bot_roll = random.randint(1, 100)

        mult = 2.0 if user_roll > bot_roll else (0.0 if user_roll < bot_roll else 1.0)
        if not await settle_bet(interaction.user.id, interaction.guild.id, bet, mult):
            return await interaction.response.send_message(
                "❌ Your wallet changed while the dice were rolling — please try again.", ephemeral=True
            )

        if mult == 2.0:
            winnings = bet
            new_bal = eco["wallet"] + winnings
            embed = discord.Embed(
                title="🎲 High-Stakes Gamble — YOU WON! 🎉",
                description=(
                    f"**Your Roll:** 🎲 `{user_roll}`\n"
                    f"**Bot's Roll:** 🎲 `{bot_roll}`\n\n"
                    f"💰 **Profit:** `+{winnings:,}` 🪙\n"
                    f"💳 **New Wallet:** `{new_bal:,}` 🪙"
                ),
                color=0x23a55a
            )
        elif mult == 0.0:
            new_bal = eco["wallet"] - bet
            embed = discord.Embed(
                title="🎲 High-Stakes Gamble — YOU LOST! 💀",
                description=(
                    f"**Your Roll:** 🎲 `{user_roll}`\n"
                    f"**Bot's Roll:** 🎲 `{bot_roll}`\n\n"
                    f"💸 **Lost:** `-{bet:,}` 🪙\n"
                    f"💳 **New Wallet:** `{new_bal:,}` 🪙"
                ),
                color=0xf23f43
            )
        else:
            embed = discord.Embed(
                title="🎲 High-Stakes Gamble — TIE! 🤝",
                description=(
                    f"Both rolled 🎲 `{user_roll}`!\n"
                    f"Your **{bet:,}** 🪙 bet was refunded."
                ),
                color=0xfee75c
            )

        embed.set_footer(text=f"Gambler: {interaction.user.display_name}")
        await send_card(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))
