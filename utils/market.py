"""The market: real prices from live exchanges, and nothing invented.

Everything here exists to make one promise true — **a price on the board is a
price somebody could actually trade at.** That has three consequences.

**The numbers come from exchanges, never from this file.** Crypto is read from
Binance's public 24-hour ticker, stocks from TradingView's scanner; Kraken,
Coinbase and CNBC sit behind them as fallbacks for hosts the first source
blocks. None of them needs an API key, and none of them is a random number
generator: if every source is unreachable the board says so and *trading stops*
rather than pricing a fill off a guess or an old number.

**One observation is shared.** A snapshot is fetched once and reused by every
command for ``QUOTE_TTL_SECONDS``, then persisted to the database so a restart
still shows the last known board. Because there is exactly one observation in
flight, two members cannot trade against each other at two prices the world
never had, and the difference between what they see and what is true is a
single, printed age rather than a per-command guess.

**Freshness is a precondition, not a detail.** A quote older than
``MAX_QUOTE_AGE_SECONDS`` cannot be traded against at all: the position someone
is looking at is real, the moment being priced may not be. Every card shows the
age and the source, so the number is never presented as more certain than it is.

That age is when *this bot* observed the price, which is what the freshness gate
is about — an observation's age. A market that is closed still has a last traded
price, and that is a real price, so a weekend stock quote is shown for what it is
rather than hidden or withheld; what is never done is filling an order against a
price nobody has seen for minutes.

The bot is the counterparty. Buying burns coins and selling mints them, priced
off the real market, which is what makes a rising asset worth holding. Fills
happen **at the observed price on both sides** — ``buy_coins()`` and
``sell_coins()`` are the same number — so a real trader's percentage return is
reproduced **1:1**: an asset that moved +10% in dollars moved +10% for a member
holding it here. Prices are quoted in the bot's currency at ``COINS_PER_USD``,
which is **1** — one coin is one dollar, so the board reads like a price board
rather than a conversion puzzle.

The one thing taken is a **named, itemised fee**: ``MEOW_TREAT_FEE``, the house
cat's cut, charged on top of a buy and out of a sell and banked into
Meow Bank (the server's house account). It is printed on every receipt with the amount, so a member sees exactly
what the treat cost and the fill price itself is never quietly worse than the
market.

**Trading is role-gated.** ``/market`` is restricted to the roles in
``access_role_ids()`` — by default the single role in
``DEFAULT_ACCESS_ROLE_IDS``, overridable with ``MARKET_ROLE_IDS`` — and the
gate is deliberately strict: no other member, and no administrator, can trade.
``access_problem`` is pure, so the rule is testable without a Discord client.

Limits, because a market is a place to lose money and not a place to print it:

``MIN_TRADE_COINS`` / ``MAX_TRADE_COINS``
    A floor that keeps dust orders off the book, and a ceiling on one fill.
``MAX_DAILY_SELL_COINS``
    How much the market will buy back from one member in a rolling day. It is
    generous, and it is the thing that stops a single lucky position from
    minting an unbounded number of coins in one afternoon.
``TRADE_COOLDOWN_SECONDS``
    Between one member's fills, so the command cannot be scripted.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping

import aiohttp
import aiosqlite

from utils import config_audit, db
from utils import meowfee

# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #
#: Coins per US dollar. Quotes arrive in USD and the bot's currency is coins, so
#: one exchange rate prices the whole market. At 250, a share of stock lands
#: between the shop's Pro and Prestige tiers and a thousandth of a Bitcoin is a
#: sensible purchase rather than a rounding error.
COINS_PER_USD = 1
#: The rate this bot used before the change to 1:1. Every stored amount — a
#: position's cost basis, a fill's unit price, a realised profit — is in coin
#: units, so switching the rate without rescaling them would show every existing
#: holder a 99.6% paper loss on a position that never moved. ``utils/db`` rescales
#: those rows exactly once, guarded by a marker, from this value to the current
#: one. A literal on purpose: it must never drift with the live rate.
PREVIOUS_COINS_PER_USD = 250

#: The house cat's cut, as a share of a fill. This is **a named, itemised fee
#: rather than a spread**: the price a fill happens at is still 1:1 with the
#: observed market, and the treat is a separate line on every receipt — charged on
#: top of a buy and taken out of a sell, with at least one treat on any fill, and
#: **banked** into Meow Bank (the server's house account).
MEOW_TREAT_FEE = 0.0025


def treat_fee(coins: float) -> int:
    """The treats owed on a fill of ``coins``.

    Proportional, rounded to a whole coin, and never less than one on a fill that
    actually happens — a fractional treat cannot be paid, and a fee that rounds
    to nothing is not a fee.
    """
    if coins <= 0:
        return 0
    return max(1, int(round(coins * MEOW_TREAT_FEE)))


def treat_fee_label() -> str:
    """``0.25%`` — the fee as it is printed on a card."""
    return f"{MEOW_TREAT_FEE * 100:g}%"


def rate_rescale_factor() -> float:
    """What to multiply a *pre-1:1* stored amount by to keep it correct."""
    return COINS_PER_USD / PREVIOUS_COINS_PER_USD
#: How much of a fill's value the market keeps. Zero: fills happen at the
#: observed price on both sides, so a member's return *is* the asset's real
#: return — buy 25,000 🪙 of something that moves +10% and the position is worth
#: 27,500 🪙, not "10% minus a cut". A round trip at an unchanged price costs
#: nothing at all.
SPREAD = 0.0

# --------------------------------------------------------------------------- #
# Who may trade
# --------------------------------------------------------------------------- #
#: The role (or roles) allowed to use ``/market``. This is a gate, not a
#: permission: a member either holds one of these roles or is refused, and
#: administrators are not an exception to it.
DEFAULT_ACCESS_ROLE_IDS: tuple[int, ...] = (1548396810257698977,)


def access_role_ids() -> tuple[int, ...]:
    """The roles allowed to trade, from ``MARKET_ROLE_IDS`` if it is set.

    A comma- or semicolon-separated list, so a server can authorise more than one
    role without a code change. Anything that is not a snowflake is ignored; if
    nothing usable is left the compiled default stands, because an empty allow
    list would silently lock the market for everyone forever.
    """
    raw = config_audit.get("MARKET_ROLE_IDS") or config_audit.get("MARKET_ROLE_ID")
    parsed = tuple(
        int(part)
        for part in raw.replace(";", ",").split(",")
        if part.strip().isdigit()
    )
    return parsed or DEFAULT_ACCESS_ROLE_IDS


def access_problem(
    member_role_ids: Iterable[int],
    *,
    allowed_role_ids: Iterable[int] | None = None,
    guild_role_ids: Iterable[int],
) -> str | None:
    """``None`` when this member may trade, otherwise why they may not.

    ``"role"``    — the server has the role, but this member does not hold it.
    ``"missing"`` — the server does not have the configured role at all, so
                     *nobody* can hold it. Still a refusal, on purpose: the rule
                     is "one of these roles, and nobody else", and quietly
                     letting administrators through would be a different rule.
                     The card says so out loud, which is what makes it fixable.
    """
    allowed = set(allowed_role_ids if allowed_role_ids is not None else access_role_ids())
    if allowed & set(member_role_ids):
        return None
    if allowed.isdisjoint(guild_role_ids):
        return "missing"
    return "role"

# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #
#: A board is reused for this long before it is fetched again.
QUOTE_TTL_SECONDS = 45.0
#: A price older than this cannot be traded against, however good it looks.
MAX_QUOTE_AGE_SECONDS = 180.0
#: Per HTTP request, so a hanging source cannot hold a command open.
REQUEST_TIMEOUT_SECONDS = 8.0
#: Floor between two *forced* refreshes. Without it a symbol that no source can
#: price would make every `/market board` view fetch again — an unbounded way to
#: exhaust a provider's rate limit — and a board seconds old cannot be improved
#: by asking twice.
REFRESH_MIN_INTERVAL_SECONDS = 15.0
#: Between one member's fills.
TRADE_COOLDOWN_SECONDS = 5.0
#: Smallest and largest single fill, in coins.
MIN_TRADE_COINS = 100
MAX_TRADE_COINS = 250_000
#: Most coins the market will pay one member for their holdings in a rolling 24h.
MAX_DAILY_SELL_COINS = 1_000_000
#: Quantities are real numbers; this is the precision they are stored at.
QUANTITY_DECIMALS = 8
#: A holding smaller than this is dust and is written off rather than tracked.
DUST_QUANTITY = 10 ** -QUANTITY_DECIMALS
#: How long observed prices are kept for charts. One row per instrument per
#: refresh, pruned on write, so the table stays small however long the bot runs.
HISTORY_RETENTION_DAYS = 7
#: Most observed points one chart will draw.
MAX_OBSERVED_POINTS = 400

CRYPTO = "crypto"
STOCK = "stock"

HTTP_HEADERS = {
    "User-Agent": "MeowDiscordBot/2.0 (+https://meow-bot.pages.dev)",
    "Accept": "application/json,text/csv,*/*",
}
#: Nasdaq's public chart endpoint closes the connection to a bot-style
#: User-Agent before answering, so history requests use a browser one. It is a
#: keyless, unauthenticated quote endpoint — nothing is being bypassed except a
#: user-agent filter — and the quotes themselves still use ``HTTP_HEADERS``.
HISTORY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,*/*",
}
#: History requests get longer than a quote: Nasdaq's chart endpoint takes
#: seconds rather than milliseconds, and an explicit command can afford to wait.
HISTORY_TIMEOUT_SECONDS = 12.0


# --------------------------------------------------------------------------- #
# The catalog
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Instrument:
    """One tradable thing, and what each provider calls it."""

    symbol: str
    name: str
    kind: str
    ids: Mapping[str, str]

    def id_for(self, provider: str) -> str | None:
        return self.ids.get(provider)


def _crypto(
    symbol: str,
    name: str,
    *,
    binance: str | None = None,
    kraken: str | None = None,
    coinbase: str | None = None,
) -> Instrument:
    """A coin, and whatever each provider calls it.

    Every provider id is optional: one that does not list a coin is simply never
    asked about it, rather than being asked for a pair it will not answer. Binance
    is the batched primary feed, TradingView mirrors it, and Kraken and Coinbase
    are the fallbacks.
    """
    ids = {
        key: value
        for key, value in (("binance", binance), ("kraken", kraken), ("coinbase", coinbase))
        if value
    }
    if binance:
        ids["tradingview"] = f"BINANCE:{binance}"
    return Instrument(symbol=symbol, name=name, kind=CRYPTO, ids=ids)


def _stock(
    symbol: str, name: str, *, exchange: str = "NASDAQ", etf: bool = False
) -> Instrument:
    """A share or a fund, and what each provider calls it.

    ``etf`` matters because Nasdaq's chart endpoint asks which book to look in and
    answers **HTTP 400 rather than an empty series** when told the wrong one — so
    every ETF's chart came back blank while the same request for a share worked.
    It is declared rather than probed: a wrong ticker and a wrong asset class are
    different failures, and only one of them is worth retrying.
    """
    return Instrument(
        symbol=symbol,
        name=name,
        kind=STOCK,
        ids={
            "tradingview": f"{exchange}:{symbol}",
            "cnbc": symbol,
            "nasdaq_class": "etf" if etf else "stocks",
        },
    )


#: What is for sale. Every entry is liquid and recognisable, because a market
#: nobody can read is a market nobody trades — and every provider id below was
#: checked against the live endpoint, since a wrong ticker does not raise, it just
#: leaves one row reading ``unavailable`` forever.
INSTRUMENTS: tuple[Instrument, ...] = (
    # -- Crypto: the majors, the layer ones, and a few memes --------------- #
    _crypto("BTC", "Bitcoin", binance="BTCUSDT", kraken="XBTUSD", coinbase="BTC-USD"),
    _crypto("ETH", "Ethereum", binance="ETHUSDT", kraken="ETHUSD", coinbase="ETH-USD"),
    _crypto("SOL", "Solana", binance="SOLUSDT", kraken="SOLUSD", coinbase="SOL-USD"),
    _crypto("XRP", "XRP", binance="XRPUSDT", kraken="XRPUSD", coinbase="XRP-USD"),
    _crypto("BNB", "BNB", binance="BNBUSDT", coinbase="BNB-USD"),
    _crypto("DOGE", "Dogecoin", binance="DOGEUSDT", kraken="DOGEUSD", coinbase="DOGE-USD"),
    _crypto("ADA", "Cardano", binance="ADAUSDT", kraken="ADAUSD", coinbase="ADA-USD"),
    _crypto("AVAX", "Avalanche", binance="AVAXUSDT", kraken="AVAXUSD", coinbase="AVAX-USD"),
    _crypto("LINK", "Chainlink", binance="LINKUSDT", kraken="LINKUSD", coinbase="LINK-USD"),
    _crypto("DOT", "Polkadot", binance="DOTUSDT", kraken="DOTUSD", coinbase="DOT-USD"),
    _crypto("LTC", "Litecoin", binance="LTCUSDT", kraken="LTCUSD", coinbase="LTC-USD"),
    _crypto("BCH", "Bitcoin Cash", binance="BCHUSDT", kraken="BCHUSD", coinbase="BCH-USD"),
    _crypto("XLM", "Stellar", binance="XLMUSDT", kraken="XLMUSD", coinbase="XLM-USD"),
    _crypto("ATOM", "Cosmos", binance="ATOMUSDT", kraken="ATOMUSD", coinbase="ATOM-USD"),
    _crypto("NEAR", "NEAR Protocol", binance="NEARUSDT", kraken="NEARUSD", coinbase="NEAR-USD"),
    _crypto("UNI", "Uniswap", binance="UNIUSDT", kraken="UNIUSD", coinbase="UNI-USD"),
    _crypto("AAVE", "Aave", binance="AAVEUSDT", kraken="AAVEUSD", coinbase="AAVE-USD"),
    _crypto("SUI", "Sui", binance="SUIUSDT", kraken="SUIUSD", coinbase="SUI-USD"),
    _crypto("APT", "Aptos", binance="APTUSDT", kraken="APTUSD", coinbase="APT-USD"),
    _crypto("ARB", "Arbitrum", binance="ARBUSDT", kraken="ARBUSD", coinbase="ARB-USD"),
    _crypto("OP", "Optimism", binance="OPUSDT", kraken="OPUSD", coinbase="OP-USD"),
    _crypto("INJ", "Injective", binance="INJUSDT", kraken="INJUSD", coinbase="INJ-USD"),
    _crypto("FIL", "Filecoin", binance="FILUSDT", kraken="FILUSD", coinbase="FIL-USD"),
    _crypto("TRX", "TRON", binance="TRXUSDT", kraken="TRXUSD", coinbase="TRX-USD"),
    _crypto("SHIB", "Shiba Inu", binance="SHIBUSDT", kraken="SHIBUSD", coinbase="SHIB-USD"),
    _crypto("PEPE", "Pepe", binance="PEPEUSDT", kraken="PEPEUSD", coinbase="PEPE-USD"),
    _crypto("TIA", "Celestia", binance="TIAUSDT", kraken="TIAUSD", coinbase="TIA-USD"),
    _crypto("SEI", "Sei", binance="SEIUSDT", kraken="SEIUSD", coinbase="SEI-USD"),
    _crypto("ALGO", "Algorand", binance="ALGOUSDT", kraken="ALGOUSD", coinbase="ALGO-USD"),
    _crypto("ETC", "Ethereum Classic", binance="ETCUSDT", kraken="ETCUSD", coinbase="ETC-USD"),
    # -- Stocks: megacaps, the chip complex, and the names people ask for -- #
    _stock("AAPL", "Apple"),
    _stock("MSFT", "Microsoft"),
    _stock("NVDA", "NVIDIA"),
    _stock("GOOGL", "Alphabet"),
    _stock("AMZN", "Amazon"),
    _stock("META", "Meta"),
    _stock("TSLA", "Tesla"),
    _stock("NFLX", "Netflix"),
    _stock("AMD", "AMD"),
    _stock("INTC", "Intel"),
    _stock("AVGO", "Broadcom"),
    _stock("QCOM", "Qualcomm"),
    _stock("TSM", "TSMC", exchange="NYSE"),
    _stock("ORCL", "Oracle", exchange="NYSE"),
    _stock("CRM", "Salesforce", exchange="NYSE"),
    _stock("ADBE", "Adobe"),
    _stock("PLTR", "Palantir", exchange="NASDAQ"),
    _stock("COIN", "Coinbase"),
    _stock("SHOP", "Shopify", exchange="NYSE"),
    _stock("UBER", "Uber", exchange="NYSE"),
    _stock("JPM", "JPMorgan", exchange="NYSE"),
    _stock("BAC", "Bank of America", exchange="NYSE"),
    _stock("V", "Visa", exchange="NYSE"),
    _stock("MA", "Mastercard", exchange="NYSE"),
    _stock("WMT", "Walmart", exchange="NYSE"),
    _stock("COST", "Costco"),
    _stock("KO", "Coca-Cola", exchange="NYSE"),
    _stock("PEP", "PepsiCo", exchange="NASDAQ"),
    _stock("MCD", "McDonald's", exchange="NYSE"),
    _stock("NKE", "Nike", exchange="NYSE"),
    _stock("DIS", "Disney", exchange="NYSE"),
    _stock("BA", "Boeing", exchange="NYSE"),
    _stock("XOM", "Exxon Mobil", exchange="NYSE"),
    _stock("PFE", "Pfizer", exchange="NYSE"),
    _stock("JNJ", "Johnson & Johnson", exchange="NYSE"),
    _stock("UNH", "UnitedHealth", exchange="NYSE"),
    _stock("BABA", "Alibaba", exchange="NYSE"),
    # -- ETFs, because "the market" is not one stock ---------------------- #
    _stock("SPY", "S&P 500 ETF", exchange="AMEX", etf=True),
    _stock("QQQ", "Nasdaq 100 ETF", etf=True),
    _stock("VOO", "Vanguard S&P 500", exchange="AMEX", etf=True),
    _stock("IWM", "Russell 2000 ETF", exchange="AMEX", etf=True),
    _stock("GLD", "Gold ETF", exchange="AMEX", etf=True),
)

#: How many instruments the catalog may hold before the board cannot fit in an
#: embed. 25 fields, 1,024 characters each and 6,000 characters of text total,
#: with each row costing roughly 60 characters once the symbol, name and price are
#: rendered — so ~70 rows fit in three fields, and this is the guard rail rather
#: than the limit. ``tests/test_market.py`` fails if the live catalog exceeds it.
MAX_CATALOG_SIZE = 80

BY_SYMBOL: dict[str, Instrument] = {item.symbol: item for item in INSTRUMENTS}


def find(query: str) -> list[Instrument]:
    """Instruments matching a `/market` argument, exact match first.

    Returns every match rather than the first, so the caller can ask which one
    was meant instead of silently trading the wrong ticker.
    """
    text = (query or "").strip().upper()
    if not text:
        return []
    if text in BY_SYMBOL:
        return [BY_SYMBOL[text]]
    matches = [
        item
        for item in INSTRUMENTS
        if text in item.symbol or text in item.name.upper()
    ]
    return matches


# --------------------------------------------------------------------------- #
# Quotes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Quote:
    """One observed price, with where it came from and when it was seen."""

    symbol: str
    price_usd: float
    change_pct: float | None
    source: str
    as_of: float

    def age(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.as_of)

    def tradable(self, now: float | None = None) -> bool:
        """Whether this observation is recent enough to price a real trade."""
        return self.price_usd > 0 and self.age(now) <= MAX_QUOTE_AGE_SECONDS

    def mid_coins(self) -> float:
        """The observed price in the bot's currency."""
        return self.price_usd * COINS_PER_USD

    def buy_coins(self) -> float:
        """What one unit costs a member: the observed price, exactly."""
        return self.mid_coins() * (1 + SPREAD)

    def sell_coins(self) -> float:
        """What one unit fetches a member: the observed price, exactly.

        Identical to :meth:`buy_coins` while ``SPREAD`` is zero, which is the
        whole point — a round trip is free, so a real gain is not eaten by the
        house. Both are kept as separate names because they are the two sides of
        a fill, and a card that prints them side by side would keep working if a
        spread were ever configured again.
        """
        return self.mid_coins() * (1 - SPREAD)


@dataclass(frozen=True)
class Snapshot:
    """Every quote the bot is willing to show right now."""

    quotes: Mapping[str, Quote]

    @property
    def as_of(self) -> float:
        """The newest observation in the snapshot (0 when it is empty)."""
        return max((q.as_of for q in self.quotes.values()), default=0.0)

    def age(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.as_of)

    def sources(self) -> tuple[str, ...]:
        return tuple(sorted({q.source for q in self.quotes.values()}))

    def missing(self) -> tuple[str, ...]:
        """Catalog symbols this snapshot could not price."""
        return tuple(i.symbol for i in INSTRUMENTS if i.symbol not in self.quotes)

    def quote(self, symbol: str) -> Quote | None:
        return self.quotes.get(symbol)

    def tradable(self, symbol: str, now: float | None = None) -> Quote | None:
        """The quote for ``symbol`` if it is present *and* fresh enough to trade."""
        quote = self.quotes.get(symbol)
        if quote is None or not quote.tradable(now):
            return None
        return quote


EMPTY = Snapshot(quotes={})


@dataclass(frozen=True)
class Period:
    """A chart range, and how to ask each kind of source for it."""

    key: str
    label: str
    crypto_interval: str
    crypto_limit: int
    calendar_days: int
    #: Kraken takes minutes, Yahoo takes an interval plus a range.
    kraken_interval: int = 60
    yahoo_interval: str = "1h"
    yahoo_range: str = "5d"


PERIODS: dict[str, Period] = {
    "day": Period(
        key="day",
        label="24 hours",
        crypto_interval="1h",
        crypto_limit=24,
        calendar_days=5,
        kraken_interval=60,
        yahoo_interval="1h",
        yahoo_range="1d",
    ),
    # 4-hour candles rather than 168 hourly ones: past MAX_CANDLES the chart
    # falls back to a line, and a week of hourly bodies is an illegible smear.
    "week": Period(
        key="week",
        label="7 days",
        crypto_interval="4h",
        crypto_limit=42,
        calendar_days=10,
        kraken_interval=240,
        yahoo_interval="1h",
        yahoo_range="5d",
    ),
    "month": Period(
        key="month",
        label="30 days",
        crypto_interval="1d",
        crypto_limit=30,
        calendar_days=40,
        kraken_interval=1440,
        yahoo_interval="1d",
        yahoo_range="1mo",
    ),
}
DEFAULT_PERIOD = "week"


def period_choices() -> list[str]:
    return list(PERIODS)


@dataclass(frozen=True)
class Candle:
    """One point of price history.

    OHLC when an exchange provided it. When it came from this bot's own polling
    instead, all four are the observed price, so the chart layer can tell the two
    apart (``flat``) and draw a line rather than inventing wicks nobody saw.
    """

    ts: float
    open: float
    high: float
    low: float
    close: float

    @property
    def flat(self) -> bool:
        return self.high == self.low == self.open == self.close


@dataclass(frozen=True)
class CandleSeries:
    """Price history for one instrument, and where it came from."""

    symbol: str
    candles: tuple[Candle, ...]
    source: str
    axis_label: str

    def closes(self) -> list[float]:
        return [candle.close for candle in self.candles]

    def scaled(self, factor: float) -> "CandleSeries":
        """The same series in another unit (USD to coins, for a chart).

        Charting is the only place this is needed, and getting it wrong is
        invisible in the numbers but glaring in the picture: an axis in dollars
        with a member's cost drawn in coins puts their line kilometres off the
        canvas, and stretches the price range to reach it.
        """
        return CandleSeries(
            symbol=self.symbol,
            candles=tuple(
                Candle(
                    ts=candle.ts,
                    open=candle.open * factor,
                    high=candle.high * factor,
                    low=candle.low * factor,
                    close=candle.close * factor,
                )
                for candle in self.candles
            ),
            source=self.source,
            axis_label=self.axis_label,
        )


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
class ProviderError(RuntimeError):
    """A source answered, but not with a usable price."""


def _as_number(value: Any) -> float | None:
    """Parse a number out of whatever shape a provider used.

    Providers hand back strings (``"77382.78000000"``, ``"1.75%"``), numbers, and
    placeholders like ``"N/A"`` or ``"UNCH"`` for a stock that has not printed a
    trade. Negatives and zero are legitimate here — a 24-hour *change* is very
    often negative, and rejecting those made every falling market look flat.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").replace("%", "").strip()
        try:
            number = float(cleaned)
        except ValueError:
            return None
    else:
        return None
    if number != number:  # NaN
        return None
    return number


def _as_price(value: Any) -> float | None:
    """Parse a *price*: a real number, and strictly positive.

    The distinction matters — ``_as_number`` accepts zero and negatives for a
    percentage change, while a price of zero and a placeholder are the same
    thing to a market: not a price, and never fillable.
    """
    number = _as_number(value)
    if number is None or number <= 0:
        return None
    return number


async def _get_json(
    session: aiohttp.ClientSession, url: str, *, params: Mapping[str, Any] | None = None
) -> Any:
    async with session.get(url, params=params) as resp:
        if resp.status != 200:
            raise ProviderError(f"HTTP {resp.status} from {url}")
        try:
            return await resp.json(content_type=None)
        except Exception as exc:  # a 200 that is not JSON is still a failure
            raise ProviderError(f"unreadable JSON from {url}: {exc}") from exc


async def _post_json(session: aiohttp.ClientSession, url: str, payload: Any) -> Any:
    async with session.post(url, json=payload) as resp:
        if resp.status != 200:
            raise ProviderError(f"HTTP {resp.status} from {url}")
        try:
            return await resp.json(content_type=None)
        except Exception as exc:
            raise ProviderError(f"unreadable JSON from {url}: {exc}") from exc


async def _binance(
    session: aiohttp.ClientSession, instruments: list[Instrument]
) -> dict[str, tuple[float, float | None]]:
    """Binance's public 24-hour ticker: one batched call for every crypto pair.

    Shape: ``[{"symbol": "BTCUSDT", "lastPrice": "77372.90",
    "priceChangePercent": "0.166"}, ...]``
    """
    wanted = {
        item.id_for("binance"): item.symbol for item in instruments if item.id_for("binance")
    }
    if not wanted:
        return {}
    payload = await _get_json(
        session,
        "https://api.binance.com/api/v3/ticker/24hr",
        params={"symbols": "[" + ",".join(f'"{pair}"' for pair in wanted) + "]"},
    )
    if not isinstance(payload, list):
        raise ProviderError("binance returned something other than a list")
    out: dict[str, tuple[float, float | None]] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        symbol = wanted.get(str(row.get("symbol", "")))
        price = _as_price(row.get("lastPrice"))
        if symbol and price is not None:
            out[symbol] = (price, _as_number(row.get("priceChangePercent")))
    return out


def kraken_aliases(identifier: str) -> set[str]:
    """Every key Kraken might use for one pair.

    Kraken's legacy naming returns ``XXBTZUSD`` and ``XETHZUSD`` for the pairs
    requested as ``XBTUSD`` and ``ETHUSD``, and the same API returns plain
    ``SOLUSD`` for newer listings — so the pair has to be matched, not looked up.
    """
    base = identifier.upper()
    variants = {base, base.replace("XBT", "BTC")}
    for variant in list(variants):
        variants.add("X" + variant)
        variants.add(variant.replace("USD", "ZUSD"))
        variants.add("X" + variant.replace("USD", "ZUSD"))
    return variants


async def _kraken(
    session: aiohttp.ClientSession, instruments: list[Instrument]
) -> dict[str, tuple[float, float | None]]:
    """Kraken's public ticker: one call for every pair.

    Shape: ``{"error": [], "result": {"XETHZUSD": {"c": ["2511.12", ...]}}}``
    where ``c`` is the last trade's closing price.
    """
    pairs = [item.id_for("kraken") for item in instruments if item.id_for("kraken")]
    if not pairs:
        return {}
    payload = await _get_json(
        session,
        "https://api.kraken.com/0/public/Ticker",
        params={"pair": ",".join(pairs)},
    )
    if not isinstance(payload, dict):
        raise ProviderError("kraken returned something other than an object")
    if payload.get("error"):
        raise ProviderError(f"kraken reported {payload['error']}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ProviderError("kraken returned no result")

    # Build the alias index once: response key -> our symbol.
    index: dict[str, str] = {}
    for item in instruments:
        identifier = item.id_for("kraken")
        if identifier:
            for alias in kraken_aliases(identifier):
                index.setdefault(alias, item.symbol)

    out: dict[str, tuple[float, float | None]] = {}
    for key, row in result.items():
        symbol = index.get(str(key).upper())
        if symbol is None or not isinstance(row, dict):
            continue
        close = row.get("c")
        price = _as_price(close[0] if isinstance(close, list) and close else close)
        if price is not None:
            out[symbol] = (price, None)
    return out


async def _coinbase(
    session: aiohttp.ClientSession, instruments: list[Instrument]
) -> dict[str, tuple[float, float | None]]:
    """Coinbase's public product ticker, one request per product.

    The last resort for crypto — per-symbol, but it does not need a key and it
    answers with a real trade (``{"price": "77348.1", ...}``).
    """
    out: dict[str, tuple[float, float | None]] = {}
    for item in instruments:
        product = item.id_for("coinbase")
        if not product:
            continue
        payload = await _get_json(
            session, f"https://api.exchange.coinbase.com/products/{product}/ticker"
        )
        if not isinstance(payload, dict):
            continue
        price = _as_price(payload.get("price"))
        if price is not None:
            out[item.symbol] = (price, None)
    return out


async def _tradingview(
    session: aiohttp.ClientSession, instruments: list[Instrument]
) -> dict[str, tuple[float, float | None]]:
    """TradingView's public scanner: every stock in one call, with its change.

    Shape: ``{"data": [{"s": "NASDAQ:AAPL", "d": [332.27, 1.745, "AAPL"]}]}``
    where the columns are the ones asked for below.
    """
    wanted = {
        item.id_for("tradingview"): item.symbol
        for item in instruments
        if item.id_for("tradingview")
    }
    if not wanted:
        return {}
    payload = await _post_json(
        session,
        "https://scanner.tradingview.com/america/scan",
        {
            "symbols": {"tickers": list(wanted), "query": {"types": []}},
            "columns": ["close", "change", "name"],
        },
    )
    if not isinstance(payload, dict):
        raise ProviderError("tradingview returned something other than an object")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ProviderError("tradingview returned no data")
    out: dict[str, tuple[float, float | None]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = wanted.get(str(row.get("s", "")))
        values = row.get("d")
        if symbol is None or not isinstance(values, list) or not values:
            continue
        price = _as_price(values[0])
        change = _as_number(values[1]) if len(values) > 1 else None
        if price is not None:
            out[symbol] = (price, change)
    return out


async def _cnbc(
    session: aiohttp.ClientSession, instruments: list[Instrument]
) -> dict[str, tuple[float, float | None]]:
    """CNBC's quote service: every stock in one call.

    Shape: ``{"FormattedQuoteResult": {"FormattedQuote": [{"symbol": "AAPL",
    "last": "332.27", "change_pct": "1.75%"}]}}``
    """
    wanted = {item.id_for("cnbc"): item.symbol for item in instruments if item.id_for("cnbc")}
    if not wanted:
        return {}
    payload = await _get_json(
        session,
        "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol",
        params={
            "symbols": "|".join(wanted),
            "requestMethod": "itv",
            "noform": "1",
            "partnerId": "2",
            "fund": "1",
            "exthrs": "1",
            "output": "json",
        },
    )
    if not isinstance(payload, dict):
        raise ProviderError("cnbc returned something other than an object")
    result = payload.get("FormattedQuoteResult")
    rows = result.get("FormattedQuote") if isinstance(result, dict) else None
    if rows is None:
        raise ProviderError("cnbc returned no quotes")
    if isinstance(rows, dict):  # a single symbol comes back unwrapped
        rows = [rows]
    out: dict[str, tuple[float, float | None]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = wanted.get(str(row.get("symbol", "")).upper())
        price = _as_price(row.get("last"))
        if symbol and price is not None:
            out[symbol] = (price, _as_number(row.get("change_pct")))
    return out


#: ``{provider: fetcher}`` — each takes the instruments it should price and
#: returns ``{symbol: (price_usd, change_pct)}``, omitting anything it could not.
SOURCES: dict[str, Callable[[aiohttp.ClientSession, list[Instrument]], Awaitable[dict]]] = {
    "binance": _binance,
    "kraken": _kraken,
    "coinbase": _coinbase,
    "tradingview": _tradingview,
    "cnbc": _cnbc,
}
#: Tried in order, and the next one is asked only for what is still missing, so
#: a source that half-answers is topped up rather than trusted or discarded.
CRYPTO_SOURCES = ("binance", "kraken", "coinbase")
STOCK_SOURCES = ("tradingview", "cnbc")


async def _binance_klines(
    session: aiohttp.ClientSession, instrument: Instrument, period: Period, now: float
) -> list[Candle]:
    """Real OHLC from Binance's public klines endpoint.

    Shape: ``[[open_time_ms, open, high, low, close, volume, close_time, ...]]``
    with every number as a string.
    """
    pair = instrument.id_for("binance")
    if not pair:
        raise ProviderError(f"binance has no pair for {instrument.symbol}")
    payload = await _get_json(
        session,
        "https://api.binance.com/api/v3/klines",
        params={"symbol": pair, "interval": period.crypto_interval, "limit": period.crypto_limit},
    )
    if not isinstance(payload, list):
        raise ProviderError("binance klines returned something other than a list")
    candles: list[Candle] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 5:
            continue
        open_, high, low, close = (_as_price(row[i]) for i in (1, 2, 3, 4))
        stamp = _as_number(row[0])
        if None in (open_, high, low, close) or stamp is None:
            continue
        candles.append(
            Candle(
                ts=float(stamp) / 1000.0,
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
            )
        )
    if not candles:
        raise ProviderError("binance klines contained no usable candles")
    return candles


async def _nasdaq_chart(
    session: aiohttp.ClientSession, instrument: Instrument, period: Period, now: float
) -> list[Candle]:
    """Daily OHLC from Nasdaq's public chart endpoint (the stock fallback).

    Shape: ``{"data": {"chart": [{"x": ms, "y": close,
    "z": {"open": "327.45", "high": ..., "low": ..., "close": ...}}]}}``
    """
    start = _day(now - period.calendar_days * 24 * 3600)
    end = _day(now)
    payload = await _get_json(
        session,
        f"https://api.nasdaq.com/api/quote/{instrument.symbol}/chart",
        params={
            "assetclass": instrument.id_for("nasdaq_class") or "stocks",
            "fromdate": start,
            "todate": end,
        },
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("chart") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ProviderError("nasdaq returned no chart")
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        block = row.get("z") if isinstance(row.get("z"), dict) else {}
        stamp = _as_number(row.get("x"))
        close = _as_price(block.get("close")) or _as_price(row.get("y"))
        if stamp is None or close is None:
            continue
        candles.append(
            Candle(
                ts=float(stamp) / 1000.0,
                open=_as_price(block.get("open")) or close,
                high=_as_price(block.get("high")) or close,
                low=_as_price(block.get("low")) or close,
                close=close,
            )
        )
    if not candles:
        raise ProviderError("nasdaq chart contained no usable candles")
    return candles


async def _kraken_ohlc(
    session: aiohttp.ClientSession, instrument: Instrument, period: Period, now: float
) -> list[Candle]:
    """Real OHLC from Kraken's public endpoint, for hosts Binance refuses.

    Shape: ``{"result": {"XBTUSD": [[time, open, high, low, close, vwap,
    volume, count], ...], "last": ...}}``
    """
    pair = instrument.id_for("kraken")
    if not pair:
        raise ProviderError(f"kraken has no pair for {instrument.symbol}")
    payload = await _get_json(
        session,
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": pair, "interval": period.kraken_interval},
    )
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise ProviderError("kraken OHLC returned no result")
    alias_index = {alias: instrument.symbol for alias in kraken_aliases(pair)}
    rows = None
    for key, value in result.items():
        if key != "last" and alias_index.get(str(key).upper()) == instrument.symbol:
            rows = value
            break
    if not isinstance(rows, list):
        raise ProviderError(f"kraken OHLC had no series for {instrument.symbol}")
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            continue
        stamp = _as_number(row[0])
        open_, high, low, close = (_as_price(row[i]) for i in (1, 2, 3, 4))
        if stamp is None or None in (open_, high, low, close):
            continue
        candles.append(
            Candle(
                ts=float(stamp),
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
            )
        )
    if not candles:
        raise ProviderError("kraken OHLC contained no usable candles")
    return candles


async def _yahoo_chart(
    session: aiohttp.ClientSession, instrument: Instrument, period: Period, now: float
) -> list[Candle]:
    """Real OHLC from Yahoo's public chart endpoint.

    Shape: ``{"chart": {"result": [{"timestamp": [...], "indicators":
    {"quote": [{"open": [...], "high": [...], "low": [...],
    "close": [...]}]}}]}}`` with nulls where no trade printed.
    """
    payload = await _get_json(
        session,
        f"https://query1.finance.yahoo.com/v8/finance/chart/{instrument.symbol}",
        params={"interval": period.yahoo_interval, "range": period.yahoo_range},
    )
    chart = payload.get("chart") if isinstance(payload, dict) else None
    results = chart.get("result") if isinstance(chart, dict) else None
    if not isinstance(results, list) or not results:
        raise ProviderError(f"yahoo returned no chart for {instrument.symbol}")
    block = results[0] if isinstance(results[0], dict) else {}
    stamps = block.get("timestamp")
    quotes = block.get("indicators", {}).get("quote") if isinstance(block.get("indicators"), dict) else None
    if not isinstance(stamps, list) or not isinstance(quotes, list) or not quotes:
        raise ProviderError("yahoo chart was missing its series")
    series = quotes[0] if isinstance(quotes[0], dict) else {}
    candles: list[Candle] = []
    for index, stamp in enumerate(stamps):
        moment = _as_number(stamp)
        close = _as_price(_pick(series.get("close"), index))
        if moment is None or close is None:
            continue
        candles.append(
            Candle(
                ts=float(moment),
                open=_as_price(_pick(series.get("open"), index)) or close,
                high=_as_price(_pick(series.get("high"), index)) or close,
                low=_as_price(_pick(series.get("low"), index)) or close,
                close=close,
            )
        )
    if not candles:
        raise ProviderError("yahoo chart contained no usable candles")
    return candles


def _pick(values, index: int):
    """One element of a provider's parallel arrays, or None if it is missing."""
    if not isinstance(values, list) or index >= len(values):
        return None
    return values[index]


def _day(ts: float) -> str:
    """``YYYY-MM-DD`` in UTC, which is what Nasdaq's chart range wants."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


async def fetch_candles(
    instrument: Instrument, period_key: str = DEFAULT_PERIOD, *, now: float | None = None
) -> CandleSeries | None:
    """Price history for one instrument, or ``None`` if there is none to draw.

    The exchange's own history comes first — real OHLC. What this bot has
    observed itself is the fallback, which is the only history that exists at all
    for a source with no chart endpoint, and it is labelled as observations so a
    line of polled closes is never presented as a candle chart.
    """
    now = time.time() if now is None else now
    period = PERIODS.get(period_key) or PERIODS[DEFAULT_PERIOD]
    timeout = aiohttp.ClientTimeout(total=HISTORY_TIMEOUT_SECONDS)
    candles: list[Candle] = []
    source = ""
    axis = ""

    if instrument.kind == CRYPTO:
        chain = (
            ("binance", _binance_klines, "Binance", f"{period.crypto_interval} candles"),
            ("kraken", _kraken_ohlc, "Kraken", f"{period.kraken_interval // 60} h candles"),
        )
    else:
        chain = (
            ("yahoo", _yahoo_chart, "Yahoo Finance", period.yahoo_interval + " candles"),
            ("nasdaq", _nasdaq_chart, "Nasdaq", "daily candles"),
        )

    async with aiohttp.ClientSession(headers=HISTORY_HEADERS, timeout=timeout) as session:
        for name, fetcher, label, axis_label in chain:
            try:
                candles = await fetcher(session, instrument, period, now)
            except Exception as exc:
                print(f"[MARKET] {name} history for {instrument.symbol}: {exc}", flush=True)
                continue
            if len(candles) >= 2:
                source, axis = label, axis_label
                break
            candles = []

    if len(candles) >= 2:
        return CandleSeries(
            symbol=instrument.symbol, candles=tuple(candles), source=source, axis_label=axis
        )

    observed = await observed_series(instrument.symbol, period.calendar_days * 24 * 3600, now)
    if len(observed) >= 2:
        return CandleSeries(
            symbol=instrument.symbol,
            candles=tuple(observed),
            source="this bot's own polling",
            axis_label="observed prices",
        )
    return None


# --------------------------------------------------------------------------- #
# The live page
# --------------------------------------------------------------------------- #
#: How long a chart's candles are reused for one browser tab. The page polls, and
#: every poll would otherwise be an outbound provider request, so the cache is the
#: difference between a chart somebody leaves open and a rate limit.
CANDLE_CACHE_SECONDS = 60.0
_candles_cache: dict[tuple[str, str], tuple[float, CandleSeries]] = {}


async def cached_candles(
    symbol: str, period_key: str = DEFAULT_PERIOD, *, now: float | None = None
) -> CandleSeries | None:
    """Candles for a symbol, served from a short cache when one is warm."""
    instrument = BY_SYMBOL.get((symbol or "").strip().upper())
    if instrument is None:
        return None
    key = (instrument.symbol, period_key if period_key in PERIODS else DEFAULT_PERIOD)
    now = time.time() if now is None else now
    hit = _candles_cache.get(key)
    if hit is not None and now - hit[0] < CANDLE_CACHE_SECONDS:
        return hit[1]
    series = await fetch_candles(instrument, key[1], now=now)
    if series is not None:
        _candles_cache[key] = (now, series)
    return series


def quotes_payload(snap: Snapshot, now: float | None = None) -> dict:
    """The whole board as JSON, for the live page.

    Every number carries its own age and source, exactly like the card: a live
    page that hides how old a price is would be inventing certainty.
    """
    now = time.time() if now is None else now
    rows = []
    for instrument in INSTRUMENTS:
        quote = snap.quote(instrument.symbol)
        row: dict[str, Any] = {
            "symbol": instrument.symbol,
            "name": instrument.name,
            "kind": instrument.kind,
            "available": quote is not None,
        }
        if quote is not None:
            row.update(
                price_usd=quote.price_usd,
                coins=quote.mid_coins(),
                change_pct=quote.change_pct,
                source=quote.source,
                age_seconds=round(quote.age(now), 1),
                tradable=quote.tradable(now),
            )
        rows.append(row)
    return {
        "as_of": snap.as_of,
        "age_seconds": round(snap.age(now), 1),
        "sources": list(snap.sources()),
        "missing": list(snap.missing()),
        "coins_per_usd": COINS_PER_USD,
        "treat_fee": MEOW_TREAT_FEE,
        "treat_fee_label": treat_fee_label(),
        "max_quote_age_seconds": MAX_QUOTE_AGE_SECONDS,
        "periods": {
            key: {"label": period.label, "days": period.calendar_days}
            for key, period in PERIODS.items()
        },
        "quotes": rows,
    }


def series_payload(
    symbol: str, series: CandleSeries | None, period_key: str = DEFAULT_PERIOD
) -> dict:
    """One instrument's history as JSON, **in coins**, and where it came from.

    Coins rather than dollars because coins are what the page draws and what a
    member trades in; ``coins_per_usd`` travels with the quotes payload for
    anyone who wants the dollar figure back.
    """
    instrument = BY_SYMBOL.get((symbol or "").strip().upper())
    payload: dict[str, Any] = {
        "symbol": (instrument.symbol if instrument else (symbol or "").upper()),
        "name": instrument.name if instrument else None,
        "kind": instrument.kind if instrument else None,
        "period": period_key,
        "source": series.source if series else None,
        "axis_label": series.axis_label if series else None,
        "observed": bool(series and series.source == "this bot's own polling"),
        "candles": [],
    }
    if series is not None:
        payload["candles"] = [
            {
                "ts": candle.ts,
                "open": candle.open * COINS_PER_USD,
                "high": candle.high * COINS_PER_USD,
                "low": candle.low * COINS_PER_USD,
                "close": candle.close * COINS_PER_USD,
                "flat": candle.flat,
            }
            for candle in series.candles
        ]
    return payload


# --------------------------------------------------------------------------- #
# Fetching and caching
# --------------------------------------------------------------------------- #
_snapshot: Snapshot | None = None
_loaded_from_db = False
_refreshing = False
_last_attempt = 0.0
#: The in-flight guard is a flag rather than an ``asyncio.Lock``: a module-level
#: Lock binds to the first event loop that awaits it, which breaks every test
#: that runs on a fresh loop, and the work being guarded is single-threaded I/O.
_last_error: str | None = None


def reset() -> None:
    """Forget the cached snapshot. For tests and for a deliberate re-read."""
    global _snapshot, _loaded_from_db, _refreshing, _last_attempt, _last_error
    _snapshot = None
    _loaded_from_db = False
    _refreshing = False
    _last_attempt = 0.0
    _last_error = None
    _candles_cache.clear()


def last_error() -> str | None:
    """Why the most recent refresh came back short, if it did."""
    return _last_error


async def _fetch(
    now: float, instruments: tuple[Instrument, ...] = INSTRUMENTS
) -> Snapshot:
    """Ask each provider chain for prices, filling gaps from the fallbacks."""
    quotes: dict[str, Quote] = {}
    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(headers=HTTP_HEADERS, timeout=timeout) as session:
            for kind, chain in ((CRYPTO, CRYPTO_SOURCES), (STOCK, STOCK_SOURCES)):
                pending = [i for i in instruments if i.kind == kind and i.symbol not in quotes]
                for name in chain:
                    if not pending:
                        break
                    try:
                        got = await SOURCES[name](session, pending)
                    except Exception as exc:
                        # A blocked or broken source is not a market failure: the
                        # chain moves on, and if nothing answers the trade gate
                        # refuses the fill rather than inventing a price.
                        print(f"[MARKET] {name} unavailable: {exc}", flush=True)
                        continue
                    for symbol, (price, change) in got.items():
                        if symbol in quotes:
                            continue
                        quotes[symbol] = Quote(
                            symbol=symbol,
                            price_usd=float(price),
                            change_pct=change,
                            source=name,
                            as_of=now,
                        )
                    pending = [i for i in pending if i.symbol not in quotes]
    except Exception as exc:  # noqa: BLE001 - the client session itself failed
        print(f"[MARKET] refresh failed: {exc}", flush=True)
    return Snapshot(quotes=quotes)


async def _load_cached() -> Snapshot | None:
    """The board as it was last written to disk, if it was."""
    try:
        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute(
                "SELECT symbol, price_usd, change_pct, source, as_of FROM market_quotes"
            ) as cur:
                rows = await cur.fetchall()
    except Exception as exc:
        print(f"[MARKET] could not read the saved board: {exc}", flush=True)
        return None
    quotes: dict[str, Quote] = {}
    for symbol, price_usd, change_pct, source, as_of in rows:
        price = _as_price(price_usd)
        if price is None or symbol not in BY_SYMBOL:
            continue
        quotes[symbol] = Quote(
            symbol=symbol,
            price_usd=price,
            change_pct=None if change_pct is None else float(change_pct),
            source=str(source or "saved"),
            as_of=float(as_of or 0.0),
        )
    return Snapshot(quotes=quotes) if quotes else None


async def _save(snapshot: Snapshot) -> None:
    """Persist the board, and record it as an observation for the charts.

    Both writes share one connection: the board is what a restart reads back, and
    ``market_history`` is the series the charts fall back to for instruments whose
    source publishes no chart of its own.
    """
    if not snapshot.quotes:
        return
    try:
        async with aiosqlite.connect(db.DB_PATH) as conn:
            cutoff = snapshot.as_of - HISTORY_RETENTION_DAYS * 24 * 3600
            for quote in snapshot.quotes.values():
                await conn.execute(
                    "INSERT INTO market_history (symbol, ts, price_usd, source) VALUES (?, ?, ?, ?)",
                    (quote.symbol, quote.as_of, quote.price_usd, quote.source),
                )
            await conn.execute("DELETE FROM market_history WHERE ts < ?", (cutoff,))
            for quote in snapshot.quotes.values():
                await conn.execute(
                    """INSERT INTO market_quotes (symbol, price_usd, change_pct, source, as_of)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(symbol) DO UPDATE SET
                           price_usd=excluded.price_usd,
                           change_pct=excluded.change_pct,
                           source=excluded.source,
                           as_of=excluded.as_of""",
                    (
                        quote.symbol,
                        quote.price_usd,
                        quote.change_pct,
                        quote.source,
                        quote.as_of,
                    ),
                )
            await conn.commit()
    except Exception as exc:
        print(f"[MARKET] could not save the board: {exc}", flush=True)


async def snapshot(now: float | None = None, *, force: bool = False) -> Snapshot:
    """The current board, fetched if it is missing or older than the TTL.

    A symbol that fails this round keeps its previous quote — with its original
    timestamp, so the age printed on it stays honest and an old price can never
    be mistaken for a fresh one.
    """
    global _snapshot, _loaded_from_db, _refreshing, _last_attempt, _last_error
    now = time.time() if now is None else now

    if _snapshot is None and not _loaded_from_db:
        _loaded_from_db = True
        _snapshot = await _load_cached()

    if not force and _snapshot is not None and _snapshot.age(now) < QUOTE_TTL_SECONDS:
        return _snapshot
    if _refreshing:
        # Another command is already fetching; show what we have rather than
        # stacking up identical requests.
        return _snapshot or EMPTY
    if force and now - _last_attempt < REFRESH_MIN_INTERVAL_SECONDS:
        # A forced refresh a moment after the last one buys nothing but load.
        return _snapshot or EMPTY

    _refreshing = True
    _last_attempt = now
    try:
        fresh = await _fetch(now)
        if fresh.quotes:
            merged = dict(_snapshot.quotes) if _snapshot else {}
            merged.update(fresh.quotes)
            _snapshot = Snapshot(quotes=merged)
            _last_error = None
            await _save(fresh)
        else:
            _last_error = "every price source was unreachable"
            print(f"[MARKET] ⚠️ no prices this cycle ({_last_error}).", flush=True)
    finally:
        _refreshing = False
    return _snapshot or EMPTY


# --------------------------------------------------------------------------- #
# Order maths
# --------------------------------------------------------------------------- #
def buy_quantity(coins: int, unit_price_coins: float) -> float:
    """Units bought for ``coins`` at ``unit_price_coins`` each."""
    if unit_price_coins <= 0 or coins <= 0:
        return 0.0
    return round(coins / unit_price_coins, QUANTITY_DECIMALS)


def sell_proceeds(quantity: float, unit_price_coins: float) -> int:
    """Coins received for ``quantity`` units at ``unit_price_coins`` each."""
    if unit_price_coins <= 0 or quantity <= 0:
        return 0
    return int(round(quantity * unit_price_coins))


def sell_value(quantity: float, unit_price_coins: float) -> int:
    """What selling ``quantity`` at ``unit_price_coins`` actually pays, treats out.

    The one function the cards should call: a position's worth is what you would
    receive, not the gross price times the size, and the difference is the treat.
    """
    gross = sell_proceeds(quantity, unit_price_coins)
    return max(0, gross - treat_fee(gross))


def cost_basis_portion(cost_coins: float, quantity: float, sold: float) -> float:
    """The share of the position's cost that ``sold`` represents.

    Closing the whole position takes the whole basis, so rounding can never
    leave a phantom fraction of cost behind on an empty holding.
    """
    if quantity <= 0:
        return 0.0
    if sold >= quantity:
        return float(cost_coins)
    return float(cost_coins) * (sold / quantity)


@dataclass(frozen=True)
class Fill:
    """The result of a settled trade, ready to render."""

    ok: bool
    reason: str = ""
    symbol: str = ""
    side: str = ""
    quantity: float = 0.0
    unit_price_coins: float = 0.0
    coins: int = 0
    realized: int = 0
    quantity_after: float = 0.0
    cost_after: float = 0.0
    #: The Meow Treat fee taken on this fill. Also included in ``coins`` (a buy
    #: spends it, a sell pays it out of the proceeds) and kept separately so every
    #: receipt can itemise it instead of hiding it.
    treat_coins: int = 0

    @property
    def avg_cost_coins(self) -> float:
        """What the remaining position cost per unit, or 0 when it is closed."""
        if self.quantity_after <= 0:
            return 0.0
        return self.cost_after / self.quantity_after


@dataclass(frozen=True)
class Holding:
    """A member's position in one instrument."""

    symbol: str
    quantity: float
    cost_coins: float

    @property
    def avg_cost_coins(self) -> float:
        return self.cost_coins / self.quantity if self.quantity > 0 else 0.0


# --------------------------------------------------------------------------- #
# Trading limits
# --------------------------------------------------------------------------- #
_trades: dict[tuple[int, int], float] = {}


def trade_cooldown_remaining(
    user_id: int, guild_id: int, now: float | None = None
) -> float:
    """Seconds this member must wait before their next fill (0 if free)."""
    now = time.time() if now is None else now
    last = _trades.get((user_id, guild_id))
    if last is None:
        return 0.0
    return max(0.0, last + TRADE_COOLDOWN_SECONDS - now)


def note_trade(user_id: int, guild_id: int, now: float | None = None) -> None:
    """Record a fill, dropping entries that can no longer block anyone."""
    now = time.time() if now is None else now
    for key in [kv[0] for kv in _trades.items() if kv[1] + TRADE_COOLDOWN_SECONDS <= now]:
        _trades.pop(key, None)
    _trades[(user_id, guild_id)] = now


def clear_trade_limits() -> None:
    """Forget recent fills. For tests, and after restoring a database."""
    _trades.clear()


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
async def holdings(user_id: int, guild_id: int) -> dict[str, Holding]:
    """Every position this member holds, dust excluded."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT symbol, quantity, cost_coins FROM market_holdings "
            "WHERE user_id=? AND guild_id=? AND quantity > 0",
            (user_id, guild_id),
        ) as cur:
            rows = await cur.fetchall()
    return {
        str(symbol): Holding(str(symbol), float(quantity or 0.0), float(cost or 0.0))
        for symbol, quantity, cost in rows
    }


async def trade_history(user_id: int, guild_id: int, limit: int = 10) -> list[dict]:
    """This member's most recent fills, newest first."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT symbol, side, quantity, unit_price_coins, coins, realized, ts "
            "FROM market_trades WHERE user_id=? AND guild_id=? ORDER BY id DESC LIMIT ?",
            (user_id, guild_id, int(limit)),
        ) as cur:
            rows = await cur.fetchall()
    return [
        {
            "symbol": str(symbol),
            "side": str(side),
            "quantity": float(quantity or 0.0),
            "unit_price_coins": float(price or 0.0),
            "coins": int(coins or 0),
            "realized": int(realized or 0),
            "ts": float(ts or 0.0),
        }
        for symbol, side, quantity, price, coins, realized, ts in rows
    ]


async def observed_series(
    symbol: str, seconds: float, now: float, limit: int = MAX_OBSERVED_POINTS
) -> list[Candle]:
    """Price history this bot observed itself, oldest first.

    Each observation becomes a flat candle (open = high = low = close), which the
    chart layer renders as a line — the honest shape for a series of polled
    prices. Points are thinned by stride rather than by dropping the ends, so the
    shape of the window survives.
    """
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT ts, price_usd FROM market_history WHERE symbol=? AND ts >= ? ORDER BY ts",
            (symbol, now - seconds),
        ) as cur:
            rows = await cur.fetchall()
    points = [(float(ts), float(price)) for ts, price in rows if float(price or 0) > 0]
    if len(points) > limit:
        # Thinned evenly, and both ends are kept: a chart whose last point is not
        # the latest observation would sit next to a header showing a different
        # price and quietly disagree with it.
        stride = (len(points) - 1) / (limit - 1)
        points = [
            points[min(len(points) - 1, round(index * stride))] for index in range(limit)
        ]
    return [Candle(ts=ts, open=price, high=price, low=price, close=price) for ts, price in points]


async def realized_total(user_id: int, guild_id: int) -> dict:
    """All-time realised profit/loss from sells, plus how often it was positive."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT COALESCE(SUM(realized), 0), COUNT(*), "
            "COALESCE(SUM(CASE WHEN realized > 0 THEN 1 ELSE 0 END), 0) "
            "FROM market_trades WHERE user_id=? AND guild_id=? AND side='sell'",
            (user_id, guild_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {"realized": 0, "sales": 0, "wins": 0}
    return {"realized": int(row[0] or 0), "sales": int(row[1] or 0), "wins": int(row[2] or 0)}


async def sold_coins_since(user_id: int, guild_id: int, since: float) -> int:
    """Coins the market has paid this member since ``since`` (for the daily cap)."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT COALESCE(SUM(coins), 0) FROM market_trades "
            "WHERE user_id=? AND guild_id=? AND side='sell' AND ts >= ?",
            (user_id, guild_id, float(since)),
        ) as cur:
            row = await cur.fetchone()
    return int(row[0] or 0) if row else 0


def _holdings_on(cur, user_id: int, guild_id: int, symbol: str) -> tuple[float, float]:
    """Read one position on a synchronous cursor: ``(quantity, cost_coins)``."""
    cur.execute(
        "SELECT quantity, cost_coins FROM market_holdings "
        "WHERE user_id=? AND guild_id=? AND symbol=?",
        (user_id, guild_id, symbol),
    )
    row = cur.fetchone()
    if row is None:
        return 0.0, 0.0
    return float(row[0] or 0.0), float(row[1] or 0.0)


def _write_holding_on(
    cur, user_id: int, guild_id: int, symbol: str, quantity: float, cost_coins: float
) -> None:
    """Upsert one position on a synchronous cursor."""
    cur.execute(
        """INSERT INTO market_holdings (user_id, guild_id, symbol, quantity, cost_coins)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id, guild_id, symbol) DO UPDATE SET
               quantity=excluded.quantity,
               cost_coins=excluded.cost_coins""",
        (user_id, guild_id, symbol, quantity, cost_coins),
    )


def _record_on(
    cur,
    user_id: int,
    guild_id: int,
    symbol: str,
    side: str,
    quantity: float,
    unit_price_coins: float,
    coins: int,
    realized: int,
    now: float,
) -> None:
    """Append one fill to the trade log on a synchronous cursor."""
    cur.execute(
        """INSERT INTO market_trades
               (user_id, guild_id, symbol, side, quantity, unit_price_coins, coins, realized, ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, guild_id, symbol, side, quantity, unit_price_coins, coins, realized, now),
    )


async def settle_buy(
    user_id: int, guild_id: int, instrument: Instrument, coins: int, quote: Quote, now: float
) -> Fill:
    """Buy ``coins`` worth of ``instrument`` at ``quote``'s ask, atomically.

    The debit is conditional and the holding is written in the same transaction,
    so two simultaneous buys cannot both be served from a wallet that covers one,
    and a fill can never be recorded without the coins having moved.
    """
    if coins < MIN_TRADE_COINS:
        return Fill(False, "too_small", instrument.symbol)
    if coins > MAX_TRADE_COINS:
        return Fill(False, "too_large", instrument.symbol)
    if not quote.tradable(now):
        return Fill(False, "stale", instrument.symbol)

    unit_price = quote.buy_coins()
    # The member names what leaves their wallet; the treat comes out of it first,
    # so the position is bought with the rest. The whole outlay — treats included —
    # becomes the cost basis, which is what makes closing at an unchanged price
    # show exactly the treat as a loss rather than a free round trip.
    treat = treat_fee(coins)
    notional = coins - treat
    if notional <= 0:
        return Fill(False, "too_small", instrument.symbol)
    quantity = buy_quantity(notional, unit_price)
    if quantity <= 0:
        return Fill(False, "too_small", instrument.symbol)

    def _buy(cur) -> Fill:
        cur.execute(
            "UPDATE economy SET wallet = wallet - ? "
            "WHERE user_id=? AND guild_id=? AND wallet >= ?",
            (coins, user_id, guild_id, coins),
        )
        if cur.rowcount != 1:
            return Fill(False, "funds", instrument.symbol)
        held, cost = _holdings_on(cur, user_id, guild_id, instrument.symbol)
        new_quantity = round(held + quantity, QUANTITY_DECIMALS)
        new_cost = float(cost) + coins
        _write_holding_on(cur, user_id, guild_id, instrument.symbol, new_quantity, new_cost)
        _record_on(
            cur, user_id, guild_id, instrument.symbol, "buy", quantity, unit_price, coins, 0, now
        )
        # Bank the treat fee into Meow Bank, inside the same transaction.
        meowfee.credit(cur, guild_id, treat)

        return Fill(
            True,
            symbol=instrument.symbol,
            side="buy",
            quantity=quantity,
            unit_price_coins=unit_price,
            coins=coins,
            quantity_after=new_quantity,
            cost_after=new_cost,
            treat_coins=treat,
        )

    return await asyncio.to_thread(db.run_atomic, _buy)


async def settle_sell(
    user_id: int,
    guild_id: int,
    instrument: Instrument,
    quantity: float,
    quote: Quote,
    now: float,
    *,
    sell_all: bool = False,
) -> Fill:
    """Sell ``quantity`` units of ``instrument`` at ``quote``'s bid, atomically.

    The holding is debited conditionally on still holding that much, and the
    rolling daily ceiling is counted *inside* the same transaction, so a burst of
    simultaneous sells can neither oversell a position nor each pass the cap on
    its own.
    """
    if not quote.tradable(now):
        return Fill(False, "stale", instrument.symbol)

    unit_price = quote.sell_coins()

    def _sell(cur) -> Fill:
        held, cost = _holdings_on(cur, user_id, guild_id, instrument.symbol)
        if held <= 0:
            return Fill(False, "none", instrument.symbol)
        wanted = held if sell_all else quantity
        if wanted <= 0:
            return Fill(False, "too_small", instrument.symbol)
        if wanted > held:
            return Fill(False, "too_many", instrument.symbol)
        if held - wanted < DUST_QUANTITY:
            # Selling all but a crumb would leave an untradable position behind.
            wanted = held

        gross = sell_proceeds(wanted, unit_price)
        treat = treat_fee(gross)
        proceeds = gross - treat
        if proceeds < 1:
            return Fill(False, "too_small", instrument.symbol)

        cur.execute(
            "SELECT COALESCE(SUM(coins), 0) FROM market_trades "
            "WHERE user_id=? AND guild_id=? AND side='sell' AND ts >= ?",
            (user_id, guild_id, now - 24 * 3600),
        )
        row = cur.fetchone()
        sold_today = int(row[0] or 0) if row else 0
        if sold_today + proceeds > MAX_DAILY_SELL_COINS:
            return Fill(False, "daily_cap", instrument.symbol)

        basis = cost_basis_portion(cost, held, wanted)
        new_quantity = round(held - wanted, QUANTITY_DECIMALS)
        if new_quantity < DUST_QUANTITY:
            new_quantity, new_cost = 0.0, 0.0
        else:
            new_cost = max(0.0, float(cost) - basis)
        realized = proceeds - int(round(basis))

        cur.execute(
            "UPDATE market_holdings SET quantity=?, cost_coins=? "
            "WHERE user_id=? AND guild_id=? AND symbol=? AND quantity >= ?",
            (new_quantity, new_cost, user_id, guild_id, instrument.symbol, wanted),
        )
        if cur.rowcount != 1:
            # The position moved while this transaction was being built.
            return Fill(False, "changed", instrument.symbol)
        cur.execute(
            "UPDATE economy SET wallet = wallet + ? WHERE user_id=? AND guild_id=?",
            (proceeds, user_id, guild_id),
        )
        _record_on(
            cur,
            user_id,
            guild_id,
            instrument.symbol,
            "sell",
            wanted,
            unit_price,
            proceeds,
            realized,
            now,
        )
        # Bank the treat fee into Meow Bank, inside the same transaction.
        meowfee.credit(cur, guild_id, treat)

        return Fill(
            True,
            symbol=instrument.symbol,
            side="sell",
            quantity=wanted,
            unit_price_coins=unit_price,
            coins=proceeds,
            realized=realized,
            quantity_after=new_quantity,
            cost_after=new_cost,
            treat_coins=treat,
        )

    return await asyncio.to_thread(db.run_atomic, _sell)
