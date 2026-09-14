"""`/market` — a real market, priced by real exchanges.

One top-level command with subcommands, because Discord caps an application at
100 global commands and a market with a command per action would have spent six
of the eight remaining slots.

The commands here are kept thin on purpose: every number is decided in
``utils.market`` and every trade is settled inside one database transaction
there, so what a member sees on a card and what actually happened to their
wallet cannot drift apart.
"""
from __future__ import annotations

import os
import time

import discord
from discord import app_commands
from discord.ext import commands

from cogs.economy import ensure_eco, get_eco
from utils import chart, market
from utils.style import send_card

#: How many trades `/market history` shows.
HISTORY_LIMIT = 10

#: Discord refuses an embed whose field value exceeds 1,024 characters, and a
#: board of eighty instruments is far past that — so the board is paginated into
#: as many fields as it needs rather than being cut off mid-row.
FIELD_LIMIT = 1024

#: Where the live chart page is served. The same host as the dashboard, because
#: that is the process that already has the market in memory.
DASHBOARD_BASE = os.getenv(
    "DASHBOARD_BASE_URL", "https://meow-discord-bot.onrender.com"
).rstrip("/")


def live_chart_url(symbol: str) -> str:
    """The interactive page for one instrument."""
    return f"{DASHBOARD_BASE}/market/{symbol.upper()}"


def tradingview_url(instrument: "market.Instrument") -> str:
    """The instrument on TradingView, for anyone who wants the full toolkit."""
    ticker = instrument.id_for("tradingview")
    if ticker:
        return f"https://www.tradingview.com/chart/?symbol={ticker}"
    return f"https://www.tradingview.com/symbols/{instrument.symbol}/"


def chunk_lines(lines: list[str], limit: int = FIELD_LIMIT) -> list[str]:
    """Pack ``lines`` into field-sized blocks, never splitting a row."""
    blocks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        extra = len(line) + (1 if current else 0)
        if current and size + extra > limit:
            blocks.append("\n".join(current))
            current, size = [line], len(line)
            continue
        current.append(line)
        size += extra
    if current:
        blocks.append("\n".join(current))
    return blocks


def fmt_qty(quantity: float) -> str:
    """A holding size, at a precision a human can read.

    Quantities run from a whole share of stock to a millionth of a Bitcoin, so
    the format follows the number rather than the other way round.
    """
    quantity = float(quantity)
    if quantity == 0:
        return "0"
    if quantity >= 1_000:
        return f"{quantity:,.2f}"
    if quantity >= 1:
        return f"{quantity:,.4f}".rstrip("0").rstrip(".")
    return f"{quantity:.8f}".rstrip("0").rstrip(".")


def fmt_coins(value: float) -> str:
    """Coins, rounded for display — never silently to zero."""
    value = float(value)
    if value == 0:
        return "0"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:,.2f}M"
    if abs(value) >= 10_000:
        return f"{value:,.0f}"
    if abs(value) >= 1:
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def fmt_change(change_pct: float | None) -> str:
    """A 24-hour move, or nothing at all when the source did not report one."""
    if change_pct is None:
        return ""
    arrow = "▲" if change_pct > 0 else ("▼" if change_pct < 0 else "▬")
    return f" {arrow} `{change_pct:+.2f}%`"


def fmt_age(seconds: float) -> str:
    """How old a price is, in the shortest honest form."""
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def resolve(query: str) -> tuple[market.Instrument | None, str]:
    """Turn a `/market` argument into one instrument, or say why it cannot be."""
    matches = market.find(query)
    if not matches:
        return None, (
            f"❌ **{query}** is not on the board. `/market board` lists every "
            "symbol that is."
        )
    if len(matches) > 1:
        symbols = ", ".join(f"`{item.symbol}`" for item in matches[:8])
        return None, f"❓ **{query}** matches more than one: {symbols}\nTry the exact symbol."
    return matches[0], ""


def parse_amount(text: str, held: float) -> tuple[float, bool, str]:
    """Parse a sell amount: ``all``, a percentage of the position, or a quantity.

    Returns ``(quantity, sell_all, error)``.
    """
    raw = (text or "").strip().lower().replace(",", "")
    if raw in {"all", "max", "everything"}:
        return held, True, ""
    if raw.endswith("%"):
        try:
            percent = float(raw[:-1])
        except ValueError:
            return 0.0, False, f"❌ `{text}` is not a percentage I can read."
        if percent <= 0 or percent > 100:
            return 0.0, False, "❌ A percentage has to be between 0 and 100."
        quantity = held * (percent / 100.0)
        return quantity, quantity >= held, ""
    try:
        quantity = float(raw)
    except ValueError:
        return 0.0, False, (
            f"❌ `{text}` is not an amount. Give a quantity, a percentage like "
            "`50%`, or `all`."
        )
    if quantity <= 0:
        return 0.0, False, "❌ The amount has to be positive."
    return quantity, quantity >= held, ""


class Market(commands.Cog):
    """A live market: real prices, real fills, no invented numbers."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "market"):
                await interaction.response.send_message(
                    "⚠️ The **Market** is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the "
                    "[Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True,
                )
                return False
        # One gate for the whole group, so a subcommand added later cannot
        # forget it — the only way past this is holding the role.
        if interaction.guild is not None:
            problem = self.access_problem(interaction)
            if problem is not None:
                await interaction.response.send_message(
                    self._access_message(interaction, problem), ephemeral=True
                )
                return False
        return True

    @staticmethod
    def access_problem(interaction: discord.Interaction) -> str | None:
        """Whether this member may trade at all, using the shared rule.

        Discord gives the member's roles as objects, so they are reduced to ids
        here and the decision itself stays in :func:`market.access_problem`,
        where it can be tested without a client.
        """
        member = interaction.user
        guild = interaction.guild
        if guild is None:
            return None
        held = getattr(member, "_roles", None)
        if held is None:
            held = {role.id for role in getattr(member, "roles", [])}
        return market.access_problem(
            held, guild_role_ids=(role.id for role in guild.roles)
        )

    def _access_message(self, interaction: discord.Interaction, problem: str) -> str:
        """Why the market refused this member, in terms they can act on."""
        allowed = market.access_role_ids()
        if problem == "missing":
            listed = ", ".join(f"`{role_id}`" for role_id in allowed)
            return (
                "🔒 The market is restricted to a role this server does not have "
                f"({listed}), so nobody can trade. Ask an administrator to create "
                "that role and give it out, or set `MARKET_ROLE_IDS` to a role "
                "this server already has."
            )
        names = [role.name for role in interaction.guild.roles if role.id in allowed]
        where = ", ".join(f"**{name}**" for name in names) or "the configured role"
        return (
            f"🔒 The market is restricted to members with the {where} role — "
            "trading is closed to everyone else."
        )

    # ----------------------------------------------------------------- #
    # Helpers
    # ----------------------------------------------------------------- #
    async def _board(self, now: float) -> tuple[market.Snapshot, str]:
        """The snapshot to show, and a line describing how good it is.

        A stale board is refreshed once before it is shown; if the refresh still
        cannot produce a current price the card says so, because a price with no
        age on it is the one thing this market must never show.
        """
        snap = await market.snapshot(now)
        # Force only when the board is genuinely old. A symbol that no source can
        # price is not a reason to fetch again on every view — the card shows it
        # as unavailable, and ``snapshot`` has its own floor on forced refreshes.
        if snap.age(now) > market.QUOTE_TTL_SECONDS:
            snap = await market.snapshot(now, force=True)
        age = snap.age(now)
        sources = ", ".join(snap.sources()) or "no source"
        if not snap.quotes:
            return snap, (
                "🔌 **No live prices right now.** Every source is unreachable, so "
                "trading is paused rather than priced off a guess."
            )
        state = "live" if age <= market.MAX_QUOTE_AGE_SECONDS else "**stale — trading paused**"
        return snap, f"Prices from **{sources}**, {fmt_age(age)} old ({state})."

    async def _positions_value(
        self, user_id: int, guild_id: int, snap: market.Snapshot, now: float
    ) -> tuple[dict[str, market.Holding], float, float]:
        """Holdings, their value at the sell price, and what they cost."""
        held = await market.holdings(user_id, guild_id)
        value = cost = 0.0
        for symbol, holding in held.items():
            quote = snap.quote(symbol)
            if quote is None:
                cost += holding.cost_coins
                continue
            # Net of the treat: what selling today would actually pay.
            value += market.sell_value(holding.quantity, quote.sell_coins())
            cost += holding.cost_coins
        return held, value, cost

    async def _fresh_quote(
        self, symbol: str, now: float
    ) -> tuple[market.Snapshot, market.Quote | None]:
        """A quote recent enough to trade against, refreshing once if needed."""
        snap = await market.snapshot(now)
        quote = snap.tradable(symbol, now)
        if quote is None:
            snap = await market.snapshot(now, force=True)
            quote = snap.tradable(symbol, now)
        return snap, quote

    # ----------------------------------------------------------------- #
    # Commands
    # ----------------------------------------------------------------- #
    # Deliberately not called ``market``: a class-body assignment of that name
    # would shadow the imported ``utils.market`` module for every decorator
    # below it, which is exactly how `/eco-admin`'s handlers once went missing.
    # The slash-command name comes from ``name=`` here, not from this attribute.
    market_group = app_commands.Group(
        name="market", description="Trade real markets with your coins."
    )

    @market_group.command(name="board", description="Live prices for everything on the market.")
    @app_commands.describe(category="Filter by category")
    @app_commands.choices(category=[
        app_commands.Choice(name="🌐 All", value="all"),
        app_commands.Choice(name="🪙 Crypto", value="crypto"),
        app_commands.Choice(name="📊 Stocks", value="stock"),
        app_commands.Choice(name="📈 ETFs", value="etf"),
        app_commands.Choice(name="🚀 Top Gainers", value="gainers"),
        app_commands.Choice(name="📉 Top Losers", value="losers"),
        app_commands.Choice(name="🔥 Most Active", value="active"),
    ])
    async def board(self, interaction: discord.Interaction, category: str = "all"):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ The market lives in a server.", ephemeral=True
            )
        now = time.time()
        snap, status = await self._board(now)

        # Determine which instruments to show
        if category == "crypto":
            instruments = [i for i in market.INSTRUMENTS if i.kind == market.CRYPTO]
            title = "📈 Crypto Board"
        elif category == "stock":
            instruments = [i for i in market.INSTRUMENTS if i.kind == market.STOCK and not getattr(i.ids, 'get', lambda x: None)('nasdaq_class') == 'etf']
            title = "📈 Stocks Board"
        elif category == "etf":
            instruments = [i for i in market.INSTRUMENTS if i.kind == market.STOCK and i.ids.get('nasdaq_class') == 'etf']
            title = "📈 ETF Board"
        elif category == "gainers":
            # Get top 10 gainers
            all_with_quotes = []
            for instrument in market.INSTRUMENTS:
                quote = snap.quote(instrument.symbol)
                if quote and quote.change_pct is not None:
                    all_with_quotes.append((instrument, quote))
            all_with_quotes.sort(key=lambda x: x[1].change_pct, reverse=True)
            instruments = [i for i, _ in all_with_quotes[:10]]
            title = "🚀 Top Gainers"
        elif category == "losers":
            # Get top 10 losers
            all_with_quotes = []
            for instrument in market.INSTRUMENTS:
                quote = snap.quote(instrument.symbol)
                if quote and quote.change_pct is not None:
                    all_with_quotes.append((instrument, quote))
            all_with_quotes.sort(key=lambda x: x[1].change_pct)
            instruments = [i for i, _ in all_with_quotes[:10]]
            title = "📉 Top Losers"
        elif category == "active":
            # Show the most popular trading pairs (crypto majors + top stocks)
            crypto_majors = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX"]
            stock_majors = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AMD"]
            instruments = [i for i in market.INSTRUMENTS if i.symbol in crypto_majors + stock_majors]
            title = "🔥 Most Active"
        else:
            # Show all
            instruments = market.INSTRUMENTS
            title = "📈 Market Board"

        embed = discord.Embed(
            title=title,
            description=(
                f"{status}\n"
                "Fills happen at the observed price, both sides — **no spread**.\n"
                f"The only charge is the **{market.treat_fee_label()} Meow Treat fee** 🐾, "
                "itemised on every fill.\n"
                f"`1 USD = {market.COINS_PER_USD:,} 🪙` • "
                f"**{len(instruments)}** instruments • "
                f"[live chart]({DASHBOARD_BASE}/market/BTC)"
            ),
            color=0x2ECC71,
            timestamp=discord.utils.utcnow(),
        )

        # Group by kind for display
        if category in ("all",):
            # Show all categories
            for kind, label in ((market.CRYPTO, "🪙 Crypto"), (market.STOCK, "📊 Stocks")):
                kind_instruments = [i for i in instruments if i.kind == kind]
                lines = []
                for instrument in kind_instruments:
                    quote = snap.quote(instrument.symbol)
                    if quote is None:
                        lines.append(f"**{instrument.symbol}** · {instrument.name} — `unavailable`")
                        continue
                    lines.append(
                        f"**{instrument.symbol}** · {instrument.name} — "
                        f"`{fmt_coins(quote.mid_coins())}` 🪙{fmt_change(quote.change_pct)}"
                    )
                for index, block in enumerate(chunk_lines(lines)):
                    embed.add_field(
                        name=label if index == 0 else f"{label} (cont.)",
                        value=block,
                        inline=False,
                    )
        elif category in ("gainers", "losers"):
            # Show sorted list with rank
            lines = []
            for idx, instrument in enumerate(instruments, 1):
                quote = snap.quote(instrument.symbol)
                if quote is None:
                    continue
                emoji = "🟢" if (quote.change_pct or 0) > 0 else "🔴"
                lines.append(
                    f"{idx}. {emoji} **{instrument.symbol}** · {instrument.name} — "
                    f"`{fmt_coins(quote.mid_coins())}` 🪙{fmt_change(quote.change_pct)}"
                )
            embed.add_field(
                name="Rankings",
                value="\n".join(lines) if lines else "No data available",
                inline=False,
            )
        else:
            # Single category view
            label_map = {
                "crypto": "🪙 Crypto",
                "stock": "📊 Stocks",
                "etf": "📈 ETFs",
                "active": "🔥 Most Active",
            }
            label = label_map.get(category, "Instruments")
            lines = []
            for instrument in instruments:
                quote = snap.quote(instrument.symbol)
                if quote is None:
                    lines.append(f"**{instrument.symbol}** · {instrument.name} — `unavailable`")
                    continue
                lines.append(
                    f"**{instrument.symbol}** · {instrument.name} — "
                    f"`{fmt_coins(quote.mid_coins())}` 🪙{fmt_change(quote.change_pct)}"
                )
            for index, block in enumerate(chunk_lines(lines)):
                embed.add_field(
                    name=label if index == 0 else f"{label} (cont.)",
                    value=block,
                    inline=False,
                )

        held, value, cost = await self._positions_value(
            interaction.user.id, interaction.guild.id, snap, now
        )
        if held and category == "all":
            profit = value - cost
            embed.add_field(
                name="💼 Your Positions",
                value=(
                    f"• Value at sell price (after treats): **{fmt_coins(value)}** 🪙\n"
                    f"• Cost: **{fmt_coins(cost)}** 🪙\n"
                    f"• Unrealised: **{profit:+,.0f}** 🪙 "
                    f"({(profit / cost * 100) if cost else 0:+.2f}%)"
                ),
                inline=False,
            )
        embed.set_footer(text="🐾 Meow Markets • /market buy • /market sell • /market portfolio")
        await send_card(interaction, embed=embed)

    @market_group.command(name="quote", description="One instrument in detail.")
    @app_commands.describe(symbol="Ticker symbol, e.g. BTC or AAPL")
    async def quote(self, interaction: discord.Interaction, symbol: str):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ The market lives in a server.", ephemeral=True
            )
        instrument, problem = resolve(symbol)
        if instrument is None:
            return await interaction.response.send_message(problem, ephemeral=True)

        now = time.time()
        snap = await market.snapshot(now)
        snapshot_quote = snap.quote(instrument.symbol)
        if snapshot_quote is None:
            snap = await market.snapshot(now, force=True)
            snapshot_quote = snap.quote(instrument.symbol)
        if snapshot_quote is None:
            return await interaction.response.send_message(
                f"🔌 No live price for **{instrument.symbol}** right now — every source "
                "that carries it is unreachable. Try again in a moment.",
                ephemeral=True,
            )

        pct = fmt_change(snapshot_quote.change_pct) or " *no 24h figure*"
        # The sparkline is drawn from prices this bot actually saw, so it appears
        # once there are two observations and never before.
        observed = await market.observed_series(instrument.symbol, 24 * 3600, now, limit=60)
        spark = chart.sparkline([candle.close for candle in observed])
        embed = discord.Embed(
            title=f"{instrument.symbol} · {instrument.name}",
            description=(
                f"### `{fmt_coins(snapshot_quote.mid_coins())}` 🪙{pct}\n"
                f"*${snapshot_quote.price_usd:,.2f} at "
                f"`{market.COINS_PER_USD:,}` coins per dollar*"
                + (f"\n**{spark}**  *last 24h of observed prices*" if spark else "")
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Order Book (this bot)",
            value=(
                f"• Buy at **{fmt_coins(snapshot_quote.buy_coins())}** 🪙 per unit\n"
                f"• Sell at **{fmt_coins(snapshot_quote.sell_coins())}** 🪙 per unit\n"
                "• Both sides are the observed price — *no spread*, so your "
                "return is the market's return.\n"
                f"• 🐾 **{market.treat_fee_label()} Meow Treat fee** on each fill "
                f"(min 1 🪙) — **{market.treat_fee(10_000)}** 🪙 on a 10,000 🪙 fill."
            ),
            inline=False,
        )
        held = (await market.holdings(interaction.user.id, interaction.guild.id)).get(
            instrument.symbol
        )
        if held and held.quantity > 0:
            value = market.sell_value(held.quantity, snapshot_quote.sell_coins())
            profit = value - held.cost_coins
            percent = (profit / held.cost_coins * 100) if held.cost_coins else 0.0
            embed.add_field(
                name="Your Position",
                value=(
                    f"• **{fmt_qty(held.quantity)}** {instrument.symbol} "
                    f"@ {fmt_coins(held.avg_cost_coins)} 🪙 avg\n"
                    f"• Worth **{fmt_coins(value)}** 🪙 "
                    f"• **{profit:+,.0f}** 🪙 ({percent:+.2f}%)"
                ),
                inline=False,
            )
        embed.set_footer(
            text=(
                f"Source: {snapshot_quote.source} • {fmt_age(snapshot_quote.age(now))} old • "
                f"/market buy {instrument.symbol} • /market sell {instrument.symbol} all"
            )
        )
        await send_card(interaction, embed=embed)

    @market_group.command(
        name="chart", description="A price chart, with your average cost drawn on it."
    )
    @app_commands.describe(
        symbol="Ticker symbol, e.g. BTC or AAPL",
        period="How much history to draw",
    )
    @app_commands.choices(
        period=[
            app_commands.Choice(name="24 hours", value="day"),
            app_commands.Choice(name="7 days", value="week"),
            app_commands.Choice(name="30 days", value="month"),
        ]
    )
    async def chart(
        self,
        interaction: discord.Interaction,
        symbol: str,
        period: str = market.DEFAULT_PERIOD,
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ The market lives in a server.", ephemeral=True
            )
        instrument, problem = resolve(symbol)
        if instrument is None:
            return await interaction.response.send_message(problem, ephemeral=True)

        now = time.time()
        series = await market.fetch_candles(instrument, period or market.DEFAULT_PERIOD, now=now)
        if series is None or len(series.candles) < 2:
            return await interaction.response.send_message(
                f"📉 Not enough price history for **{instrument.symbol}** yet. The bot records "
                "every price it sees, so a chart appears as soon as there are two "
                "observations — try again in a few minutes.",
                ephemeral=True,
            )

        window = market.PERIODS.get(period) or market.PERIODS[market.DEFAULT_PERIOD]
        snapshot_this = await market.snapshot(now)
        snapshot_quote = snapshot_this.quote(instrument.symbol)
        holding = (await market.holdings(interaction.user.id, interaction.guild.id)).get(
            instrument.symbol
        )
        closes = series.closes()
        first, last = closes[0], closes[-1]
        change = ((last - first) / first * 100) if first else 0.0
        price_coins = (
            snapshot_quote.mid_coins() if snapshot_quote else last * market.COINS_PER_USD
        )

        # Charts are drawn in coins, because that is what the header, the member's
        # average cost and their profit are all in — an axis in dollars with a
        # cost line in coins would put their line off the canvas entirely.
        plot_series = series.scaled(market.COINS_PER_USD)
        buffer = chart.render_market_chart(
            symbol=instrument.symbol,
            name=instrument.name,
            candles=list(plot_series.candles),
            price_coins=price_coins,
            change_pct=change,
            entry_coins=holding.avg_cost_coins if holding else None,
            source=series.source,
            interval_label=f"{series.axis_label} · {window.label}",
            age_seconds=snapshot_quote.age(now) if snapshot_quote else None,
        )

        embed = discord.Embed(
            title=f"📈 {instrument.symbol} · {instrument.name}",
            description=(
                f"### {fmt_coins(price_coins)} 🪙\n"
                f"*{change:+.2f}% over the last {window.label}*"
                + (f" • 24h {snapshot_quote.change_pct:+.2f}%" if snapshot_quote and snapshot_quote.change_pct is not None else "")
            ),
            color=0x2ECC71 if change >= 0 else 0xED4245,
            timestamp=discord.utils.utcnow(),
        )
        if holding and holding.quantity > 0:
            value = market.sell_value(
                holding.quantity,
                snapshot_quote.sell_coins() if snapshot_quote else price_coins,
            )
            profit = value - holding.cost_coins
            percent = (profit / holding.cost_coins * 100) if holding.cost_coins else 0.0
            embed.add_field(
                name="Your Position",
                value=(
                    f"• **{fmt_qty(holding.quantity)}** {instrument.symbol} @ "
                    f"{fmt_coins(holding.avg_cost_coins)} 🪙 avg *(yellow line)*\n"
                    f"• Worth **{fmt_coins(value)}** 🪙 • "
                    f"**{profit:+,.0f}** 🪙 ({percent:+.2f}%)"
                ),
                inline=False,
            )
        # The image is a snapshot of one moment; the page is the same prices,
        # live, with a crosshair and a what-if calculator. Both are offered,
        # because a card cannot update and a page cannot be reacted to.
        embed.add_field(
            name="🔗 Live & interactive",
            value=(
                f"• [**Open the live chart**]({live_chart_url(instrument.symbol)}) "
                "— it refreshes itself every few seconds\n"
                f"• [Full TradingView chart]({tradingview_url(instrument)}) "
                "— every indicator and tool"
            ),
            inline=False,
        )
        embed.set_image(url="attachment://chart.png")
        embed.set_footer(
            text=f"🐾 {series.source} • {len(series.candles)} points • /market sell {instrument.symbol} all"
        )
        await send_card(interaction, embed=embed, file=discord.File(buffer, filename="chart.png"))

    @market_group.command(name="buy", description="Buy an instrument with coins.")
    @app_commands.describe(
        symbol="Ticker symbol, e.g. BTC or AAPL",
        coins=f"Coins to spend ({market.MIN_TRADE_COINS:,}-{market.MAX_TRADE_COINS:,})",
    )
    async def buy(
        self,
        interaction: discord.Interaction,
        symbol: str,
        coins: app_commands.Range[int, market.MIN_TRADE_COINS, market.MAX_TRADE_COINS],
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ The market lives in a server.", ephemeral=True
            )
        instrument, problem = resolve(symbol)
        if instrument is None:
            return await interaction.response.send_message(problem, ephemeral=True)

        now = time.time()
        waiting = market.trade_cooldown_remaining(interaction.user.id, interaction.guild.id, now)
        if waiting > 0.05:
            return await interaction.response.send_message(
                f"⏳ The book is settling — try again in **{waiting:.1f}s**.", ephemeral=True
            )

        await ensure_eco(interaction.user.id, interaction.guild.id)
        wallet = (await get_eco(interaction.user.id, interaction.guild.id))["wallet"]
        if wallet < coins:
            return await interaction.response.send_message(
                f"❌ That costs **{coins:,}** 🪙 and your wallet holds "
                f"**{wallet:,}** 🪙.",
                ephemeral=True,
            )

        snap, snapshot_quote = await self._fresh_quote(instrument.symbol, now)
        if snapshot_quote is None:
            reason = (
                "there is no live price for it right now"
                if snap.quote(instrument.symbol) is None
                else f"its last price is {fmt_age(snap.quote(instrument.symbol).age(now))} old"
            )
            return await interaction.response.send_message(
                f"🔌 I will not fill that: {reason}. A fill needs a current price — "
                "try again in a moment.",
                ephemeral=True,
            )

        fill = await market.settle_buy(
            interaction.user.id, interaction.guild.id, instrument, coins, snapshot_quote, now
        )
        if not fill.ok:
            messages = {
                "funds": "❌ Your wallet changed while that was processing — try again.",
                "stale": "🔌 That price went stale mid-order — try again.",
                "too_small": f"❌ The smallest order is **{market.MIN_TRADE_COINS:,}** 🪙.",
                "too_large": f"❌ The largest single order is **{market.MAX_TRADE_COINS:,}** 🪙.",
            }
            return await interaction.response.send_message(
                messages.get(fill.reason, "❌ That order could not be filled."), ephemeral=True
            )

        market.note_trade(interaction.user.id, interaction.guild.id, now)
        left = (await get_eco(interaction.user.id, interaction.guild.id))["wallet"]
        embed = discord.Embed(
            title="🟢 Order Filled — Buy",
            description=(
                f"### +{fmt_qty(fill.quantity)} {instrument.symbol}\n"
                f"Spent **{fill.coins:,}** 🪙 at "
                f"**{fmt_coins(fill.unit_price_coins)}** 🪙 per unit.\n"
                f"*The observed market price — no spread. That outlay includes the "
                f"**{fmt_coins(fill.treat_coins)}** 🪙 Meow Treat fee 🐾 "
                f"({market.treat_fee_label()}), and it is part of your cost basis.*"
            ),
            color=0x2ECC71,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Position",
            value=(
                f"• Holding: **{fmt_qty(fill.quantity_after)}** {instrument.symbol}\n"
                f"• Average cost: **{fmt_coins(fill.avg_cost_coins)}** 🪙\n"
                f"• Wallet left: **{left:,}** 🪙"
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                f"Priced from {snapshot_quote.source}, {fmt_age(snapshot_quote.age(now))} old • "
                f"/market sell {instrument.symbol} all to close"
            )
        )
        await send_card(interaction, embed=embed)

    @market_group.command(name="sell", description="Sell an instrument back to the market.")
    @app_commands.describe(
        symbol="Ticker symbol, e.g. BTC or AAPL",
        amount="How much: a quantity, a percentage like 50%, or 'all'",
    )
    async def sell(self, interaction: discord.Interaction, symbol: str, amount: str):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ The market lives in a server.", ephemeral=True
            )
        instrument, problem = resolve(symbol)
        if instrument is None:
            return await interaction.response.send_message(problem, ephemeral=True)

        now = time.time()
        waiting = market.trade_cooldown_remaining(interaction.user.id, interaction.guild.id, now)
        if waiting > 0.05:
            return await interaction.response.send_message(
                f"⏳ The book is settling — try again in **{waiting:.1f}s**.", ephemeral=True
            )

        await ensure_eco(interaction.user.id, interaction.guild.id)
        holding = (await market.holdings(interaction.user.id, interaction.guild.id)).get(
            instrument.symbol
        )
        if holding is None or holding.quantity <= 0:
            return await interaction.response.send_message(
                f"❌ You do not hold any **{instrument.symbol}**.", ephemeral=True
            )

        quantity, sell_all, error = parse_amount(amount, holding.quantity)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)

        snap, snapshot_quote = await self._fresh_quote(instrument.symbol, now)
        if snapshot_quote is None:
            return await interaction.response.send_message(
                f"🔌 No current price for **{instrument.symbol}** — I will not fill a "
                "sale against an old number. Try again in a moment.",
                ephemeral=True,
            )

        fill = await market.settle_sell(
            interaction.user.id,
            interaction.guild.id,
            instrument,
            quantity,
            snapshot_quote,
            now,
            sell_all=sell_all,
        )
        if not fill.ok:
            if fill.reason == "too_many":
                return await interaction.response.send_message(
                    f"❌ You hold **{fmt_qty(holding.quantity)}** {instrument.symbol}, "
                    "not that much.",
                    ephemeral=True,
                )
            if fill.reason == "daily_cap":
                sold = await market.sold_coins_since(
                    interaction.user.id, interaction.guild.id, now - 24 * 3600
                )
                return await interaction.response.send_message(
                    f"📉 The market has already bought **{sold:,}** 🪙 back from you in "
                    f"the last 24 hours, and its ceiling is "
                    f"**{market.MAX_DAILY_SELL_COINS:,}** 🪙. Try again later.",
                    ephemeral=True,
                )
            messages = {
                "none": "❌ You do not hold that any more.",
                "too_small": "❌ That sale is too small to fill.",
                "stale": "🔌 That price went stale mid-order — try again.",
                "changed": "❌ Your position changed while that was processing — try again.",
            }
            return await interaction.response.send_message(
                messages.get(fill.reason, "❌ That sale could not be filled."), ephemeral=True
            )

        market.note_trade(interaction.user.id, interaction.guild.id, now)
        wallet = (await get_eco(interaction.user.id, interaction.guild.id))["wallet"]
        sold_today = await market.sold_coins_since(
            interaction.user.id, interaction.guild.id, now - 24 * 3600
        )
        embed = discord.Embed(
            title="🔴 Order Filled — Sell",
            description=(
                f"### −{fmt_qty(fill.quantity)} {instrument.symbol}\n"
                f"Received **{fill.coins:,}** 🪙 at "
                f"**{fmt_coins(fill.unit_price_coins)}** 🪙 per unit.\n"
                f"*The observed market price — no spread, so this is real P/L, after "
                f"the **{fmt_coins(fill.treat_coins)}** 🪙 Meow Treat fee 🐾.*"
            ),
            color=0xED4245,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Realised",
            value=(
                f"• Profit / loss: **{fill.realized:+,}** 🪙\n"
                f"• Remaining: **{fmt_qty(fill.quantity_after)}** {instrument.symbol}\n"
                f"• Wallet: **{wallet:,}** 🪙"
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                f"Market buyback used today: {sold_today:,}/{market.MAX_DAILY_SELL_COINS:,} 🪙 • "
                f"priced from {snapshot_quote.source}, {fmt_age(snapshot_quote.age(now))} old"
            )
        )
        await send_card(interaction, embed=embed)

    @market_group.command(name="portfolio", description="Your positions and what they are worth.")
    async def portfolio(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ The market lives in a server.", ephemeral=True
            )
        now = time.time()
        snap = await market.snapshot(now)
        held, value, cost = await self._positions_value(
            interaction.user.id, interaction.guild.id, snap, now
        )
        if not held:
            return await interaction.response.send_message(
                "💼 You hold nothing yet. `/market board` shows what is for sale.",
                ephemeral=True,
            )

        lines = []
        unpriced = []
        for symbol, holding in sorted(held.items()):
            instrument = market.BY_SYMBOL.get(symbol)
            name = instrument.name if instrument else symbol
            snapshot_quote = snap.quote(symbol)
            if snapshot_quote is None:
                unpriced.append(symbol)
                lines.append(
                    f"**{symbol}** · {fmt_qty(holding.quantity)} "
                    f"@ {fmt_coins(holding.avg_cost_coins)} 🪙 — *no live price*"
                )
                continue
            position_value = market.sell_value(holding.quantity, snapshot_quote.sell_coins())
            profit = position_value - holding.cost_coins
            change = (profit / holding.cost_coins * 100) if holding.cost_coins else 0.0
            lines.append(
                f"**{symbol}** · {name}\n"
                f"> {fmt_qty(holding.quantity)} @ {fmt_coins(holding.avg_cost_coins)} 🪙 avg • "
                f"now {fmt_coins(snapshot_quote.sell_coins())} 🪙\n"
                f"> worth **{fmt_coins(position_value)}** 🪙 • "
                f"**{profit:+,.0f}** 🪙 ({change:+.2f}%)"
            )

        profit = value - cost
        realised = await market.realized_total(interaction.user.id, interaction.guild.id)
        embed = discord.Embed(
            title="💼 Your Portfolio",
            description="\n".join(lines),
            color=0x5865F2 if profit + realised["realized"] >= 0 else 0xED4245,
            timestamp=discord.utils.utcnow(),
        )
        # Both halves of profit are shown: what has been banked by selling, and
        # what is only on paper right now. A single number would let a member
        # believe a loss is real when it is unrealised, or the reverse.
        unrealised_pct = (profit / cost * 100) if cost else 0.0
        embed.add_field(
            name="Totals",
            value=(
                f"• Position value at the sell price: **{fmt_coins(value)}** 🪙\n"
                f"• Cost of what you hold: **{fmt_coins(cost)}** 🪙\n"
                f"• **Unrealised: {profit:+,.0f}** 🪙 ({unrealised_pct:+.2f}%)\n"
                f"• **Realised: {realised['realized']:+,}** 🪙 over "
                f"{realised['sales']} sale{'s' if realised['sales'] != 1 else ''} "
                f"({realised['wins']} profitable)\n"
                f"• **Total: {profit + realised['realized']:+,.0f}** 🪙"
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                ("⚠️ Unpriced: " + ", ".join(unpriced) + " • " if unpriced else "")
                + f"prices {fmt_age(snap.age(now))} old • /market sell <symbol> all"
            )
        )
        await send_card(interaction, embed=embed)

    @market_group.command(name="history", description="Your most recent fills.")
    async def history(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ The market lives in a server.", ephemeral=True
            )
        now = time.time()
        fills = await market.trade_history(
            interaction.user.id, interaction.guild.id, HISTORY_LIMIT
        )
        if not fills:
            return await interaction.response.send_message(
                "🧾 No fills yet. `/market board` shows what is for sale.", ephemeral=True
            )

        summary = await market.realized_total(interaction.user.id, interaction.guild.id)
        lines = []
        for fill in fills:
            icon = "🟢" if fill["side"] == "buy" else "🔴"
            line = (
                f"{icon} **{fill['side'].upper()}** {fmt_qty(fill['quantity'])} "
                f"{fill['symbol']} @ {fmt_coins(fill['unit_price_coins'])} 🪙 "
                f"• {fill['coins']:,} 🪙"
            )
            if fill["side"] == "sell":
                line += f" • P/L **{fill['realized']:+,}** 🪙"
            line += f" • *{fmt_age(now - fill['ts'])} ago*"
            lines.append(line)

        embed = discord.Embed(
            title="🧾 Trade History",
            description="\n".join(lines),
            color=0x5865F2 if summary["realized"] >= 0 else 0xED4245,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="All Time",
            value=(
                f"• **Realised: {summary['realized']:+,}** 🪙 across "
                f"{summary['sales']} sale{'s' if summary['sales'] != 1 else ''}\n"
                f"• {summary['wins']} of them were profitable"
            ),
            inline=False,
        )
        embed.set_footer(text=f"🐾 Last {len(fills)} fills • /market portfolio")
        await send_card(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Market(bot))
