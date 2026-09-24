/* ===========================================================================
   The market — behaviour
   ===========================================================================
   One page, two endpoints (`/api/market/quotes` and `/api/market/candles`), and
   one third-party library that is allowed to fail. The order below is the whole
   design:

   1. **The prices do not wait for the chart.** `loadQuotes()` is issued by the
      bootstrap, not from inside the chart setup, so the numbers are already on
      their way while a fallback CDN is being tried. That is the fix for the bug
      this page shipped: one blocked request killed the rest of one inline
      script, and the page sat on **"Loading live prices…"** with four empty
      cards — which is indistinguishable from a market with no prices, the one
      thing a price page must never say.
   2. **Everything that can hang has a deadline.** Fetches go through
      `getJSON` (an `AbortController` and a timer) and library loads through
      `loadScript` (a timer), because a CDN that accepts the connection and then
      says nothing is the same failure as one that refuses — only slower.
   3. **The chart is optional; the numbers are not.** Every drawing call is
      inside a guard, the library's absence is checked rather than assumed, and a
      chart that cannot be built is *reported in its own card* by `chartNotice`
      instead of leaving an empty rectangle.
   4. **Nothing is written from data.** Every value that came from an endpoint is
      written with `textContent`, for the reason the dashboard's composer is
      tested for: a value that looks like a path is not a path.
   =========================================================================== */
(function () {
    "use strict";

    document.documentElement.classList.remove("no-js");

    /* ---------------------------------------------------------------------
       How often, and only while somebody is looking
       ---------------------------------------------------------------------
       The board is one JSON object of every instrument, and it is fetched by
       the page rather than pushed to it, so the interval *is* the cost: every
       tick is bytes this host sends and is billed for.

       Two rules, both of which used to be missing:

       * **`QUOTE_POLL_MS` is not faster than the prices themselves.** The bot
         refreshes quotes at most once every `REFRESH_MIN_INTERVAL_SECONDS` (15),
         so an eight-second poll asked twice as often as the numbers could
         change and re-sent an identical board for half of those answers. There
         is nothing to gain below that floor.
       * **A hidden tab asks for nothing.** An open tab is not a reader. A window
         behind another window, a laptop that was closed, a browser that restored
         last week's session — each of those kept asking this host for the whole
         board every eight seconds for as long as it stayed open, all night, and
         none of it was looked at. The timer stops while the page is hidden and
         fires immediately when it is shown again, so a reader who comes back
         gets today's numbers rather than the one from whenever they left. */
    var QUOTE_POLL_MS = 15000;
    var CANDLE_POLL_MS = 60000;

    /* Poll `fn` on `ms`, but only while the page is visible. Returns the timer
       handle so a caller can stop it; the bootstrap does not.

       `document.hidden` rather than a focus check: focus moves between windows
       of the same tab and between a window and its own devtools, which is not
       the question. Hidden is the browser's own answer to "can anybody see
       this", and every browser on the supported list has it. */
    function pollWhileVisible(fn, ms) {
        var timer = null;
        var start = function () {
            if (timer === null) timer = window.setInterval(function () {
                if (!document.hidden) fn();
            }, ms);
        };
        var stop = function () {
            if (timer !== null) {
                window.clearInterval(timer);
                timer = null;
            }
        };
        document.addEventListener("visibilitychange", function () {
            if (document.hidden) {
                stop();
            } else {
                fn();
                start();
            }
        });
        if (!document.hidden) start();
        return { start: start, stop: stop };
    }

    /* ---------------------------------------------------------------------
       Boot
       --------------------------------------------------------------------- */
    function boot() {
        var node = document.getElementById("market-boot");
        if (!node) return { symbol: "", period: "" };
        try {
            return JSON.parse(node.textContent || "{}");
        } catch (error) {
            console.error("Meow Markets: could not read the boot payload", error);
            return { symbol: "", period: "" };
        }
    }

    var BOOT = boot();
    var SYMBOL = BOOT.symbol || "";
    var DEFAULT_PERIOD = BOOT.period || "";
    /* The bot's own fill window, carried in the boot payload. The ticket is a
       calculator for a command that will be run in Discord, so an amount outside
       this window is not a small error: it is a command that comes back refused,
       and the ticket is the only place a reader can find that out before typing.
       `0` means "not told", and then the ticket keeps the old behaviour of
       offering what it computed rather than inventing a limit of its own. */
    var MIN_TRADE = Number(BOOT.minTrade) || 0;
    var MAX_TRADE = Number(BOOT.maxTrade) || 0;
    var period = DEFAULT_PERIOD;
    var quotes = null;
    var chartObj = null;
    var candleSeries = null;
    var volumeSeries = null;
    var lastPrice = null;
    var watchKey = "meow-market-watch";

    /* The chart library is this page's only third-party request, and a single
       blocked or stalled one used to leave the whole page blank. These are tried
       in order; nothing else on the page depends on the library. */
    var CHART_FALLBACKS = [
        "https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js",
    ];

    var el = function (id) { return document.getElementById(id); };

    function fmt(n, digits) {
        if (n === null || n === undefined || isNaN(n)) return "—";
        var d = digits === undefined ? (Math.abs(n) >= 1000 ? 2 : (Math.abs(n) >= 1 ? 2 : 6)) : digits;
        return Number(n).toLocaleString("en-US", { maximumFractionDigits: d });
    }

    function coins(n) { return fmt(n) + " 🪙"; }

    function pct(n, digits) {
        if (n === null || n === undefined || isNaN(n)) return "—";
        return (n > 0 ? "+" : "") + Number(n).toFixed(digits === undefined ? 2 : digits) + "%";
    }

    function ago(seconds) {
        if (seconds === null || seconds === undefined || isNaN(seconds)) return "—";
        if (seconds < 90) return Math.round(seconds) + "s";
        if (seconds < 5400) return Math.round(seconds / 60) + "m";
        return (seconds / 3600).toFixed(1) + "h";
    }

    function toneClass(value) {
        if (value === null || value === undefined || isNaN(value) || Number(value) === 0) return "mk-flat";
        return Number(value) > 0 ? "mk-up" : "mk-down";
    }

    /* ---------------------------------------------------------------------
       Requests
       --------------------------------------------------------------------- */
    // A request that never answers is a failure too: without this, a hung fetch
    // leaves the page on its initial text with no way to tell it apart from a
    // slow one.
    async function getJSON(url, timeoutMs) {
        var controller = new AbortController();
        var timer = setTimeout(function () { controller.abort(); }, timeoutMs);
        try {
            var response = await fetch(url, { signal: controller.signal, headers: { Accept: "application/json" } });
            if (!response.ok) throw new Error("HTTP " + response.status);
            return await response.json();
        } finally {
            clearTimeout(timer);
        }
    }

    function loadScript(src, timeoutMs) {
        return new Promise(function (resolve) {
            var tag = document.createElement("script");
            var timer = null;
            var done = function (ok) {
                if (timer) clearTimeout(timer);
                resolve(ok);
            };
            timer = setTimeout(function () { done(false); }, timeoutMs || 10000);
            tag.src = src;
            tag.onload = function () { done(true); };
            tag.onerror = function () { done(false); };
            document.head.appendChild(tag);
        });
    }

    /* ---------------------------------------------------------------------
       The chart's palette
       ---------------------------------------------------------------------
       A canvas cannot read CSS: every colour the library draws has to be handed
       to it, and a theme change must hand it the new ones. So the palette is
       read from the stylesheet when the chart is built and again whenever
       `data-theme` moves — one source of truth, consulted twice. */
    function palette() {
        var style = getComputedStyle(document.documentElement);
        var read = function (name, fallback) {
            var value = (style.getPropertyValue(name) || "").trim();
            return value || fallback;
        };
        return {
            plot: read("--surface-card", "#17171a"),
            grid: read("--border", "#26262b"),
            axis: read("--text-muted", "#8b8883"),
            text: read("--text-secondary", "#a9a6a1"),
            accent: read("--accent", "#2dd4bf"),
            up: read("--green", "#23a55a"),
            down: read("--red", "#f23f43"),
        };
    }

    function themedChartOptions() {
        var c = palette();
        return {
            layout: { background: { type: "solid", color: c.plot }, textColor: c.text },
            grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
            crosshair: {
                vertLine: { color: c.axis, labelBackgroundColor: c.accent },
                horzLine: { color: c.axis, labelBackgroundColor: c.accent },
            },
            rightPriceScale: { borderColor: c.grid },
            timeScale: { borderColor: c.grid },
        };
    }

    function themedSeriesOptions() {
        var c = palette();
        return {
            candles: {
                upColor: c.up,
                downColor: c.down,
                borderUpColor: c.up,
                borderDownColor: c.down,
                wickUpColor: c.up,
                wickDownColor: c.down,
            },
            volume: { color: c.accent },
        };
    }

    function repaintChart() {
        if (!chartObj) return;
        chartObj.applyOptions(themedChartOptions());
        if (candleSeries) candleSeries.applyOptions(themedSeriesOptions().candles);
        if (volumeSeries) volumeSeries.applyOptions(themedSeriesOptions().volume);
    }

    function initChart() {
        if (typeof LightweightCharts === "undefined") return false;
        var wrap = el("chartWrap");
        if (!wrap) return false;
        wrap.innerHTML = "";
        var themed = themedChartOptions();
        chartObj = LightweightCharts.createChart(wrap, {
            ...themed,
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal, ...themed.crosshair },
            rightPriceScale: { ...themed.rightPriceScale, scaleMargins: { top: 0.08, bottom: 0.2 } },
            timeScale: { ...themed.timeScale, timeVisible: true, secondsVisible: false },
            handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true },
            handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
        });

        candleSeries = chartObj.addCandlestickSeries(themedSeriesOptions().candles);
        volumeSeries = chartObj.addHistogramSeries({
            ...themedSeriesOptions().volume,
            priceFormat: { type: "volume" },
            priceScaleId: "vol",
        });
        chartObj.priceScale("vol").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

        // One observer for the life of the chart: `data-theme` on `<html>` is the
        // only place the choice lives, whoever moved it.
        new MutationObserver(repaintChart).observe(document.documentElement, {
            attributes: true,
            attributeFilter: ["data-theme"],
        });

        var ro = new ResizeObserver(function () {
            if (chartObj) chartObj.applyOptions({ width: wrap.clientWidth, height: wrap.clientHeight });
        });
        ro.observe(wrap);
        return true;
    }

    function chartNotice(text) {
        var wrap = el("chartWrap");
        if (!wrap || chartObj) return;
        wrap.innerHTML = "";
        var box = document.createElement("div");
        box.className = "mk-chart-fallback";
        box.textContent = text;
        wrap.appendChild(box);
    }

    /* ---------------------------------------------------------------------
       The board
       --------------------------------------------------------------------- */
    function quoteFor(symbol) {
        if (!quotes || !quotes.quotes) return null;
        for (var i = 0; i < quotes.quotes.length; i += 1) {
            if (quotes.quotes[i].symbol === symbol) return quotes.quotes[i];
        }
        return null;
    }

    function feed(state, text) {
        var pill = el("mk-feed");
        if (pill) pill.setAttribute("data-state", state);
        var label = el("mk-feed-text");
        if (label) label.textContent = text;
    }

    function paintRow(row) {
        var price = document.querySelector('[data-price="' + row.symbol + '"]');
        if (price) {
            price.textContent = row.available ? coins(row.coins) : "—";
            if (row.available && row.symbol === SYMBOL && lastPrice !== null && row.coins !== lastPrice) {
                price.classList.remove("mk-tick");
                void price.offsetWidth;
                price.classList.add("mk-tick");
            }
        }
        var change = document.querySelector('[data-change="' + row.symbol + '"]');
        if (change) {
            change.textContent = row.available ? pct(row.change_pct) : "unavailable";
            change.className = "mk-row-change " + toneClass(row.change_pct);
        }
    }

    function paintHud() {
        var me = quoteFor(SYMBOL);
        var big = el("price");
        var change = el("change");
        if (!me) {
            if (big) big.textContent = "—";
            if (change) change.textContent = "no source carries this symbol right now";
            return;
        }
        if (big) {
            big.textContent = me.available ? coins(me.coins) : "unavailable";
            big.className = "mk-hud-big " + toneClass(me.change_pct);
        }
        if (change) {
            change.textContent = me.available
                ? pct(me.change_pct) + " · $" + fmt(me.price_usd, 2) + " · " + (me.source || "no source") + " · " + ago(me.age_seconds) + " old"
                : "no source carries this symbol right now";
            change.className = "mk-hud-change " + toneClass(me.change_pct);
        }
        lastPrice = me.available ? me.coins : null;
    }

    function paintSummary() {
        if (!quotes) return;
        var rate = el("rate");
        if (rate) rate.textContent = coins(quotes.coins_per_usd);
        var count = el("count");
        if (count) count.textContent = quotes.quotes.length + " · " + quotes.treat_fee_label;
        var sources = el("sources");
        if (sources) sources.textContent = quotes.sources.length ? quotes.sources.join(", ") : "none";
        var fresh = el("freshness");
        if (fresh) {
            fresh.textContent = quotes.max_quote_age_seconds + "s";
        }
        var fee = el("feeLabel");
        if (fee) fee.textContent = quotes.treat_fee_label;
        var feeRate = el("feeRate");
        if (feeRate) feeRate.textContent = (Number(quotes.treat_fee) * 100).toFixed(2) + "% of the spend, minimum 1";
        var age = el("feedAge");
        if (age) age.textContent = ago(quotes.age_seconds) + " ago";
        var missing = el("missing");
        if (missing) {
            missing.textContent = quotes.missing && quotes.missing.length
                ? quotes.missing.join(", ")
                : "none — every instrument answered";
        }
    }

    function paintWatchlist() {
        var host = el("mk-watch-list");
        if (!host || !quotes) return;
        var pinned = watchlist();
        host.textContent = "";
        if (!pinned.length) {
            var empty = document.createElement("p");
            empty.className = "mk-watch-empty";
            empty.textContent = "Nothing pinned yet. The star beside an instrument keeps it here, in this browser only.";
            host.appendChild(empty);
            return;
        }
        pinned.forEach(function (symbol) {
            var row = quoteFor(symbol);
            var line = document.createElement("a");
            line.className = "mk-row";
            line.href = "/market/" + symbol;
            var left = document.createElement("span");
            var sym = document.createElement("span");
            sym.className = "mk-row-sym";
            sym.textContent = symbol;
            var name = document.createElement("span");
            name.className = "mk-row-name";
            name.textContent = row && row.name ? row.name : "no reading";
            left.appendChild(sym);
            left.appendChild(name);
            var right = document.createElement("span");
            right.className = "mk-row-price";
            var value = document.createElement("b");
            value.textContent = row && row.available ? coins(row.coins) : "—";
            var delta = document.createElement("span");
            delta.className = toneClass(row && row.change_pct);
            delta.textContent = row && row.available ? pct(row.change_pct) : "—";
            right.appendChild(value);
            right.appendChild(delta);
            line.appendChild(left);
            line.appendChild(right);
            host.appendChild(line);
        });
    }

    function watchlist() {
        try {
            var raw = window.localStorage.getItem(watchKey);
            var parsed = raw ? JSON.parse(raw) : [];
            return Array.isArray(parsed) ? parsed : [];
        } catch (error) {
            return [];
        }
    }

    function saveWatchlist(list) {
        try {
            window.localStorage.setItem(watchKey, JSON.stringify(list));
        } catch (error) {
            /* A browser that refuses storage is not an error worth surfacing: the
               pins are a convenience, and the page works without them. */
        }
    }

    function paintWatchButtons() {
        var pinned = watchlist();
        document.querySelectorAll("[data-watch]").forEach(function (button) {
            var symbol = button.getAttribute("data-watch");
            var on = pinned.indexOf(symbol) !== -1;
            button.setAttribute("aria-pressed", on ? "true" : "false");
            button.textContent = on ? "★" : "☆";
            button.setAttribute(
                "aria-label",
                (on ? "Unpin " : "Pin ") + symbol + (on ? " from the watchlist" : " to the watchlist"),
            );
        });
    }

    function wireWatchButtons() {
        document.querySelectorAll("[data-watch]").forEach(function (button) {
            button.addEventListener("click", function () {
                var symbol = button.getAttribute("data-watch");
                var pinned = watchlist();
                var index = pinned.indexOf(symbol);
                if (index === -1) pinned.push(symbol);
                else pinned.splice(index, 1);
                saveWatchlist(pinned);
                paintWatchButtons();
                paintWatchlist();
            });
        });
    }

    async function loadQuotes() {
        try {
            quotes = await getJSON("/api/market/quotes", 20000);
            if (!quotes || !quotes.quotes) throw new Error("empty");
            var stale = Number(quotes.age_seconds) > Number(quotes.max_quote_age_seconds);
            feed(stale ? "stale" : "live", stale ? "stale — trading paused" : "live");
            quotes.quotes.forEach(paintRow);
            paintHud();
            paintSummary();
            paintWatchlist();
            paintWatchButtons();
            renderTicket();
            var status = el("status");
            if (status) {
                status.textContent = quotes.sources.length
                    ? (stale ? "stale — trading paused" : "live") +
                      " · prices from " + quotes.sources.join(", ") +
                      " · updated " + ago(quotes.age_seconds) + " ago"
                    : "No source could be reached right now — trading is paused.";
            }
        } catch (error) {
            // Say which failure it is: a first load that never landed, or a
            // refresh that did not, while the last good snapshot is still on
            // screen. A reply that parsed but carried no `as_of` is not a
            // snapshot, and claiming an age for it would be inventing a number.
            var age = quotes && Number.isFinite(quotes.as_of) ? Date.now() / 1000 - quotes.as_of : null;
            feed("down", age === null ? "offline" : "stale");
            var status = el("status");
            if (status) {
                status.textContent = age === null
                    ? "offline · could not reach the bot — retrying every 8s"
                    : "offline · showing the last snapshot (" + ago(age) + " old), retrying";
            }
        }
    }

    /* ---------------------------------------------------------------------
       History and the chart
       --------------------------------------------------------------------- */
    function prints(candles) {
        var body = el("mk-prints-body");
        if (!body) return;
        body.textContent = "";
        candles.slice(-8).reverse().forEach(function (row) {
            var tr = document.createElement("tr");
            var when = document.createElement("td");
            // `row.time`, not `row.ts`: the candles are mapped into the chart's
            // shape (`{time, open, …}`) before they get here, and reading the
            // API's own field name off that shape produced `NaN` for every row —
            // a table whose first column read "Invalid Date" eight times, under a
            // caption promising the most recent candles.
            when.textContent = new Date(Math.floor(row.time) * 1000).toLocaleString("en-US", {
                month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
            });
            tr.appendChild(when);
            ["open", "high", "low", "close"].forEach(function (key) {
                var cell = document.createElement("td");
                cell.textContent = fmt(row[key], 6);
                if (key === "close") {
                    cell.className = row.close >= row.open ? "mk-up" : "mk-down";
                }
                tr.appendChild(cell);
            });
            body.appendChild(tr);
        });
    }

    async function loadCandles() {
        try {
            var series = await getJSON(
                "/api/market/candles?symbol=" + encodeURIComponent(SYMBOL) + "&period=" + encodeURIComponent(period),
                25000,
            );
            if (!series || !series.candles || series.candles.length === 0) throw new Error("empty");
            var candles = series.candles.map(function (c) {
                return { time: Math.floor(c.ts), open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume };
            });

            // Stats and the prints table are computed from the *data*, so the
            // numbers keep working when the chart library never arrives.
            var first = candles[0];
            var last = candles[candles.length - 1];
            var change = first.close ? ((last.close - first.close) / first.close) * 100 : 0;
            var periodMove = el("periodMove");
            if (periodMove) {
                periodMove.textContent = pct(change);
                periodMove.className = toneClass(change);
            }
            var source = el("candleSource");
            if (source) source.textContent = (series.source || "no source") + " · " + candles.length + " points";
            var axis = el("axisNote");
            if (axis) axis.textContent = "· coins" + (series.observed ? " · observations" : "");
            var note = el("historyNote");
            if (note) {
                note.textContent = series.observed
                    ? "This bot's own polling — a line of observed prices rather than exchange candles."
                    : "Candles from the exchange, with open, high, low and close.";
            }
            prints(candles);
            paintMeter(change);

            if (!candleSeries || !volumeSeries || !chartObj) return;

            candleSeries.setData(candles);

            var volumes = candles.map(function (c) {
                return {
                    time: c.time,
                    value: c.volume || 0,
                    color: c.close >= c.open ? "rgba(35,165,90,0.32)" : "rgba(242,63,67,0.32)",
                };
            });
            volumeSeries.setData(volumes);
            chartObj.timeScale().fitContent();
        } catch (error) {
            var note = el("historyNote");
            if (note) note.textContent = "Could not load history — retrying every minute.";
        }
    }

    function paintMeter(change) {
        var bar = el("mk-move-meter");
        if (!bar) return;
        var magnitude = Math.min(1, Math.abs(Number(change) || 0) / 10);
        bar.style.transform = "scaleX(" + magnitude.toFixed(3) + ")";
        bar.style.background = Number(change) >= 0 ? "var(--green)" : "var(--red)";
        var label = el("mk-move-label");
        if (label) label.textContent = "Period move " + pct(change);
    }

    /* ---------------------------------------------------------------------
       The order ticket
       ---------------------------------------------------------------------
       A *calculator*, and it says so: this page is public, the order endpoint
       does not exist here, and a Buy button that looked real and did nothing
       would be the one dishonest control on the surface. The numbers under it
       are the same ones a fill would use — the observed price and the fee — so
       what a reader works out here is what would happen in Discord. */
    function renderTicket() {
        var me = quoteFor(SYMBOL);
        var spendNode = el("spend");
        /* A negative or unparseable spend clamps to zero *and is reported as
           zero*: `Math.max(0, NaN)` is NaN, and a ticket reading "0 units" beside
           an enabled Buy is a control offering an order nobody can place. */
        var typed = Number(spendNode && spendNode.value);
        var spend = Number.isFinite(typed) ? Math.max(0, typed) : 0;
        var available = !!(me && me.available);
        var belowMinimum = MIN_TRADE > 0 && spend < MIN_TRADE;
        var aboveMaximum = MAX_TRADE > 0 && spend > MAX_TRADE;
        var fillable = available && !belowMinimum && !aboveMaximum;
        /* The control is a *copier*, not a Buy button: it puts the exact command
           on the clipboard and says so — including the amount, because
           `/market buy BTC` with no amount is not the order the ticket just
           worked out. A button labelled "Buy" that quietly did nothing would be
           the one dishonest control on this page, and so is one that offers a
           fill the bot will refuse. */
        var call = el("mk-call");
        if (call) {
            call.setAttribute("aria-disabled", fillable ? "false" : "true");
            if (call.getAttribute("data-copied") === "true") {
                /* leave the confirmation in place until the price changes */
            } else if (!available) {
                call.textContent = "No price — trading paused";
            } else if (belowMinimum) {
                call.textContent = "The smallest fill is " + coins(MIN_TRADE);
            } else if (aboveMaximum) {
                call.textContent = "The largest fill is " + coins(MAX_TRADE);
            } else {
                call.textContent = "Copy " + orderCommand(spend);
            }
        }
        if (!fillable) {
            ["units", "unitPrice", "treat", "notional"].forEach(function (id) {
                var node = el(id);
                if (node) node.textContent = "—";
            });
            return;
        }
        var fee = Math.max(spend > 0 ? 1 : 0, Math.round(spend * Number(quotes.treat_fee || 0)));
        var notional = Math.max(0, spend - fee);
        var units = notional / me.coins;
        var unitPrice = el("unitPrice");
        if (unitPrice) unitPrice.textContent = coins(me.coins);
        var treat = el("treat");
        if (treat) treat.textContent = "− " + coins(fee);
        var left = el("notional");
        if (left) left.textContent = coins(notional);
        var unitsNode = el("units");
        if (unitsNode) {
            unitsNode.textContent = units >= 1 ? fmt(units, 4) + " " + SYMBOL : fmt(units, 8) + " " + SYMBOL;
        }
        var call2 = el("mk-call");
        if (call2) {
            call2.textContent = "Copy " + orderCommand(spend);
        }
    }

    /* The command line this ticket stands for, and the button's label is the
       string that goes on the clipboard — a label that said "10,000 🪙" while
       the clipboard got `10000` would be one more place the page and the bot
       disagree about the same order. */
    function orderCommand(spend) {
        var amount = Number.isFinite(Number(spend)) ? Math.round(Math.max(0, Number(spend))) : 0;
        return "/market buy " + SYMBOL + " " + amount;
    }

    function copyCommand(text, button) {
        var done = function (ok) {
            button.setAttribute("data-copied", ok ? "true" : "false");
            button.textContent = ok
                ? "Copied — paste it in Discord"
                : "Copy " + text + " by hand";
            if (ok) setTimeout(function () {
                button.setAttribute("data-copied", "false");
                renderTicket();
            }, 2600);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
            return;
        }
        // No clipboard API (an old browser, or a page served over plain http):
        // the command is printed instead of failing silently.
        done(false);
    }

    function wireTicket() {
        var spend = el("spend");
        if (spend) spend.addEventListener("input", renderTicket);
        var call = el("mk-call");
        if (call) {
            call.addEventListener("click", function () {
                var me = quoteFor(SYMBOL);
                if (!me || !me.available) return;
                var typed = Number(spend && spend.value);
                var amount = Number.isFinite(typed) ? Math.round(Math.max(0, typed)) : 0;
                // The window is re-checked here rather than trusted from the last
                // render: this is the click that puts a command on a clipboard.
                if (MIN_TRADE > 0 && amount < MIN_TRADE) return;
                if (MAX_TRADE > 0 && amount > MAX_TRADE) return;
                copyCommand(orderCommand(amount), call);
            });
        }
        document.querySelectorAll("[data-size]").forEach(function (button) {
            button.addEventListener("click", function () {
                var size = button.getAttribute("data-size");
                if (spend) {
                    spend.value = size;
                    renderTicket();
                }
            });
        });
    }

    function wirePeriods() {
        var group = el("periods");
        if (!group) return;
        group.addEventListener("click", function (event) {
            var button = event.target.closest ? event.target.closest("button[data-period]") : null;
            if (!button) return;
            period = button.getAttribute("data-period");
            Array.prototype.forEach.call(group.querySelectorAll("button"), function (other) {
                var on = other === button;
                other.classList.toggle("is-on", on);
                other.setAttribute("aria-pressed", String(on));
            });
            loadCandles();
        });
    }

    function wireSearch() {
        var input = el("mk-search");
        if (!input) return;
        input.addEventListener("input", function () {
            var needle = input.value.trim().toLowerCase();
            var shown = 0;
            document.querySelectorAll("[data-instrument]").forEach(function (wrap) {
                var haystack = (wrap.getAttribute("data-instrument") || "").toLowerCase();
                var match = !needle || haystack.indexOf(needle) !== -1;
                wrap.hidden = !match;
                if (match) shown += 1;
            });
            var empty = el("mk-search-empty");
            if (empty) empty.hidden = shown !== 0;
        });
    }

    /* ---------------------------------------------------------------------
       Chart bring-up
       ---------------------------------------------------------------------
       Deliberately *not* where the prices are loaded: a wall of CDN attempts
       must not stand in front of the numbers. */
    async function startChart() {
        if (typeof LightweightCharts === "undefined") {
            chartNotice("Loading the chart… the prices on this page are live either way.");
            for (var i = 0; i < CHART_FALLBACKS.length; i += 1) {
                var loaded = await loadScript(CHART_FALLBACKS[i]);
                if (loaded) break;
            }
        }
        var ready = false;
        try {
            ready = initChart();
        } catch (error) {
            // A library that loads but cannot draw must not take the numbers with it.
            console.error("Meow Markets: chart init failed", error);
            if (chartObj) {
                try { chartObj.remove(); } catch (ignored) { /* already gone */ }
            }
            var wrap = el("chartWrap");
            if (wrap) wrap.innerHTML = "";
            chartObj = candleSeries = volumeSeries = null;
            ready = false;
        }
        await loadCandles();
        if (!ready) {
            chartNotice(
                "⚠️ The chart library could not be loaded, so the graph is unavailable — " +
                "the price, the period move, the prints below and the ticket are all live.",
            );
        }
    }

    /* ---------------------------------------------------------------------
       Bootstrap
       --------------------------------------------------------------------- */
    /* One disclosure — how a fill works — animated by `anim.js`, which is the
       same open/close the rest of the project's drawers use. This only has to
       find each trigger's panel and hand them over. */
    function wireDrawers() {
        if (!window.MeowAnim || !window.MeowAnim.panel) return;
        document.querySelectorAll("[data-drawer]").forEach(function (trigger) {
            var target = document.getElementById(trigger.getAttribute("data-drawer"));
            if (!target) return;
            window.MeowAnim.panel(trigger, target);
        });
    }

    wirePeriods();
    wireTicket();
    wireWatchButtons();
    wireSearch();
    wireDrawers();
    paintWatchButtons();

    loadQuotes();
    startChart();
    pollWhileVisible(loadQuotes, QUOTE_POLL_MS);
    pollWhileVisible(loadCandles, CANDLE_POLL_MS);
})();
