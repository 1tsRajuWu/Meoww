"""Serve the real dashboard and market templates with stub APIs, to eyeball them.

Usage (run from the repo root): ``python scripts/preview_dashboard.py``
then open http://127.0.0.1:8099/ (or the port it prints — ``PORT`` moves it).

Verification harness only: it renders ``templates/dashboard.html`` and
``templates/market_live.html`` exactly as their routes do, and answers the
endpoints those pages call with plausible data so their own JavaScript runs end
to end. Nothing here is imported by the bot.

It writes to a **copy** of the database, in a temporary directory — not to the
project's ``bot.db``. The pages here toggle real modules and queue real
scheduled messages, so a preview that shares the live database is a preview that
edits production state.

It has no session, and says so: ``/login`` and ``/logout`` (the header's own
Sign out button) answer a page explaining that instead of Flask's 404.

Where a payload has a builder in the real code — the market's quotes and
candles, the module catalog, the scheduling limits — this file calls it rather
than describing it, because a stub that is *written* to look right is a stub
that drifts, and the page then gets blamed for the harness's shape.
"""
import os
import shutil
import sys
import tempfile
import time

from flask import Flask, jsonify, redirect, render_template, request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# This harness gets its own copy of the database
#
# The pages it serves write through the *real* ``utils.db`` — module toggles,
# scheduled messages, channel bindings — so driving this preview used to change
# the operator's live state. It did: a click on the bulk toggle during a
# playthrough turned every module off in the Development server, on the database
# the running bot then uploaded as the production backup, and the symptom was
# commands answering "the X system is currently turned off" in the only server
# the operator was watching.
#
# A copy, not a promise: what a person clicks here is discarded with the process,
# and the live file is never opened. It is made *before* ``keep_alive`` is
# imported, because that import is what reads the path.
_LIVE_DB = os.path.join(ROOT, "bot.db")
_WORKDIR = tempfile.mkdtemp(prefix="meow-preview-")
_PREVIEW_DB = os.path.join(_WORKDIR, "bot.db")
if os.path.exists(_LIVE_DB):
    shutil.copy2(_LIVE_DB, _PREVIEW_DB)

from utils import db as _db  # noqa: E402

_db.DB_PATH = _PREVIEW_DB

app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))

# The real app's security headers, applied to every response — otherwise this
# harness would prove nothing about whether the pages survive our own CSP.
import keep_alive  # noqa: E402
from utils import digest, market as market_module  # noqa: E402


@app.after_request
def _old_policy_for_comparison(response):
    """``?csp=old`` reproduces the header that blocked the chart library.

    Registered *before* the real hook: Flask runs after-request handlers in
    reverse registration order, so this one runs last and wins.
    """
    if request.args.get("csp") == "old":
        response.headers["Content-Security-Policy"] = response.headers[
            "Content-Security-Policy"
        ].replace(
            "'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net;",
            "'self' 'unsafe-inline';",
        )
    return response


app.after_request(keep_alive.set_security_headers)

#: What the page last asked a toggle endpoint for. Same idea as ``LAST_SEND``:
#: it exists so a check can ask the page's question and read the answer back.
LAST_TOGGLE: dict = {}

MODULES = [
    {"id": "economy", "name": "Economy", "icon": "🪙", "category": "Economy",
     "desc": "Wallets, bank, daily rewards and the Meow Fee.", "commands": ["/balance", "/daily"], "enabled": True},
    {"id": "tickets", "name": "Support Tickets", "icon": "🎟️", "category": "Support",
     "desc": "Interactive ticket panels with private staff channels.", "commands": ["/ticket setup"], "enabled": True},
    {"id": "music", "name": "Music", "icon": "🎵", "category": "Voice",
     "desc": "Retired.", "commands": ["/play"], "enabled": False},
]

#: Real-sized ids, as strings, on purpose. A snowflake this big (bigger than
#: 2**53) is the only kind that can show a browser rounding it, and that is what
#: "every send answers *that channel no longer exists*" actually was: stub ids of
#: 1 and 2 survive any arithmetic, so this harness used to prove nothing about the
#: one number the page has to hand back untouched.
CHANNELS = [
    {"id": "1548437081120833657", "name": "general", "category": "Community", "kind": "text", "can_send": True, "slowmode": 0, "is_news": False},
    {"id": "1548437081120833658", "name": "announcements", "category": "Community", "kind": "text", "can_send": True, "slowmode": 0, "is_news": True},
    {"id": "1548437081120833659", "name": "memes", "category": "Community", "kind": "text", "can_send": True, "slowmode": 30, "is_news": False},
    {"id": "1548437081120833660", "name": "staff-chat", "category": "Staff", "kind": "text", "can_send": True, "slowmode": 0, "is_news": False},
    {"id": "1548437081120833661", "name": "mod-logs", "category": "Staff", "kind": "text", "can_send": False, "slowmode": 0, "is_news": False},
    {"id": "1548437081120833662", "name": "welcome", "category": "No category", "kind": "text", "can_send": False, "slowmode": 0, "is_news": False},
    # Voice channels, because a voice channel has a text chat of its own and a
    # message can land in it — the picker used to leave them out entirely.
    {"id": "1548437081120833663", "name": "General Voice", "category": "Voice", "kind": "voice", "can_send": True, "slowmode": 0, "is_news": False},
    {"id": "1548437081120833664", "name": "Music", "category": "Voice", "kind": "voice", "can_send": False, "slowmode": 0, "is_news": False},
]

MEMBERS = [
    {"id": "1542296506290143349", "label": "RAJ", "sub": "@raj", "kind": "user"},
    {"id": "1542296506290143350", "label": "Meow", "sub": "@meow", "kind": "user"},
    {"id": "1542296506290143351", "label": "Bob", "sub": "@bob", "kind": "user"},
    {"id": "1542296506290143352", "label": "Bobette", "sub": "@bobette", "kind": "user"},
]

ROLES = [
    {"id": "1546716293657530368", "label": "Members", "kind": "role"},
    {"id": "1546716293657530369", "label": "Moderator", "kind": "role"},
]

SPECIAL = [
    {"id": "@everyone", "label": "@everyone", "sub": "everyone in the server", "kind": "special"},
    {"id": "@here", "label": "@here", "sub": "everyone online right now", "kind": "special"},
]


#: The servers this harness pretends the bot is in. The page's own switcher
#: navigates to ``/dashboard?guild_id=``, so the preview has to answer for the
#: server that was asked for: it used to render the first one whatever the query
#: said, which made the switcher look broken in the one place it gets checked.
GUILDS = [
    {"id": 777001, "name": "Meow Server", "members": 1627},
    {"id": 777002, "name": "Second Server", "members": 42},
]


@app.route("/")
def home():
    """The entry point this module's own usage line tells you to open.

    Production answers ``/`` for both a monitor and a person — a liveness line,
    then a redirect into the dashboard (or the sign-in). Opening the printed
    port here used to land on Flask's 404, so the documented first step of
    checking the dashboard was itself the thing that looked broken. There is no
    sign-in to redirect to in a preview, so a browser goes straight in.
    """
    user_agent = request.headers.get("User-Agent", "").lower()
    if "uptimerobot" in user_agent or "curl" in user_agent:
        return "Meow Bot is alive and operational!"
    return redirect("/dashboard")


@app.route("/dashboard")
def dashboard():
    try:
        guild_id = int(request.args.get("guild_id"))
    except (TypeError, ValueError):
        guild_id = GUILDS[0]["id"]
    # An unknown id falls back to the first server, exactly as
    # ``keep_alive._resolve_guild_id`` does, so the select can never point at a
    # server it does not also list.
    guild = next((g for g in GUILDS if g["id"] == guild_id), GUILDS[0])
    return render_template(
        "dashboard.html",
        guild_id=guild["id"],
        guild_name=guild["name"],
        member_count=guild["members"],
        bot_ping=int(os.environ.get("PREVIEW_PING", "41")),
        bot_ready=True,
        modules=MODULES,
        active_count=2,
        total_count=3,
        csrf_token="preview-token",
        guilds=[{"id": g["id"], "name": g["name"]} for g in GUILDS],
        can_switch_guild=True,
        composer_max_channels=keep_alive.MESSAGE_MAX_CHANNELS,
        # The real scheduling limits, so the page cannot look right here while the
        # endpoint would refuse what it offers.
        schedule_repeat_choices=[
            {"minutes": minutes, "label": label}
            for minutes, label in keep_alive.scheduled.REPEAT_CHOICES
        ],
        schedule_max_pending=keep_alive.scheduled.MAX_PENDING_PER_GUILD,
        schedule_catchup_hours=keep_alive.scheduled.MISS_CATCHUP_SECONDS // 3600,
        # The real content sources, from the registry the endpoint validates
        # against — without this the picker offered "Message I write" and
        # nothing else, so the one scheduled-message kind that builds its own
        # text could not be reached from the preview at all.
        schedule_content_choices=[
            {"value": "", "label": "Message I write", "blurb": ""},
            *[
                {"value": name, "label": entry["label"], "blurb": entry["blurb"]}
                for name, entry in digest.GENERATORS.items()
            ],
        ],
        server_now=int(time.time()),
    )


@app.route("/market")
def market_index():
    """Bare ``/market`` — production redirects it to the first instrument.

    The dashboard's own 📈 Market button lands here, so a preview that answered
    Flask's 404 made the one link to the charts look broken.
    """
    return redirect("/market/BTC")


@app.route("/login", methods=["GET", "POST"])
@app.route("/logout", methods=["GET", "POST"])
def no_session():
    """Say there is no session here rather than 404-ing on a visible button.

    The rendered page carries the real header, Sign out included, so a playtester
    clicks it — and a 404 reads as "the dashboard is broken" when the truth is
    that this harness has no sign-in to begin with. Production sends both of these
    to `/login`; there is no sign-in here, so it says that instead of pretending.
    """
    return (
        "<h1>Preview harness — no sign-in</h1>"
        "<p>This page renders the real dashboard's templates with stand-in data; "
        "it has no session, so there is nothing to sign in to or out of."
        "</p>"
        "<p><a href='/dashboard'>Back to the dashboard</a></p>",
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )


@app.route("/market/<symbol>")
def market_page(symbol):
    """The real market route, so the chart can be checked under the real CSP."""
    from utils import market as market_module

    instrument = market_module.BY_SYMBOL.get(str(symbol).strip().upper())
    if instrument is None:
        # Production's own refusal, in production's own words: a misspelt symbol
        # is a thing people do, and "404 nope" tells them nothing. Same sentence,
        # same way out, so the preview cannot flatter the real page.
        safe = str(symbol).replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;").replace('"', "&quot;")
        return (
            f"<h1>404 — {safe} is not on the market</h1>"
            "<p><a href='/market/BTC'>Open BTC instead</a></p>",
            404,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    return render_template(
        "market_live.html",
        symbol=instrument.symbol,
        name=instrument.name,
        kind=instrument.kind,
        periods=list(market_module.PERIODS.items()),
        default_period=market_module.DEFAULT_PERIOD,
    )


#: A few plausible prices for the preview board, in USD.
_PREVIEW_PRICES = {"BTC": 96400.0, "ETH": 3180.0, "SOL": 182.0, "XRP": 2.15, "BNB": 610.0}


def _preview_snapshot():
    """A small, real snapshot — built by the code the live endpoint calls.

    The shapes here are the endpoint's, not this file's: a hand-written
    ``{"quotes": {}}`` looked fine and was not. The page does
    ``quotes.quotes.find(...)``, so an object threw a TypeError that its own
    ``catch`` reported as "could not reach the bot" — a permanent outage drawn
    over a page whose every request had answered 200. Going through
    ``quotes_payload`` means the preview cannot drift from production again.
    """
    now = time.time()
    quotes = {
        instrument.symbol: market_module.Quote(
            symbol=instrument.symbol,
            price_usd=_PREVIEW_PRICES.get(instrument.symbol, 100.0 + index),
            change_pct=((index % 7) - 3) * 0.75,
            source="preview",
            # Fresh enough to be tradable, so the board shows a live page rather
            # than the paused one.
            as_of=now - 12,
        )
        for index, instrument in enumerate(market_module.INSTRUMENTS[:5])
    }
    return market_module.Snapshot(quotes)


def _preview_series(symbol: str):
    """A short history shaped the way the bot's own polling is: flat points.

    Labelled as observed rather than as exchange OHLC on purpose — the page
    prints that label, and claiming "real candle data" over points nobody traded
    is the kind of thing this project keeps taking out.
    """
    now = time.time()
    start = _PREVIEW_PRICES.get(symbol, 100.0)
    candles = []
    for i in range(60):
        price = round(start * (1 + ((i % 11) - 5) / 100.0), 4)
        candles.append(market_module.Candle(
            ts=now - (60 - i) * 86400,
            open=price, high=price, low=price, close=price,
        ))
    return market_module.CandleSeries(
        symbol=symbol, candles=tuple(candles),
        source="this bot's own polling", axis_label="observed prices",
    )


@app.route("/api/market/quotes")
def api_market_quotes():
    return jsonify(market_module.quotes_payload(_preview_snapshot(), time.time()))


@app.route("/api/market/candles")
def api_market_candles():
    symbol = str(request.args.get("symbol") or "BTC").strip().upper()
    period = str(request.args.get("period") or market_module.DEFAULT_PERIOD)
    if period not in market_module.PERIODS:
        period = market_module.DEFAULT_PERIOD
    return jsonify(market_module.series_payload(symbol, _preview_series(symbol), period))


@app.route("/api/channels")
def api_channels():
    return jsonify({"success": True, "guild_id": 777001, "channels": CHANNELS})


@app.route("/api/mentionables")
def api_mentionables():
    query = (request.args.get("q") or "").strip().lower()
    if not query:
        return jsonify({"success": True, "guild_id": 777001, "users": [], "roles": ROLES, "special": SPECIAL})
    users = [m for m in MEMBERS if query in m["label"].lower() or query in m["sub"].lower()]
    roles = [r for r in ROLES if query in r["label"].lower()]
    special = SPECIAL if query in ("@", "every", "here", "e") else []
    return jsonify({"success": True, "guild_id": 777001, "users": users, "roles": roles, "special": special})


#: Whatever the browser last posted, so a human — or an agent driving the page
#: with a browser — can read the ids it handed back instead of guessing.
LAST_SEND: dict = {}


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    """Flip one module, the way the real endpoint does.

    Missing until a playtest drove the switches in a browser: every toggle was
    answered with the Flask 404 page, so the dashboard's headline control showed
    a red "Request failed (404)" toast and a switch that sprang back — the page
    looked broken when only the harness was. Nothing here writes anything down;
    the point is that the page's own optimistic-update-and-revert path can be
    exercised at all.
    """
    payload = request.get_json(silent=True) or {}
    if payload.get("guild_id") is None:
        return jsonify({"success": False, "error": "No guild available"}), 400
    module_id = payload.get("module")
    enabled = bool(payload.get("enabled"))
    module = next((m for m in MODULES if m["id"] == module_id), None)
    if module is None:
        # The real route's own refusal, status and words: an unknown module is a
        # 400 "Unknown module", and a harness that answers differently teaches
        # the page's error path the wrong lesson.
        return jsonify({"success": False, "error": "Unknown module"}), 400
    module["enabled"] = enabled
    LAST_TOGGLE.clear()
    LAST_TOGGLE.update({"module": module_id, "enabled": enabled})
    return jsonify({
        "success": True,
        "guild_id": payload.get("guild_id"),
        "module": module_id,
        "enabled": enabled,
        "sync_latency_ms": 0,
    })


@app.route("/api/toggle-all", methods=["POST"])
def api_toggle_all():
    """Enable or disable every module at once — the real route's shape."""
    payload = request.get_json(silent=True) or {}
    if payload.get("guild_id") is None:
        return jsonify({"success": False, "error": "No guild available"}), 400
    enabled = bool(payload.get("enabled"))
    for module in MODULES:
        module["enabled"] = enabled
    LAST_TOGGLE.clear()
    LAST_TOGGLE.update({"module": "*", "enabled": enabled})
    return jsonify({
        "success": True,
        "guild_id": payload.get("guild_id"),
        "enabled": enabled,
        "sync_latency_ms": 0,
    })


@app.route("/__last-toggle")
def last_toggle():
    """What the page last asked for — the same shape as ``/__last-send``."""
    return jsonify(LAST_TOGGLE)


@app.route("/api/send-message", methods=["POST"])
def api_send_message():
    """Resolve the posted ids the way the real endpoint does.

    An id the page rounded is an id nothing matches, and the answer says so in
    the same words the bot uses — which is the whole failure this harness is
    here to make visible.
    """
    LAST_SEND.clear()
    LAST_SEND.update(request.get_json(silent=True) or {})
    by_id = {c["id"]: c for c in CHANNELS}
    sent, failed = [], []
    for cid in LAST_SEND.get("channel_ids") or []:
        channel = by_id.get(str(cid))
        if channel is None:
            failed.append({"channel_id": cid, "name": None, "error": "That channel no longer exists"})
        else:
            sent.append({
                "channel_id": cid,
                "name": channel["name"],
                "jump_url": f"https://discord.com/channels/777001/{cid}/1",
            })
    # The real endpoint files every immediate send as a finished history row
    # (``utils.scheduled.record_sent``); mirrored here so the page's "sent now"
    # row can be seen and driven in a browser.
    results = [
        {"channel_id": item["channel_id"], "name": item["name"], "ok": True,
         "error": "", "jump_url": item["jump_url"]}
        for item in sent
    ] + [
        {"channel_id": item["channel_id"], "name": item["name"], "ok": False,
         "error": item["error"], "jump_url": ""}
        for item in failed
    ]
    now = int(time.time())
    SCHEDULE.append({
        "id": max([r["id"] for r in SCHEDULE] + [0]) + 1,
        "channel_ids": [str(item["channel_id"]) for item in results],
        "payload": {"content": LAST_SEND.get("content") or ""},
        "preview": LAST_SEND.get("content") or "(an embed)",
        "run_at": now,
        "repeat_minutes": 0,
        "source": "now",
        "status": "sent" if sent else "failed",
        "created_by_name": "RAJ",
        "last_run_at": now,
        "last_error": "" if not failed else failed[0]["error"],
        "results": results,
        "fire_count": 1,
    })
    return jsonify({
        "success": bool(sent),
        "results": [],
        "sent": sent,
        "failed": failed,
        "ignored_mentions": [],
    })


#: Whatever the browser last posted to the scheduling endpoints, and a small
#: in-memory queue behind them, so the page's scheduling path can be driven end to
#: end. The validation is the *real* function (``keep_alive._schedule_timing``), so
#: this harness shows the wording the bot actually answers with.
LAST_SCHEDULE: dict = {}
SCHEDULE: list = []


def _seed_queue():
    now = int(time.time())
    SCHEDULE.append({
        "id": 1,
        "channel_ids": ["1548437081120833657", "1548437081120833658"],
        "payload": {"content": "@everyone Weekly town hall starts in an hour", "everyone": True},
        "preview": "@everyone Weekly town hall starts in an hour",
        "run_at": now + 6 * 3600,
        "repeat_minutes": 10080,
        "source": "schedule",
        "status": "pending",
        "created_by_name": "RAJ",
        "last_run_at": now - 86400,
        "last_error": "1 channel(s) refused it: I cannot send messages in #mod-logs",
        # A real row carries the name for anything it could resolve (see
        # ``keep_alive``'s per-channel results); only the two "I cannot find that
        # channel" refusals have no name to carry. Seeding a *successful*
        # channel without one made the page's id fallback look like a defect in
        # a browser, when it is the honest answer for a channel that is gone.
        "results": [
            {"channel_id": "1548437081120833657", "name": "general", "ok": True,
             "error": "",
             "jump_url": "https://discord.com/channels/777001/1548437081120833657/1"},
            {"channel_id": "1548437081120833661", "name": "mod-logs", "ok": False,
             "error": "I cannot send messages in #mod-logs", "jump_url": ""},
        ],
        "fire_count": 3,
    })
    SCHEDULE.append({
        "id": 2,
        "channel_ids": ["1548437081120833663"],
        "payload": {"content": "Movie night in the voice channel"},
        "preview": "Movie night in the voice channel",
        "run_at": now + 400,
        "repeat_minutes": 0,
        "source": "schedule",
        "status": "pending",
        "created_by_name": "Meow",
        "last_run_at": None,
        "last_error": "",
        "results": [],
        "fire_count": 0,
    })


_seed_queue()


def _schedule_view(row: dict) -> dict:
    by_id = {c["id"]: c for c in CHANNELS}
    channels = []
    for cid in row["channel_ids"]:
        channel = by_id.get(str(cid))
        channels.append({
            "id": str(cid),
            "name": channel["name"] if channel else None,
            "kind": channel["kind"] if channel else "",
            "exists": bool(channel),
        })
    repeat = dict(keep_alive.scheduled.REPEAT_CHOICES).get(row["repeat_minutes"], "Once")
    return {
        **row,
        "channels": channels,
        "repeat": repeat,
        "has_everyone": "@everyone" in str(row["payload"].get("content") or ""),
        "channel_count": len(channels),
    }


@app.route("/api/scheduled")
def api_scheduled():
    return jsonify({
        "success": True,
        "guild_id": 777001,
        "server_now": int(time.time()),
        "upcoming": [_schedule_view(r) for r in SCHEDULE if r["status"] in ("pending", "sending")],
        "recent": sorted(
            (_schedule_view(r) for r in SCHEDULE if r["last_run_at"]),
            key=lambda r: r["last_run_at"],
            reverse=True,
        ),
        "pending": sum(1 for r in SCHEDULE if r["status"] == "pending"),
        "max_pending": keep_alive.scheduled.MAX_PENDING_PER_GUILD,
        "catchup_hours": keep_alive.scheduled.MISS_CATCHUP_SECONDS // 3600,
    })


@app.route("/api/schedule-message", methods=["POST"])
def api_schedule_message():
    body = request.get_json(silent=True) or {}
    LAST_SCHEDULE.clear()
    LAST_SCHEDULE.update(body)
    now = int(time.time())
    run_at, repeat, error = keep_alive._schedule_timing(body, now)
    if error:
        return jsonify({"success": False, "error": error, "server_now": now}), 400
    if not body.get("channel_ids"):
        return jsonify({"success": False, "error": "Pick at least one channel."}), 400
    if not str(body.get("content") or "").strip() and not body.get("title"):
        return jsonify({"success": False, "error": "Write something to send."}), 400
    by_id = {c["id"]: c for c in CHANNELS}
    if any(str(cid) not in by_id for cid in body.get("channel_ids") or []):
        return jsonify({
            "success": False,
            "error": "That channel no longer exists — reload the page and pick one again.",
            "server_now": now,
        }), 400

    mentions = [str(m) for m in (body.get("mentions") or [])]
    user_ids = {m["id"] for m in MEMBERS}
    content = str(body.get("content") or "").strip()
    ping = " ".join(tag for tag in mentions if tag in ("@everyone", "@here"))
    content = (ping + "\n" + content).strip() if ping else content
    row = {
        "id": (max((r["id"] for r in SCHEDULE), default=0) + 1),
        "channel_ids": [str(cid) for cid in body["channel_ids"]],
        "payload": {
            "content": content,
            "embed": {"title": body.get("title")} if body.get("embed") else None,
            "users": sorted(uid for uid in mentions if uid in user_ids),
            "everyone": bool(ping),
            "ignored_mentions": sorted(
                tag for tag in mentions
                if tag not in user_ids and tag not in ("@everyone", "@here")
                and tag not in {r["id"] for r in ROLES}
            ),
        },
        "preview": " ".join(content.split())[:140],
        "run_at": run_at,
        "repeat_minutes": repeat,
        "status": "pending",
        "created_by_name": "RAJ",
        "last_run_at": None,
        "last_error": "",
        "results": [],
        "fire_count": 0,
    }
    SCHEDULE.append(row)
    return jsonify({
        "success": True,
        "server_now": now,
        "message": _schedule_view(row),
        "ignored_mentions": row["payload"]["ignored_mentions"],
        "pending": sum(1 for r in SCHEDULE if r["status"] == "pending"),
        "max_pending": keep_alive.scheduled.MAX_PENDING_PER_GUILD,
    })


def _find(msg_id):
    return next((r for r in SCHEDULE if r["id"] == msg_id), None)


@app.route("/api/scheduled/<int:msg_id>/cancel", methods=["POST"])
def api_cancel_scheduled(msg_id: int):
    row = _find(msg_id)
    if row is None:
        return jsonify({"success": False, "error": "That is not a scheduled message in this server."}), 404
    if row["status"] != "pending":
        return jsonify({"success": False, "error": f"That message is already {row['status']}."}), 409
    row["status"] = "cancelled"
    row["last_run_at"] = int(time.time())
    row["last_error"] = "cancelled from the dashboard"
    return jsonify({"success": True, "id": msg_id, "server_now": int(time.time())})


@app.route("/api/scheduled/<int:msg_id>/fire", methods=["POST"])
def api_fire_scheduled(msg_id: int):
    row = _find(msg_id)
    if row is None:
        return jsonify({"success": False, "error": "That is not a scheduled message in this server."}), 404
    if row["status"] != "pending":
        return jsonify({"success": False, "error": f"That message is already {row['status']}."}), 409
    by_id = {c["id"]: c for c in CHANNELS}
    now = int(time.time())
    sent, failed = [], []
    results = []
    for cid in row["channel_ids"]:
        channel = by_id.get(str(cid))
        if channel is None or not channel["can_send"]:
            reason = "That channel no longer exists" if channel is None else (
                f"I cannot send messages in #{channel['name']}"
            )
            failed.append({"channel_id": str(cid), "name": channel["name"] if channel else None,
                           "error": reason})
            results.append({"channel_id": str(cid), "name": channel["name"] if channel else None,
                            "ok": False, "error": reason, "jump_url": ""})
        else:
            url = f"https://discord.com/channels/777001/{cid}/1"
            sent.append({"channel_id": str(cid), "name": channel["name"], "jump_url": url})
            results.append({"channel_id": str(cid), "name": channel["name"], "ok": True,
                            "error": "", "jump_url": url})
    row["status"] = "sent" if not row["repeat_minutes"] else "pending"
    row["last_run_at"] = now
    row["fire_count"] += 1
    row["results"] = results
    row["last_error"] = "" if not failed else f"{len(failed)} channel(s) refused it: {failed[0]['error']}"
    if row["repeat_minutes"]:
        row["run_at"] = row["run_at"] + row["repeat_minutes"] * 60
        while row["run_at"] <= now:
            row["run_at"] += row["repeat_minutes"] * 60
    return jsonify({
        "success": bool(sent),
        "error": None if sent else (failed[0]["error"] if failed else "Nothing was sent."),
        "results": results,
        "sent": sent,
        "failed": failed,
        "ignored_mentions": row["payload"].get("ignored_mentions", []),
        "server_now": now,
    })


@app.route("/__last-send")
def last_send():
    """The ids the browser actually posted, verbatim."""
    return jsonify(LAST_SEND)


@app.route("/__last-schedule")
def last_schedule():
    """The scheduling body the browser actually posted, verbatim."""
    return jsonify(LAST_SCHEDULE)


@app.route("/__queue")
def queue_state():
    return jsonify(SCHEDULE)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8099")))
