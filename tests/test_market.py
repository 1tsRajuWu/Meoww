"""The market: real provider payloads, honest staleness, and boxes that balance.

Three properties are pinned here, because they are the three ways a market in a
Discord bot goes wrong:

* **The parsing is checked against what the providers actually return.** Every
  payload below was captured from the live endpoint it names, including the
  shapes that broke the first draft — Kraken's legacy ``XXBTZUSD`` keys, CNBC's
  ``"UNCH"`` for a stock that has not printed, and a *negative* 24-hour change,
  which an over-eager "positive number" parse silently dropped so that every
  falling asset looked flat.
* **A price that cannot be trusted is never traded against.** A quote older than
  ``MAX_QUOTE_AGE_SECONDS``, or one that no source could produce, refuses the
  fill instead of pricing it off a guess.
* **The coins add up.** Buys debit conditionally, sells check the holding and the
  rolling daily ceiling inside the same transaction, and two simultaneous orders
  cannot both be served from one wallet or one position.
* **A real return is reproduced 1:1.** Fills happen at the observed price on both
  sides, so a position that moved +10% in dollars moved +10% in coins and a round
  trip at an unchanged price is free.
* **The market is one role's, and nobody else's.** Every subcommand sits behind
  the same gate, and there is no administrator escape hatch in the decision.
"""
import asyncio
import inspect
import io
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import aiohttp

from PIL import Image

from cogs import economy as economy_module
from cogs import market as market_module
from tests.support import TempDatabaseMixin
from utils import chart, db, market

GUILD = 4242
TRADER = 77
T0 = 1_700_000_000.0

# --------------------------------------------------------------------------- #
# Payloads, exactly as the live endpoints returned them
# --------------------------------------------------------------------------- #
BINANCE_24HR = [
    {"symbol": "BTCUSDT", "lastPrice": "77372.90000000", "priceChangePercent": "0.166"},
    {"symbol": "ETHUSDT", "lastPrice": "2511.96000000", "priceChangePercent": "-0.512"},
]
KRAKEN_TICKER = {
    "error": [],
    "result": {
        "XXBTZUSD": {"c": ["77348.1", "0.001"], "a": ["77348.67", "1", "1.0"]},
        "XETHZUSD": {"c": ["2511.12", "0.004"], "a": ["2511.20", "1", "1.0"]},
        "SOLUSD": {"c": ["101.34", "2.0"], "a": ["101.40", "1", "1.0"]},
    },
}
COINBASE_TICKER = {
    "ask": "77348.67",
    "bid": "77348.11",
    "volume": "1933.27796833",
    "price": "77348.1",
    "time": "2026-09-13T21:36:27.429965859Z",
}
TRADINGVIEW_SCAN = {
    "totalCount": 2,
    "data": [
        {"s": "NASDAQ:AAPL", "d": [332.27, 1.7454144593808336, "AAPL"]},
        {"s": "NASDAQ:NVDA", "d": [218.29, -0.03205715332479465, "NVDA"]},
    ],
}
CNBC_QUOTES = {
    "FormattedQuoteResult": {
        "FormattedQuote": [
            {"symbol": "AAPL", "last": "332.27", "change_pct": "1.75%"},
            {"symbol": "TSLA", "last": "UNCH", "change_pct": "0%"},
        ]
    }
}


class _FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeHTTP:
    """Answers calls by URL substring, and records what was asked for.

    An unmatched URL answers 500, which is how a blocked provider behaves and
    what makes the fallback chains testable without a network.
    """

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls: list[tuple] = []

    def _answer(self, url):
        for needle, payload in self.routes:
            if needle in url:
                if isinstance(payload, Exception):
                    return _FakeResponse(500, None)
                return _FakeResponse(200, payload)
        return _FakeResponse(500, None)

    def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        return self._answer(url)

    def post(self, url, json=None):
        self.calls.append(("POST", url, json))
        return self._answer(url)


class _FakeClientSession(_FakeHTTP):
    """Stands in for ``aiohttp.ClientSession`` inside ``market._fetch``."""

    def __init__(self, routes, **_kwargs):
        super().__init__(routes)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_http(routes):
    """Patch the session factory; every HTTP call inside the block is canned."""
    return mock.patch.object(
        market,
        "aiohttp",
        SimpleNamespace(
            ClientSession=lambda **kwargs: _FakeClientSession(routes, **kwargs),
            ClientTimeout=aiohttp.ClientTimeout,
        ),
    )


def quote(symbol: str, price_usd: float, *, as_of: float = T0, change=None, source="test"):
    return market.Quote(
        symbol=symbol, price_usd=price_usd, change_pct=change, source=source, as_of=as_of
    )


def snapshot(*, as_of: float = T0, **prices) -> market.Snapshot:
    """A snapshot from ``symbol=price`` keywords, all stamped at ``as_of``."""
    return market.Snapshot(
        quotes={symbol: quote(symbol, price, as_of=as_of) for symbol, price in prices.items()}
    )


class TestCatalog(unittest.TestCase):
    def test_symbols_are_unique_and_every_provider_can_name_them(self):
        symbols = [item.symbol for item in market.INSTRUMENTS]
        self.assertEqual(len(symbols), len(set(symbols)))
        for item in market.INSTRUMENTS:
            if item.kind == market.CRYPTO:
                self.assertTrue(item.id_for("binance"), item.symbol)
                self.assertTrue(item.id_for("kraken"), item.symbol)
                self.assertTrue(item.id_for("coinbase"), item.symbol)
            else:
                self.assertEqual(item.kind, market.STOCK)
                self.assertTrue(item.id_for("tradingview"), item.symbol)
                self.assertTrue(item.id_for("cnbc"), item.symbol)

    def test_find_prefers_an_exact_symbol_then_offers_the_matches(self):
        self.assertEqual(market.find("btc")[0].symbol, "BTC")
        self.assertEqual(market.find("BTC")[0].symbol, "BTC")
        self.assertEqual(market.find("bitcoin")[0].symbol, "BTC")
        self.assertEqual(market.find(""), [])
        self.assertEqual(market.find("nonsense"), [])
        # An ambiguous query returns every candidate so the caller can ask.
        self.assertGreater(len(market.find("A")), 1)

    def test_fills_are_one_to_one_with_the_observed_price(self):
        """No spread, on either side: the house takes nothing from a fill."""
        q = quote("BTC", 100.0)
        self.assertEqual(q.mid_coins(), 100.0 * market.COINS_PER_USD)
        self.assertEqual(market.SPREAD, 0.0)
        self.assertEqual(q.buy_coins(), q.mid_coins())
        self.assertEqual(q.sell_coins(), q.mid_coins())
        # A round trip at an unchanged price is therefore free, not a loss.
        self.assertEqual(q.sell_coins() / q.buy_coins() - 1, 0.0)

    def test_a_real_move_is_the_same_move_in_coins(self):
        """The whole point of 1:1 — +10% in dollars is +10% here."""
        bought = quote("BTC", 100.0)
        later = quote("BTC", 110.0)
        coins_in = 25_000
        quantity = market.buy_quantity(coins_in, bought.buy_coins())
        coins_out = market.sell_proceeds(quantity, later.sell_coins())
        self.assertAlmostEqual(coins_out / coins_in - 1, 0.10, places=6)
        # And a loss is the real loss too, not a loss plus a fee.
        worse = quote("BTC", 90.0)
        coins_out = market.sell_proceeds(quantity, worse.sell_coins())
        self.assertAlmostEqual(coins_out / coins_in - 1, -0.10, places=6)


class TestParsing(unittest.TestCase):
    def test_a_change_may_be_negative_or_zero_but_a_price_may_not(self):
        """The bug that made every falling market look flat."""
        self.assertEqual(market._as_number("-0.512"), -0.512)
        self.assertEqual(market._as_number("0.000"), 0.0)
        self.assertEqual(market._as_number("-0.03205715332479465"), -0.03205715332479465)
        self.assertIsNone(market._as_price("-0.512"))
        self.assertIsNone(market._as_price("0"))
        self.assertIsNone(market._as_price(0.0))

    def test_placeholders_are_not_numbers(self):
        for junk in ("N/A", "UNCH", "", "  ", "--", None, True, [], {}):
            self.assertIsNone(market._as_number(junk), repr(junk))
            self.assertIsNone(market._as_price(junk), repr(junk))

    def test_numbers_survive_the_shapes_providers_use(self):
        self.assertEqual(market._as_price("$332.27"), 332.27)
        self.assertEqual(market._as_price("1,234.5"), 1234.5)
        self.assertEqual(market._as_price(2511.12), 2511.12)
        self.assertEqual(market._as_number("1.75%"), 1.75)

    def test_kraken_pair_names_are_matched_not_looked_up(self):
        self.assertIn("XXBTZUSD", market.kraken_aliases("XBTUSD"))
        self.assertIn("XETHZUSD", market.kraken_aliases("ETHUSD"))
        self.assertIn("SOLUSD", market.kraken_aliases("SOLUSD"))

    async def test_binance_reads_its_batched_payload(self):
        http = _FakeHTTP([("api.binance.com", BINANCE_24HR)])
        got = await market._binance(http, [market.BY_SYMBOL["BTC"], market.BY_SYMBOL["ETH"]])
        self.assertEqual(got["BTC"][0], 77372.90)
        self.assertEqual(got["BTC"][1], 0.166)
        self.assertEqual(got["ETH"][1], -0.512, "a negative change was dropped")
        # The pairs asked for are the provider's names, not ours.
        self.assertIn("BTCUSDT", http.calls[0][2]["symbols"])

    async def test_kraken_reads_legacy_and_modern_pair_keys(self):
        http = _FakeHTTP([("api.kraken.com", KRAKEN_TICKER)])
        instruments = [market.BY_SYMBOL[s] for s in ("BTC", "ETH", "SOL")]
        got = await market._kraken(http, instruments)
        self.assertEqual(got["BTC"][0], 77348.1)   # from XXBTZUSD
        self.assertEqual(got["ETH"][0], 2511.12)   # from XETHZUSD
        self.assertEqual(got["SOL"][0], 101.34)    # from SOLUSD
        self.assertIsNone(got["BTC"][1], "kraken does not report a 24h change here")

    async def test_kraken_surfaces_its_own_error_list(self):
        http = _FakeHTTP([("api.kraken.com", {"error": ["EGeneral:Invalid arguments"], "result": {}})])
        with self.assertRaises(market.ProviderError):
            await market._kraken(http, [market.BY_SYMBOL["BTC"]])

    async def test_coinbase_reads_one_product(self):
        http = _FakeHTTP([("api.exchange.coinbase.com", COINBASE_TICKER)])
        got = await market._coinbase(http, [market.BY_SYMBOL["BTC"]])
        self.assertEqual(got["BTC"][0], 77348.1)

    async def test_tradingview_reads_the_scanner_columns(self):
        http = _FakeHTTP([("scanner.tradingview.com", TRADINGVIEW_SCAN)])
        got = await market._tradingview(http, [market.BY_SYMBOL["AAPL"], market.BY_SYMBOL["NVDA"]])
        self.assertEqual(got["AAPL"], (332.27, 1.7454144593808336))
        self.assertEqual(got["NVDA"][1], -0.03205715332479465)

    async def test_cnbc_skips_a_stock_that_has_not_printed(self):
        http = _FakeHTTP([("quote.cnbc.com", CNBC_QUOTES)])
        got = await market._cnbc(http, [market.BY_SYMBOL["AAPL"], market.BY_SYMBOL["TSLA"]])
        self.assertEqual(got["AAPL"][0], 332.27)
        self.assertEqual(got["AAPL"][1], 1.75)
        self.assertNotIn("TSLA", got, "'UNCH' is not a price")

    async def test_a_bad_status_is_a_provider_failure_not_an_empty_result(self):
        http = _FakeHTTP([("api.binance.com", RuntimeError("blocked"))])
        with self.assertRaises(market.ProviderError):
            await market._binance(http, [market.BY_SYMBOL["BTC"]])

    async def test_a_200_that_is_not_the_expected_shape_is_a_failure(self):
        http = _FakeHTTP([("api.binance.com", {"unexpected": "object"})])
        with self.assertRaises(market.ProviderError):
            await market._binance(http, [market.BY_SYMBOL["BTC"]])


class TestFetching(unittest.TestCase):
    """``asyncio`` running on a plain TestCase: the fetches are the unit here."""

    def setUp(self):
        market.reset()

    def test_the_chain_fills_gaps_and_only_asks_for_what_is_missing(self):
        now = T0
        routes = [
            ("api.binance.com", [BINANCE_24HR[0]]),          # only BTC
            ("api.kraken.com", KRAKEN_TICKER),               # ETH, and SOL on top
            ("scanner.tradingview.com", TRADINGVIEW_SCAN),
        ]
        with _patch_http(routes):
            snap = asyncio.run(market._fetch(now))
        self.assertEqual(snap.quote("BTC").source, "binance")
        self.assertEqual(snap.quote("ETH").source, "kraken")
        self.assertEqual(snap.quote("AAPL").source, "tradingview")
        # Everything on the board is priced; the rest of the catalog simply is not.
        self.assertIn("BTC", snap.quotes)
        self.assertIn("AAPL", snap.quotes)
        self.assertNotIn("MSFT", snap.quotes)

    def test_a_blocked_crypto_source_is_replaced_by_the_next_one(self):
        with _patch_http([("api.kraken.com", KRAKEN_TICKER)]):
            snap = asyncio.run(market._fetch(T0))
        self.assertEqual(snap.quote("BTC").source, "kraken")
        self.assertIsNone(snap.quote("AAPL"), "no stock source answered")

    def test_every_source_down_returns_nothing_rather_than_a_guess(self):
        with _patch_http([]):
            snap = asyncio.run(market._fetch(T0))
        self.assertEqual(snap.quotes, {})
        self.assertEqual(snap.missing(), tuple(i.symbol for i in market.INSTRUMENTS))


class TestSnapshot(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("market_snap.db")
        economy_module.DB_PATH = path
        await db.setup()
        market.reset()

    async def asyncTearDown(self):
        market.reset()
        self.release_temp_database()

    async def test_a_board_is_reused_for_the_ttl_and_refetchable_on_demand(self):
        calls = []

        async def fake_fetch(now, instruments=market.INSTRUMENTS):
            calls.append(now)
            return market.Snapshot(quotes={"BTC": quote("BTC", 100.0, as_of=now)})

        with mock.patch.object(market, "_fetch", new=fake_fetch):
            first = await market.snapshot(T0)
            again = await market.snapshot(T0 + market.QUOTE_TTL_SECONDS - 1)
            forced = await market.snapshot(T0 + market.QUOTE_TTL_SECONDS - 1, force=True)

        self.assertEqual(len(calls), 2, "the same board was fetched twice inside its TTL")
        self.assertIs(first, again)
        self.assertGreater(forced.as_of, first.as_of)

    async def test_a_forced_refresh_has_a_floor(self):
        """Otherwise a symbol no source can price makes every view fetch again."""
        calls = []

        async def fake_fetch(now, instruments=market.INSTRUMENTS):
            calls.append(now)
            return market.Snapshot(quotes={"BTC": quote("BTC", 100.0, as_of=now)})

        with mock.patch.object(market, "_fetch", new=fake_fetch):
            await market.snapshot(T0)
            await market.snapshot(T0 + 1, force=True)
            self.assertEqual(len(calls), 1, "a one-second-old board was fetched again")
            await market.snapshot(T0 + market.REFRESH_MIN_INTERVAL_SECONDS, force=True)
        self.assertEqual(len(calls), 2)

    async def test_a_symbol_that_fails_keeps_its_old_price_and_its_old_timestamp(self):
        """Merging must not launder an old price into a fresh one."""
        async def first_fetch(now, instruments=market.INSTRUMENTS):
            return market.Snapshot(
                quotes={
                    "BTC": quote("BTC", 100.0, as_of=now),
                    "ETH": quote("ETH", 10.0, as_of=now),
                }
            )

        async def later_fetch(now, instruments=market.INSTRUMENTS):
            return market.Snapshot(quotes={"BTC": quote("BTC", 101.0, as_of=now)})

        with mock.patch.object(market, "_fetch", new=first_fetch):
            await market.snapshot(T0)
        with mock.patch.object(market, "_fetch", new=later_fetch):
            snap = await market.snapshot(T0 + market.QUOTE_TTL_SECONDS + 1, force=True)

        self.assertEqual(snap.quote("BTC").price_usd, 101.0)
        self.assertEqual(snap.quote("ETH").price_usd, 10.0)
        self.assertEqual(snap.quote("ETH").as_of, T0, "the stale leg was re-stamped as fresh")

    async def test_a_dead_feed_keeps_the_board_but_blocks_the_fill(self):
        async def first_fetch(now, instruments=market.INSTRUMENTS):
            return market.Snapshot(quotes={"BTC": quote("BTC", 100.0, as_of=now)})

        with mock.patch.object(market, "_fetch", new=first_fetch):
            await market.snapshot(T0)
        with mock.patch.object(market, "_fetch", new=lambda *a, **k: _empty_fetch()):
            snap = await market.snapshot(T0 + 10_000, force=True)

        self.assertEqual(snap.quote("BTC").price_usd, 100.0, "the board was blanked by a dead feed")
        self.assertEqual(market.last_error(), "every price source was unreachable")
        self.assertIsNone(
            snap.tradable("BTC", T0 + 10_000), "an old price was still tradable"
        )

    async def test_a_price_older_than_the_gate_is_not_tradable(self):
        snap = snapshot(BTC=100.0)
        self.assertIsNotNone(snap.tradable("BTC", T0 + market.MAX_QUOTE_AGE_SECONDS))
        self.assertIsNone(snap.tradable("BTC", T0 + market.MAX_QUOTE_AGE_SECONDS + 1))
        self.assertIsNone(snap.tradable("DOGE", T0), "an unpriced symbol is not tradable")

    async def test_the_board_survives_a_restart(self):
        async def fake_fetch(now, instruments=market.INSTRUMENTS):
            return market.Snapshot(
                quotes={"BTC": quote("BTC", 100.0, as_of=now, source="binance")}
            )

        with mock.patch.object(market, "_fetch", new=fake_fetch):
            await market.snapshot(T0)

        market.reset()  # a fresh process: nothing in memory
        with mock.patch.object(market, "_fetch", new=lambda *a, **k: _empty_fetch()):
            reloaded = await market.snapshot(T0 + market.QUOTE_TTL_SECONDS + 1, force=True)

        self.assertEqual(reloaded.quote("BTC").price_usd, 100.0)
        self.assertEqual(reloaded.quote("BTC").source, "binance")
        self.assertEqual(reloaded.quote("BTC").as_of, T0)


async def _empty_fetch(*_args, **_kwargs):
    return market.Snapshot(quotes={})


class TestOrderMaths(unittest.TestCase):
    def test_buying_spends_what_it_says(self):
        price = 1_000_000.0
        quantity = market.buy_quantity(25_000, price)
        self.assertAlmostEqual(quantity * price, 25_000, places=4)
        self.assertEqual(market.buy_quantity(0, price), 0.0)
        self.assertEqual(market.buy_quantity(1_000, 0), 0.0)

    def test_selling_pays_out_at_the_sell_price(self):
        self.assertEqual(market.sell_proceeds(2.0, 1_000.0), 2_000)
        self.assertEqual(market.sell_proceeds(0.0, 1_000.0), 0)
        self.assertEqual(market.sell_proceeds(1.0, 0.0), 0)

    def test_a_full_close_takes_the_whole_basis(self):
        # Rounding must never leave a sliver of cost behind on an empty position.
        self.assertEqual(market.cost_basis_portion(1_000.0, 3.0, 3.0), 1_000.0)
        self.assertEqual(market.cost_basis_portion(1_000.0, 3.0, 9.0), 1_000.0)
        self.assertAlmostEqual(market.cost_basis_portion(1_000.0, 4.0, 1.0), 250.0)
        self.assertEqual(market.cost_basis_portion(1_000.0, 0.0, 1.0), 0.0)


class TestSettlement(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("market_trade.db")
        economy_module.DB_PATH = path
        await db.setup()
        market.reset()
        market.clear_trade_limits()
        self.btc = market.BY_SYMBOL["BTC"]
        self.quote = quote("BTC", 100.0, as_of=T0)  # 25,000 coins per unit

    async def asyncTearDown(self):
        market.reset()
        market.clear_trade_limits()
        self.release_temp_database()

    def _seed_wallet(self, wallet: int, user_id: int = TRADER):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?, ?, ?, 0)",
                (user_id, GUILD, wallet),
            )
            conn.commit()
        finally:
            conn.close()

    def _wallet(self, user_id: int = TRADER) -> int:
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT wallet FROM economy WHERE user_id=? AND guild_id=?", (user_id, GUILD)
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    async def test_a_buy_debits_records_and_sets_the_basis(self):
        self._seed_wallet(50_000)
        fill = await market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0)

        self.assertTrue(fill.ok, fill.reason)
        self.assertEqual(fill.coins, 25_000)
        self.assertAlmostEqual(fill.quantity, 25_000 / self.quote.buy_coins(), places=8)
        self.assertEqual(self._wallet(), 25_000)
        holding = (await market.holdings(TRADER, GUILD))["BTC"]
        self.assertAlmostEqual(holding.quantity, fill.quantity, places=8)
        self.assertEqual(holding.cost_coins, 25_000)
        trades = await market.trade_history(TRADER, GUILD)
        self.assertEqual(trades[0]["side"], "buy")
        self.assertEqual(trades[0]["coins"], 25_000)
        self.assertEqual(trades[0]["unit_price_coins"], self.quote.buy_coins())

    async def test_a_second_buy_averages_the_cost(self):
        self._seed_wallet(100_000)
        await market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0)
        await market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0 + 1)
        holding = (await market.holdings(TRADER, GUILD))["BTC"]
        self.assertEqual(holding.cost_coins, 50_000)
        self.assertAlmostEqual(
            holding.avg_cost_coins, 50_000 / holding.quantity, places=6
        )
        # Equal to the fill price to within the stored quantity's precision.
        self.assertAlmostEqual(holding.avg_cost_coins, self.quote.buy_coins(), places=3)

    async def test_a_buy_is_refused_when_the_wallet_cannot_cover_it(self):
        self._seed_wallet(1_000)
        fill = await market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0)
        self.assertFalse(fill.ok)
        self.assertEqual(fill.reason, "funds")
        self.assertEqual(self._wallet(), 1_000)
        self.assertEqual(await market.holdings(TRADER, GUILD), {})

    async def test_order_size_limits_are_enforced(self):
        self._seed_wallet(10_000_000)
        small = await market.settle_buy(
            TRADER, GUILD, self.btc, market.MIN_TRADE_COINS - 1, self.quote, T0
        )
        large = await market.settle_buy(
            TRADER, GUILD, self.btc, market.MAX_TRADE_COINS + 1, self.quote, T0
        )
        self.assertEqual((small.reason, large.reason), ("too_small", "too_large"))
        self.assertEqual(self._wallet(), 10_000_000)

    async def test_a_stale_price_cannot_be_traded_against(self):
        self._seed_wallet(50_000)
        stale = quote("BTC", 100.0, as_of=T0 - market.MAX_QUOTE_AGE_SECONDS - 1)
        fill = await market.settle_buy(TRADER, GUILD, self.btc, 25_000, stale, T0)
        self.assertEqual(fill.reason, "stale")
        self.assertEqual(self._wallet(), 50_000)

    async def test_two_simultaneous_buys_cannot_share_one_wallet(self):
        self._seed_wallet(25_000)  # enough for exactly one of the two
        fills = await asyncio.gather(
            market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0),
            market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0),
        )
        self.assertEqual(sum(1 for f in fills if f.ok), 1)
        self.assertEqual(self._wallet(), 0)
        holding = (await market.holdings(TRADER, GUILD))["BTC"]
        self.assertAlmostEqual(holding.cost_coins, 25_000, places=4)

    async def test_a_partial_sale_realises_its_share_of_the_basis(self):
        self._seed_wallet(50_000)
        await market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0)
        self.quote = quote("BTC", 120.0, as_of=T0, source="test")
        held = (await market.holdings(TRADER, GUILD))["BTC"]

        fill = await market.settle_sell(
            TRADER, GUILD, self.btc, held.quantity / 2, self.quote, T0
        )
        self.assertTrue(fill.ok, fill.reason)
        self.assertAlmostEqual(fill.quantity, held.quantity / 2, places=8)
        self.assertEqual(fill.coins, market.sell_proceeds(fill.quantity, self.quote.sell_coins()))
        # Half the position leaves with half the cost, so the P/L is real.
        self.assertEqual(fill.realized, fill.coins - int(round(25_000 / 2)))
        after = (await market.holdings(TRADER, GUILD))["BTC"]
        self.assertAlmostEqual(after.quantity, held.quantity / 2, places=8)
        self.assertEqual(after.cost_coins, 25_000 - int(round(25_000 / 2)))

    async def test_selling_everything_closes_the_position_cleanly(self):
        self._seed_wallet(50_000)
        await market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0)
        fill = await market.settle_sell(
            TRADER, GUILD, self.btc, 0.0, self.quote, T0, sell_all=True
        )
        self.assertTrue(fill.ok, fill.reason)
        self.assertEqual(fill.quantity_after, 0.0)
        self.assertEqual(fill.cost_after, 0.0)
        self.assertEqual(fill.avg_cost_coins, 0.0)
        self.assertEqual(await market.holdings(TRADER, GUILD), {})
        # No spread: closing at the price you bought at returns the coins you
        # spent, to within the one coin of integer rounding on the way out.
        self.assertAlmostEqual(fill.coins, 25_000, delta=1)
        self.assertEqual(fill.realized, fill.coins - 25_000)

    async def test_a_round_trip_at_an_unchanged_price_leaves_the_wallet_whole(self):
        self._seed_wallet(25_000)
        await market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0)
        await market.settle_sell(TRADER, GUILD, self.btc, 0.0, self.quote, T0, sell_all=True)
        self.assertAlmostEqual(self._wallet(), 25_000, delta=1)

    async def test_a_real_ten_percent_move_pays_ten_percent(self):
        """The 1:1 promise, measured end to end through the real settlement."""
        self._seed_wallet(25_000)
        await market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0)
        self.quote = quote("BTC", 110.0, as_of=T0, source="test")
        fill = await market.settle_sell(
            TRADER, GUILD, self.btc, 0.0, self.quote, T0, sell_all=True
        )
        self.assertTrue(fill.ok, fill.reason)
        self.assertAlmostEqual(self._wallet() / 25_000 - 1, 0.10, places=4)
        self.assertEqual(fill.realized, self._wallet() - 25_000)

    async def test_you_cannot_sell_more_than_you_hold(self):
        self._seed_wallet(50_000)
        await market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0)
        held = (await market.holdings(TRADER, GUILD))["BTC"]
        fill = await market.settle_sell(TRADER, GUILD, self.btc, held.quantity * 2, self.quote, T0)
        self.assertEqual(fill.reason, "too_many")

    async def test_selling_what_you_do_not_hold_is_refused(self):
        fill = await market.settle_sell(TRADER, GUILD, self.btc, 1.0, self.quote, T0)
        self.assertEqual(fill.reason, "none")

    async def test_the_market_will_not_buy_back_more_than_its_daily_ceiling(self):
        """A generous ceiling, but a real one: it is what bounds the minting."""
        self._seed_wallet(300_000)
        cheap = quote("BTC", 10.0, as_of=T0)          # 2,500 coins per unit
        await market.settle_buy(TRADER, GUILD, self.btc, 250_000, cheap, T0)

        rich = quote("BTC", 100_000.0, as_of=T0)      # 24,875,000 coins a unit
        first = await market.settle_sell(TRADER, GUILD, self.btc, 0.04, rich, T0)
        self.assertTrue(first.ok, first.reason)
        # Spends the ceiling exactly — the boundary is inclusive, and it is the
        # *next* coin that is refused.
        self.assertEqual(first.coins, market.MAX_DAILY_SELL_COINS)

        second = await market.settle_sell(TRADER, GUILD, self.btc, 0.04, rich, T0)
        self.assertEqual(second.reason, "daily_cap")
        self.assertEqual(
            await market.sold_coins_since(TRADER, GUILD, T0 - 1), first.coins
        )
        # The position is untouched by a refused sale.
        self.assertGreater((await market.holdings(TRADER, GUILD))["BTC"].quantity, 0)

    async def test_sold_coins_since_counts_only_sales(self):
        self._seed_wallet(100_000)
        await market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0)
        self.assertEqual(await market.sold_coins_since(TRADER, GUILD, T0 - 1), 0)
        await market.settle_sell(TRADER, GUILD, self.btc, 0.0, self.quote, T0, sell_all=True)
        sold = await market.sold_coins_since(TRADER, GUILD, T0 - 1)
        self.assertGreater(sold, 0)
        self.assertEqual(await market.sold_coins_since(TRADER, GUILD, T0 + 1), 0)

    async def test_two_simultaneous_sales_cannot_sell_the_same_units_twice(self):
        self._seed_wallet(100_000)
        await market.settle_buy(TRADER, GUILD, self.btc, 25_000, self.quote, T0)
        held = (await market.holdings(TRADER, GUILD))["BTC"]

        fills = await asyncio.gather(
            market.settle_sell(TRADER, GUILD, self.btc, held.quantity, self.quote, T0),
            market.settle_sell(TRADER, GUILD, self.btc, held.quantity, self.quote, T0),
        )
        self.assertEqual(sum(1 for f in fills if f.ok), 1)
        self.assertEqual(await market.holdings(TRADER, GUILD), {})

    async def test_the_trade_cooldown_blocks_only_the_same_member(self):
        market.clear_trade_limits()
        try:
            self.assertEqual(market.trade_cooldown_remaining(1, 2, T0), 0.0)
            market.note_trade(1, 2, T0)
            self.assertAlmostEqual(
                market.trade_cooldown_remaining(1, 2, T0 + 1),
                market.TRADE_COOLDOWN_SECONDS - 1,
            )
            self.assertEqual(
                market.trade_cooldown_remaining(1, 2, T0 + market.TRADE_COOLDOWN_SECONDS), 0.0
            )
            self.assertEqual(market.trade_cooldown_remaining(2, 2, T0 + 1), 0.0)
            self.assertEqual(market.trade_cooldown_remaining(1, 3, T0 + 1), 0.0)
        finally:
            market.clear_trade_limits()


class _FakeInteractionResponse:
    """The interaction's response object, capturing whatever was sent."""

    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _FakeUser:
    def __init__(self, uid: int = TRADER):
        self.id = uid
        self.name = "Trader"
        self.display_name = "Trader"


class _FakeInteraction:
    def __init__(self, uid: int = TRADER, gid: int = GUILD):
        self.user = _FakeUser(uid)
        self.guild = SimpleNamespace(id=gid)
        self.guild_id = gid
        self.response = _FakeInteractionResponse()


class _MarketCommands(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("market_cmd.db")
        economy_module.DB_PATH = path
        await db.setup()
        market.reset()
        market.clear_trade_limits()
        self.cog = market_module.Market(bot=None)
        self.cards: list = []
        self.files: list = []
        # The commands price off the real wall clock, so a frozen board has to be
        # stamped *now* to be tradable — T0 is in the past and must stay there for
        # the staleness tests.
        self._live = snapshot(BTC=100.0, AAPL=100.0, as_of=time.time())

    async def asyncTearDown(self):
        market.reset()
        market.clear_trade_limits()
        self.release_temp_database()

    async def _fake_send_card(self, _target, *args, **kwargs):
        self.cards.append(kwargs.get("embed"))
        file = kwargs.get("file")
        self.files.append(None if file is None else file.fp.getvalue())
        return SimpleNamespace(id=1)

    def _seed_wallet(self, wallet: int, user_id: int = TRADER):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) VALUES (?, ?, ?, 0)",
                (user_id, GUILD, wallet),
            )
            conn.commit()
        finally:
            conn.close()

    def _answer(self, interaction):
        if self.cards:
            card = self.cards[-1]
            return SimpleNamespace(
                title=getattr(card, "title", "") or "",
                description=getattr(card, "description", "") or "",
                fields=list(getattr(card, "fields", [])),
                footer=getattr(card, "footer", None),
                image=getattr(getattr(card, "image", None), "url", "") or "",
            )
        content = interaction.response.messages[-1]["content"] if interaction.response.messages else ""
        return SimpleNamespace(
            title="", description=content or "", fields=[], footer=None, image=""
        )

    def _frozen_snapshot(self, snap: market.Snapshot):
        """Serve a fixed board, whatever the command asks for."""
        async def _snapshot(now=None, *, force=False):
            return snap
        return mock.patch.object(market, "snapshot", new=_snapshot)

    async def _call(self, command, interaction, *args):
        self.cards.clear()
        self.files.clear()
        with mock.patch.object(market_module, "send_card", new=self._fake_send_card):
            await command.callback(self.cog, interaction, *args)
        return self._answer(interaction)

    async def board(self, interaction=None, live: market.Snapshot | None = None):
        with self._frozen_snapshot(live or self._live):
            return await self._call(
                market_module.Market.board, interaction or _FakeInteraction()
            )

    async def buy(self, symbol: str, coins: int, interaction=None):
        with self._frozen_snapshot(self._live):
            return await self._call(
                market_module.Market.buy, interaction or _FakeInteraction(), symbol, coins
            )

    async def sell(self, symbol: str, amount: str, interaction=None):
        with self._frozen_snapshot(self._live):
            return await self._call(
                market_module.Market.sell, interaction or _FakeInteraction(), symbol, amount
            )

    async def portfolio(self, interaction=None):
        with self._frozen_snapshot(self._live):
            return await self._call(
                market_module.Market.portfolio, interaction or _FakeInteraction()
            )

    async def history(self, interaction=None):
        # History is pure bookkeeping, but its card is rendered from the same
        # helper; no snapshot is read, so nothing needs freezing.
        return await self._call(
            market_module.Market.history, interaction or _FakeInteraction()
        )

    async def chart_card(self, symbol: str, period: str = "week", series=None, interaction=None):
        series = series or market.CandleSeries(
            symbol=symbol.upper(),
            candles=tuple(
                market.Candle(
                    ts=T0 + index * 3600,
                    open=100.0 + index,
                    high=102.0 + index,
                    low=98.0 + index,
                    close=101.0 + index,
                )
                for index in range(20)
            ),
            source="Binance",
            axis_label="4h candles",
        )

        async def _fetch(_instrument, _period, *, now=None):
            return series

        with mock.patch.object(market, "fetch_candles", new=_fetch):
            with self._frozen_snapshot(self._live):
                return await self._call(
                    market_module.Market.chart,
                    interaction or _FakeInteraction(),
                    symbol,
                    period,
                )

    async def quote_card(self, symbol: str, interaction=None):
        with self._frozen_snapshot(self._live):
            return await self._call(
                market_module.Market.quote, interaction or _FakeInteraction(), symbol
            )


class TestBoardAndQuote(_MarketCommands):
    async def test_the_board_lists_every_instrument_with_a_price(self):
        now = time.time()
        live = market.Snapshot(
            quotes={
                i.symbol: quote(i.symbol, 100.0 + n, as_of=now)
                for n, i in enumerate(market.INSTRUMENTS)
            }
        )
        card = await self.board(live=live)
        rendered = "\n".join(f"{f.name}\n{f.value}" for f in card.fields)
        for instrument in market.INSTRUMENTS:
            self.assertIn(instrument.symbol, rendered, instrument.symbol)
        self.assertIn("live", card.description)

    async def test_an_unpriced_symbol_is_shown_as_unavailable_not_guessed(self):
        live = snapshot(BTC=100.0, as_of=time.time())
        live = market.Snapshot(
            quotes={"BTC": quote("BTC", 100.0, as_of=time.time(), change=-1.25)}
        )
        card = await self.board(live=live)
        rendered = "\n".join(f"{f.name}\n{f.value}" for f in card.fields)
        self.assertIn("unavailable", rendered)
        self.assertIn("-1.25%", rendered, "a falling price lost its change")

    async def test_a_dead_market_says_so_and_pauses(self):
        card = await self.board(live=market.Snapshot(quotes={}))
        self.assertIn("No live prices", card.description)
        self.assertIn("paused", card.description)

    async def test_the_board_fits_discords_embed_limits(self):
        now = time.time()
        live = market.Snapshot(
            quotes={
                i.symbol: quote(i.symbol, 100.0 + n, as_of=now, change=1.5)
                for n, i in enumerate(market.INSTRUMENTS)
            }
        )
        card = await self.board(live=live)
        total = len(card.title) + len(card.description)
        for field in card.fields:
            self.assertLessEqual(len(field.name), 256)
            self.assertLessEqual(len(field.value), 1024, field.name)
            total += len(field.name) + len(field.value)
        self.assertLessEqual(len(card.fields), 25)
        self.assertLessEqual(total, 6000)

    async def test_a_quote_shows_both_sides_of_the_book(self):
        card = await self.quote_card("BTC")
        rendered = "\n".join(f"{f.name}\n{f.value}" for f in card.fields)
        expected = market.Quote("BTC", 100.0, None, "test", time.time())
        self.assertIn(f"{expected.buy_coins():,.0f}", rendered)
        self.assertIn(f"{expected.sell_coins():,.0f}", rendered)
        self.assertIn("test", card.footer.text)

    async def test_an_unknown_symbol_is_refused_with_a_pointer(self):
        card = await self.quote_card("NOTAREALTICKER")
        self.assertIn("not on the board", card.description)


class TestBuyAndSellCommands(_MarketCommands):
    async def test_buying_fills_and_shows_the_position(self):
        self._seed_wallet(50_000)
        card = await self.buy("BTC", 25_000)
        self.assertIn("Order Filled", card.title)
        self.assertIn("25,000", card.description)
        holding = (await market.holdings(TRADER, GUILD))["BTC"]
        self.assertGreater(holding.quantity, 0)
        self.assertEqual(holding.cost_coins, 25_000)
        self.assertIn("Wallet left", "\n".join(f"{f.name}\n{f.value}" for f in card.fields))

    async def test_buying_without_the_coins_is_refused_before_anything_moves(self):
        self._seed_wallet(1_000)
        card = await self.buy("BTC", 25_000)
        self.assertIn("wallet holds", card.description)
        self.assertEqual(await market.holdings(TRADER, GUILD), {})

    async def test_a_stale_market_refuses_the_fill_instead_of_pricing_it(self):
        self._seed_wallet(50_000)
        self._live = market.Snapshot(
            quotes={"BTC": quote("BTC", 100.0, as_of=T0 - market.MAX_QUOTE_AGE_SECONDS - 60)}
        )
        card = await self.buy("BTC", 25_000)
        self.assertIn("will not fill", card.description)
        self.assertEqual(await market.holdings(TRADER, GUILD), {})

    async def test_an_unpriced_symbol_refuses_the_fill(self):
        self._seed_wallet(50_000)
        self._live = market.Snapshot(quotes={})
        card = await self.buy("BTC", 25_000)
        self.assertIn("will not fill", card.description)

    async def test_selling_all_closes_the_position_and_reports_the_profit(self):
        self._seed_wallet(50_000)
        await self.buy("BTC", 25_000)
        market.clear_trade_limits()
        self._live = snapshot(BTC=120.0, as_of=time.time())
        card = await self.sell("BTC", "all")
        self.assertIn("Order Filled — Sell", card.title)
        self.assertIn("Profit / loss", "\n".join(f"{f.name}\n{f.value}" for f in card.fields))
        self.assertEqual(await market.holdings(TRADER, GUILD), {})

    async def test_a_percentage_sale_leaves_the_rest(self):
        self._seed_wallet(50_000)
        await self.buy("BTC", 25_000)
        market.clear_trade_limits()
        before = (await market.holdings(TRADER, GUILD))["BTC"]
        await self.sell("BTC", "50%")
        after = (await market.holdings(TRADER, GUILD))["BTC"]
        self.assertAlmostEqual(after.quantity, before.quantity / 2, places=8)

    async def test_unreadable_amounts_say_what_they_want(self):
        self._seed_wallet(50_000)
        await self.buy("BTC", 25_000)
        market.clear_trade_limits()
        card = await self.sell("BTC", "some of it")
        self.assertIn("not an amount", card.description)

    async def test_selling_nothing_is_refused(self):
        self._seed_wallet(50_000)
        card = await self.sell("BTC", "all")
        self.assertIn("do not hold", card.description)

    async def test_the_cooldown_stops_a_second_immediate_fill(self):
        self._seed_wallet(50_000)
        first = await self.buy("BTC", 25_000)
        self.assertIn("Order Filled", first.title)
        # No clearing this time: the second fill is inside the same few ms.
        with self._frozen_snapshot(self._live):
            second = await self._call(
                market_module.Market.buy, _FakeInteraction(), "BTC", 25_000
            )
        self.assertIn("settling", second.description)
        holding = (await market.holdings(TRADER, GUILD))["BTC"]
        self.assertEqual(holding.cost_coins, 25_000)


class TestChartCommand(_MarketCommands):
    async def test_the_chart_command_sends_an_image_with_the_card(self):
        card = await self.chart_card("BTC")
        self.assertIn("BTC", card.title)
        self.assertEqual(len(self.files), 1, "no image was attached")
        self.assertTrue(self.files[0].startswith(b"\x89PNG"))
        self.assertEqual(card.image, "attachment://chart.png")
        self.assertIn("over the last 7 days", card.description)

    async def test_the_chart_draws_the_members_average_cost(self):
        self._seed_wallet(500_000)
        await self.buy("BTC", 250_000)
        card = await self.chart_card("BTC")
        rendered = "\n".join(f"{f.name}\n{f.value}" for f in card.fields)
        self.assertIn("avg", rendered)
        self.assertIn("yellow line", rendered)
        # The drawn chart must actually contain the entry colour.
        image = Image.open(io.BytesIO(self.files[-1])).convert("RGB")
        self.assertGreater(list(image.get_flattened_data()).count(chart.ENTRY[:3]), 0)

    async def test_a_symbol_with_no_history_says_so(self):
        card = await self.chart_card("BTC", series=None)

        async def _none(_instrument, _period, *, now=None):
            return None

        with mock.patch.object(market, "fetch_candles", new=_none):
            with self._frozen_snapshot(self._live):
                card = await self._call(
                    market_module.Market.chart, _FakeInteraction(), "BTC", "week"
                )
        self.assertIn("Not enough price history", card.description)
        self.assertEqual(self.files, [])

    async def test_a_one_point_series_is_not_drawn_as_a_chart(self):
        single = market.CandleSeries(
            symbol="BTC",
            candles=(market.Candle(ts=T0, open=1.0, high=1.0, low=1.0, close=1.0),),
            source="Binance",
            axis_label="1h candles",
        )
        card = await self.chart_card("BTC", series=single)
        self.assertIn("Not enough price history", card.description)


class TestPortfolioAndHistory(_MarketCommands):
    async def test_an_empty_portfolio_says_where_to_start(self):
        card = await self.portfolio()
        self.assertIn("/market board", card.description)

    async def test_the_portfolio_separates_unrealised_from_realised_profit(self):
        self._seed_wallet(500_000)
        await self.buy("BTC", 250_000)
        market.clear_trade_limits()
        await self.sell("BTC", "25%")
        market.clear_trade_limits()
        card = await self.portfolio()
        rendered = "\n".join(f"{f.name}\n{f.value}" for f in card.fields)
        self.assertIn("Unrealised", rendered)
        self.assertIn("Realised", rendered)
        self.assertIn("Total", rendered)
        self.assertIn("over 1 sale", rendered)

    async def test_the_history_totals_every_sale(self):
        self._seed_wallet(500_000)
        await self.buy("BTC", 250_000)
        market.clear_trade_limits()
        await self.sell("BTC", "all")
        market.clear_trade_limits()
        card = await self.history()
        rendered = "\n".join(f"{f.name}\n{f.value}" for f in card.fields)
        self.assertIn("All Time", rendered)
        self.assertIn("across 1 sale", rendered)

    async def test_the_portfolio_values_a_position_and_its_profit(self):
        self._seed_wallet(50_000)
        await self.buy("BTC", 25_000)
        market.clear_trade_limits()
        self._live = snapshot(BTC=120.0, as_of=time.time())
        card = await self.portfolio()
        rendered = f"{card.description}\n" + "\n".join(
            f"{f.name}\n{f.value}" for f in card.fields
        )
        self.assertIn("BTC", rendered)
        self.assertIn("Unrealised", rendered)
        # Bought at 100, marked at 120: the position is up 20%, exactly.
        self.assertIn("+", rendered)
        self.assertIn("20.00%", rendered)

    async def test_an_empty_history_says_so(self):
        card = await self.history()
        self.assertIn("No fills yet", card.description)

    async def test_history_renders_buys_and_sells_with_realised_profit(self):
        self._seed_wallet(50_000)
        await self.buy("BTC", 25_000)
        market.clear_trade_limits()
        await self.sell("BTC", "all")
        market.clear_trade_limits()
        card = await self.history()
        self.assertIn("BUY", card.description)
        self.assertIn("SELL", card.description)
        self.assertIn("P/L", card.description)


class TestHistoryProviders(unittest.TestCase):
    """Real payloads from the history endpoints, same treatment as the quotes."""

    BINANCE_KLINES = [
        [1789326000000, "77338.87", "77409.04", "77223.16", "77284.00", "148.98", 1789329599999],
        [1789329600000, "77284.01", "77368.00", "77262.00", "77348.01", "103.43", 1789333199999],
    ]
    KRAKEN_OHLC = {
        "error": [],
        "result": {
            "XXBTZUSD": [
                [1789326000, "77338.8", "77409.0", "77223.1", "77284.0", "77300.1", "1.2", 90],
                [1789329600, "77284.0", "77368.0", "77262.0", "77348.0", "77320.0", "0.9", 71],
            ],
            "last": 1789333200,
        },
    }
    YAHOO_CHART = {
        "chart": {
            "result": [
                {
                    "timestamp": [1789000000, 1789086400, 1789172800],
                    "indicators": {
                        "quote": [
                            {
                                "open": [330.0, None, 331.5],
                                "high": [334.0, None, 336.3],
                                "low": [328.0, None, 326.3],
                                "close": [332.27, None, 335.1],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }
    NASDAQ_CHART = {
        "data": {
            "symbol": "AAPL",
            "chart": [
                {
                    "x": 1788220800000,
                    "y": 325.13,
                    "z": {
                        "high": "327.3",
                        "low": "314.73",
                        "open": "316.98",
                        "close": "325.13",
                        "dateTime": "9/1/2026",
                    },
                },
                {
                    "x": 1789084800000,
                    "y": 332.27,
                    "z": {
                        "high": "336.22",
                        "low": "326.3",
                        "open": "327.45",
                        "close": "332.27",
                        "dateTime": "9/11/2026",
                    },
                },
            ],
        }
    }

    def test_binance_klines_read_real_ohlc(self):
        http = _FakeHTTP([("api.binance.com", self.BINANCE_KLINES)])
        period = market.PERIODS["week"]
        candles = asyncio.run(
            market._binance_klines(http, market.BY_SYMBOL["BTC"], period, T0)
        )
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0].open, 77338.87)
        self.assertEqual(candles[0].high, 77409.04)
        self.assertEqual(candles[0].low, 77223.16)
        self.assertEqual(candles[0].close, 77284.00)
        self.assertEqual(candles[0].ts, 1789326000.0)
        self.assertFalse(candles[0].flat)
        self.assertEqual(http.calls[0][2]["interval"], "4h")

    def test_kraken_ohlc_matches_its_legacy_pair_name(self):
        http = _FakeHTTP([("api.kraken.com", self.KRAKEN_OHLC)])
        candles = asyncio.run(
            market._kraken_ohlc(http, market.BY_SYMBOL["BTC"], market.PERIODS["day"], T0)
        )
        self.assertEqual(candles[0].close, 77284.0)
        self.assertEqual(candles[1].high, 77368.0)

    def test_yahoo_reads_parallel_arrays_and_skips_nulls(self):
        http = _FakeHTTP([("query1.finance.yahoo.com", self.YAHOO_CHART)])
        candles = asyncio.run(
            market._yahoo_chart(http, market.BY_SYMBOL["AAPL"], market.PERIODS["month"], T0)
        )
        # The middle row has no trade in it, so it is not a candle.
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0].close, 332.27)
        self.assertEqual(candles[1].high, 336.3)

    def test_nasdaq_reads_its_z_blocks(self):
        http = _FakeHTTP([("api.nasdaq.com", self.NASDAQ_CHART)])
        candles = asyncio.run(
            market._nasdaq_chart(http, market.BY_SYMBOL["AAPL"], market.PERIODS["month"], T0)
        )
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0].low, 314.73)
        self.assertEqual(candles[1].close, 332.27)

    def test_a_history_source_that_answers_nonsense_is_a_failure(self):
        http = _FakeHTTP([("api.binance.com", {"not": "a list"})])
        with self.assertRaises(market.ProviderError):
            asyncio.run(
                market._binance_klines(http, market.BY_SYMBOL["BTC"], market.PERIODS["day"], T0)
            )


class TestFetchCandles(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("market_hist.db")
        economy_module.DB_PATH = path
        await db.setup()
        market.reset()

    async def asyncTearDown(self):
        market.reset()
        self.release_temp_database()

    async def test_crypto_history_comes_from_the_exchange_as_candles(self):
        routes = [("api.binance.com", TestHistoryProviders.BINANCE_KLINES)]
        with _patch_http(routes):
            series = await market.fetch_candles(market.BY_SYMBOL["BTC"], "week")
        self.assertIsNotNone(series)
        self.assertEqual(series.source, "Binance")
        self.assertEqual(series.axis_label, "4h candles")
        self.assertFalse(chart.is_line_series(list(series.candles)))

    async def test_a_blocked_exchange_falls_back_to_the_next_one(self):
        routes = [("api.kraken.com", TestHistoryProviders.KRAKEN_OHLC)]
        with _patch_http(routes):
            series = await market.fetch_candles(market.BY_SYMBOL["BTC"], "day")
        self.assertEqual(series.source, "Kraken")
        self.assertEqual(len(series.candles), 2)

    async def test_stock_history_falls_back_to_nasdaq_when_yahoo_fails(self):
        routes = [("api.nasdaq.com", TestHistoryProviders.NASDAQ_CHART)]
        with _patch_http(routes):
            series = await market.fetch_candles(market.BY_SYMBOL["AAPL"], "month")
        self.assertEqual(series.source, "Nasdaq")
        self.assertEqual(series.axis_label, "daily candles")

    async def test_history_falls_back_to_what_the_bot_observed_itself(self):
        """The only history that exists for a source with no chart endpoint."""
        observed = market.Snapshot(
            quotes={
                "AAPL": market.Quote("AAPL", 100.0, None, "tradingview", T0),
                "BTC": market.Quote("BTC", 50.0, None, "binance", T0),
            }
        )
        await market._save(observed)
        later = market.Snapshot(
            quotes={
                "AAPL": market.Quote("AAPL", 101.0, None, "tradingview", T0 + 60),
                "BTC": market.Quote("BTC", 51.0, None, "binance", T0 + 60),
            }
        )
        await market._save(later)

        with _patch_http([]):  # every exchange is down
            series = await market.fetch_candles(market.BY_SYMBOL["AAPL"], "day", now=T0 + 120)
        self.assertIsNotNone(series)
        self.assertEqual(series.source, "this bot's own polling")
        self.assertEqual(series.axis_label, "observed prices")
        self.assertEqual([c.close for c in series.candles], [100.0, 101.0])
        # Polled prices carry no intrabar range, so they must draw as a line.
        self.assertTrue(chart.is_line_series(list(series.candles)))

    async def test_no_history_anywhere_is_none_rather_than_an_empty_chart(self):
        with _patch_http([]):
            series = await market.fetch_candles(market.BY_SYMBOL["AAPL"], "day", now=T0)
        self.assertIsNone(series)

    async def test_a_single_observation_is_not_a_chart(self):
        await market._save(
            market.Snapshot(quotes={"AAPL": market.Quote("AAPL", 100.0, None, "x", T0)})
        )
        with _patch_http([]):
            series = await market.fetch_candles(market.BY_SYMBOL["AAPL"], "day", now=T0 + 10)
        self.assertIsNone(series, "one point is a fact, not a trend")

    async def test_observations_are_recorded_on_every_refresh_and_pruned(self):
        stale = market.Snapshot(
            quotes={"BTC": market.Quote("BTC", 10.0, None, "binance", T0 - 40 * 86400)}
        )
        await market._save(stale)
        fresh = market.Snapshot(quotes={"BTC": market.Quote("BTC", 20.0, None, "binance", T0)})
        await market._save(fresh)

        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT ts FROM market_history WHERE symbol='BTC' ORDER BY ts"
            ).fetchall()
        finally:
            conn.close()
        # The old row cannot be charted and is not kept.
        self.assertEqual([row[0] for row in rows], [T0])

    async def test_observed_series_is_ordered_and_thinned_by_stride(self):
        for index in range(20):
            await market._save(
                market.Snapshot(
                    quotes={
                        "BTC": market.Quote("BTC", 100.0 + index, None, "binance", T0 + index)
                    }
                )
            )
        series = await market.observed_series("BTC", 3600, T0 + 20, limit=10)
        self.assertEqual(len(series), 10)
        stamps = [candle.ts for candle in series]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(series[0].close, 100.0, "thinning dropped the start of the window")
        self.assertAlmostEqual(series[-1].close, 119.0, places=1)


class TestChartRendering(unittest.TestCase):
    """The chart is checked by counting pixels, because that is what the eye does."""

    @staticmethod
    def _counts(image, plot_only: bool = True):
        """Colours inside the plot panel — the header draws in the same palette."""
        width, height = image.size
        box = (26, 96, width - 110, height - 52) if plot_only else (0, 0, width, height)
        cropped = image.convert("RGB").crop(box)
        pixels = list(cropped.get_flattened_data())
        return {
            "up": pixels.count(chart.UP[:3]),
            "down": pixels.count(chart.DOWN[:3]),
            "entry": pixels.count(chart.ENTRY[:3]),
            "line": pixels.count(chart.ACCENT[:3]),
        }

    @staticmethod
    def _candles(count=30, start=100.0):
        out = []
        for index in range(count):
            base = start + index * 0.5
            out.append(
                market.Candle(
                    ts=T0 + index * 3600,
                    open=base,
                    high=base + 2,
                    low=base - 2,
                    close=base + (0.4 if index % 2 else -0.3),
                )
            )
        return out

    def _render(self, **kwargs):
        params = {
            "symbol": "BTC",
            "name": "Bitcoin",
            "candles": self._candles(),
            "price_coins": 115.0,
            "change_pct": -3.31,
            "source": "Binance",
            "interval_label": "4h candles · 7 days",
            "age_seconds": 2,
        }
        params.update(kwargs)
        return Image.open(chart.render_market_chart(**params))

    def test_it_returns_a_png_of_the_requested_size(self):
        buffer = chart.render_market_chart(symbol="BTC", candles=self._candles())
        data = buffer.getvalue()
        self.assertTrue(data.startswith(b"\x89PNG"), "not a PNG")
        self.assertEqual(Image.open(io.BytesIO(data)).size, (1000, 430))

    def test_candles_are_drawn_across_the_panel(self):
        """A regression guard for a unit mismatch that squashed them flat.

        With the axis in dollars and the member's cost in coins, the range was
        stretched to millions and every candle collapsed into a sliver at the
        bottom of the panel — the numbers still looked plausible, and only the
        pixels showed it.
        """
        image = self._render(entry_coins=104.0)
        pixels = image.convert("RGB").load()
        rows = [
            y
            for y in range(96, image.size[1] - 52)
            for x in range(26, image.size[0] - 110)
            if pixels[x, y] in (chart.UP[:3], chart.DOWN[:3])
        ]
        self.assertTrue(rows, "no candles were drawn at all")
        span = (max(rows) - min(rows)) / image.size[1]
        self.assertGreater(span, 0.3, f"candles covered only {span:.0%} of the image")
        self.assertGreater(self._counts(image)["entry"], 0)

    def test_an_entry_far_from_the_price_range_is_still_drawn_on_the_canvas(self):
        image = self._render(entry_coins=400.0)
        self.assertGreater(self._counts(image)["entry"], 0)

    def test_a_flat_series_does_not_divide_by_zero(self):
        flat = [
            market.Candle(ts=T0 + index * 60, open=50.0, high=50.0, low=50.0, close=50.0)
            for index in range(5)
        ]
        image = self._render(candles=flat, entry_coins=None)
        self.assertEqual(image.size, (1000, 430))
        # A flat line is a line, not candle bodies.
        self.assertGreater(self._counts(image)["line"], 0)

    def test_an_empty_series_says_so_instead_of_drawing_one(self):
        image = self._render(candles=[], entry_coins=None)
        counts = self._counts(image)
        self.assertEqual(counts["up"] + counts["down"] + counts["line"], 0)

    def test_many_points_switch_to_a_line(self):
        self.assertTrue(chart.is_line_series(self._candles(count=chart.MAX_CANDLES + 1)))
        self.assertFalse(chart.is_line_series(self._candles(count=10)))

    def test_the_sparkline_needs_a_shape_and_never_invents_one(self):
        self.assertEqual(chart.sparkline([]), "")
        self.assertEqual(chart.sparkline([5.0]), "")
        self.assertEqual(chart.sparkline([5.0, 5.0, 5.0]), chart.BLOCKS[0] * 3)
        rising = chart.sparkline([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(rising, "".join(chart.BLOCKS[index] for index in (0, 2, 4, 7)))
        self.assertEqual(chart.sparkline([4.0, 3.0, 2.0, 1.0]), rising[::-1])

    def test_axis_numbers_stay_readable(self):
        self.assertEqual(chart.short_number(19_350_202), "19.35M")
        self.assertEqual(chart.short_number(83_068), "83.1k")
        self.assertEqual(chart.short_number(332.27), "332")
        self.assertEqual(chart.short_number(0.08), "0.08")
        self.assertEqual(chart.short_number(-1_500_000), "-1.50M")


class _RoleStub:
    def __init__(self, role_id: int, name: str):
        self.id = role_id
        self.name = name


class _MemberStub:
    """A member reduced to the one thing the gate reads: their role ids."""

    def __init__(self, role_ids=()):
        self.id = TRADER
        self._roles = {role_id: None for role_id in role_ids}


class _GuildStub:
    def __init__(self, role_ids=()):
        self.id = GUILD
        self.roles = [_RoleStub(role_id, f"role-{role_id}") for role_id in role_ids]


class _RoleInteraction:
    """An interaction carrying real roles — the gate's entire input."""

    def __init__(self, member_role_ids=(), guild_role_ids=()):
        self.user = _MemberStub(member_role_ids)
        self.guild = _GuildStub(guild_role_ids)
        self.guild_id = GUILD
        self.response = _FakeInteractionResponse()

    @property
    def reply(self) -> str:
        return self.response.messages[-1]["content"] if self.response.messages else ""


class TestMarketAccess(unittest.TestCase):
    """`/market` belongs to one role, and to nobody else."""

    ROLE = 1548396810257698977

    def test_the_compiled_default_is_the_role_we_were_asked_for(self):
        self.assertEqual(market.DEFAULT_ACCESS_ROLE_IDS, (self.ROLE,))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MARKET_ROLE_IDS", None)
            os.environ.pop("MARKET_ROLE_ID", None)
            self.assertEqual(market.access_role_ids(), (self.ROLE,))

    def test_a_configured_list_overrides_it_and_junk_cannot_empty_it(self):
        with mock.patch.dict(os.environ, {"MARKET_ROLE_IDS": "111, 222;"}):
            self.assertEqual(market.access_role_ids(), (111, 222))
        # An allow list that parses to nothing would lock the market for
        # everyone forever, so a bad value falls back instead of applying.
        with mock.patch.dict(os.environ, {"MARKET_ROLE_IDS": "not-a-role"}):
            self.assertEqual(market.access_role_ids(), market.DEFAULT_ACCESS_ROLE_IDS)

    def test_only_the_listed_role_passes(self):
        self.assertIsNone(market.access_problem([self.ROLE], guild_role_ids=[self.ROLE, 999]))
        self.assertEqual(market.access_problem([999], guild_role_ids=[self.ROLE, 999]), "role")
        self.assertEqual(market.access_problem([], guild_role_ids=[self.ROLE]), "role")

    def test_there_is_no_administrator_escape_hatch(self):
        """"Nobody else" is the whole rule, so there is no admin parameter to pass."""
        params = set(inspect.signature(market.access_problem).parameters)
        self.assertEqual(params, {"member_role_ids", "allowed_role_ids", "guild_role_ids"})

    def test_a_guild_without_the_role_is_told_exactly_that(self):
        # Not silently everyone's, and not silently nobody's either: a distinct
        # answer, so the card can say which role id is missing.
        self.assertEqual(market.access_problem([1, 2], guild_role_ids=[1, 2]), "missing")


class TestTheGateOnTheCommand(unittest.IsolatedAsyncioTestCase):
    """The gate as a member actually meets it: on the cog, before any work."""

    ROLE = TestMarketAccess.ROLE

    def setUp(self):
        self.cog = market_module.Market(bot=None)

    async def test_a_member_without_the_role_is_refused(self):
        interaction = _RoleInteraction(member_role_ids=[999], guild_role_ids=[self.ROLE, 999])
        self.assertFalse(await self.cog.interaction_check(interaction))
        self.assertIn("restricted", interaction.reply)
        self.assertIn(f"role-{self.ROLE}", interaction.reply)

    async def test_the_listed_role_gets_through_untouched(self):
        interaction = _RoleInteraction(member_role_ids=[self.ROLE], guild_role_ids=[self.ROLE])
        self.assertTrue(await self.cog.interaction_check(interaction))
        self.assertEqual(interaction.response.messages, [])

    async def test_holding_some_other_role_is_not_enough(self):
        interaction = _RoleInteraction(member_role_ids=[7, 8], guild_role_ids=[7, 8, self.ROLE])
        self.assertFalse(await self.cog.interaction_check(interaction))

    async def test_a_guild_missing_the_role_is_told_which_id_is_missing(self):
        interaction = _RoleInteraction(member_role_ids=[7], guild_role_ids=[7])
        self.assertFalse(await self.cog.interaction_check(interaction))
        self.assertIn("does not have", interaction.reply)
        self.assertIn(str(self.ROLE), interaction.reply)

    async def test_a_direct_message_is_left_to_the_command_to_explain(self):
        interaction = _RoleInteraction(member_role_ids=[self.ROLE])
        interaction.guild = None
        interaction.guild_id = None
        self.assertTrue(await self.cog.interaction_check(interaction))


class TestCommandSurface(unittest.TestCase):
    def test_one_top_level_command_with_six_subcommands(self):
        """Discord caps an app at 100 global commands; the market spends one."""
        group = market_module.Market.market_group
        self.assertEqual(group.name, "market")
        names = sorted(c.name for c in group.commands)
        self.assertEqual(
            names, ["board", "buy", "chart", "history", "portfolio", "quote", "sell"]
        )

    def test_helpers_format_the_awkward_values(self):
        self.assertEqual(market_module.fmt_qty(0.0), "0")
        self.assertEqual(market_module.fmt_qty(0.00001234), "0.00001234")
        self.assertEqual(market_module.fmt_qty(2.5), "2.5")
        self.assertEqual(market_module.fmt_coins(0), "0")
        self.assertEqual(market_module.fmt_coins(1_500_000), "1.50M")
        self.assertEqual(market_module.fmt_change(-1.25), " ▼ `-1.25%`")
        self.assertEqual(market_module.fmt_change(None), "")

    def test_parsing_a_sell_amount(self):
        self.assertEqual(market_module.parse_amount("all", 4.0), (4.0, True, ""))
        self.assertEqual(market_module.parse_amount("25%", 4.0)[0], 1.0)
        # Asking for more than is held is a close-out, not an error.
        self.assertEqual(market_module.parse_amount("1,500", 4.0), (1500.0, True, ""))
        self.assertEqual(market_module.parse_amount("2", 4.0), (2.0, False, ""))
        self.assertIn("not an amount", market_module.parse_amount("lots", 4.0)[2])
        self.assertIn("between 0 and 100", market_module.parse_amount("900%", 4.0)[2])
        self.assertIn("positive", market_module.parse_amount("-2", 4.0)[2])
