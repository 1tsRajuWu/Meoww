"""Serve the real dashboard template with stub APIs, to eyeball the composer.

Usage (run from the repo root): ``python scripts/preview_dashboard.py``
then open http://localhost:5000/ (or the printed port).

Verification harness only: it renders ``templates/dashboard.html`` exactly as the
dashboard route does, and answers the three composer endpoints with plausible
data so the page's own JavaScript runs end to end. Nothing here is imported by
the bot.
"""
import os
import sys

from flask import Flask, jsonify, render_template, request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))

# The real app's security headers, applied to every response — otherwise this
# harness would prove nothing about whether the pages survive our own CSP.
import keep_alive  # noqa: E402


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


@app.route("/dashboard")
def dashboard():
    return render_template(
        "dashboard.html",
        guild_id=777001,
        guild_name="Meow Server",
        member_count=1627,
        bot_ping=int(os.environ.get("PREVIEW_PING", "41")),
        bot_ready=True,
        modules=MODULES,
        active_count=2,
        total_count=3,
        csrf_token="preview-token",
        guilds=[{"id": 777001, "name": "Meow Server"}, {"id": 777002, "name": "Second Server"}],
        can_switch_guild=True,
        composer_max_channels=keep_alive.MESSAGE_MAX_CHANNELS,
    )


@app.route("/market/<symbol>")
def market_page(symbol):
    """The real market route, so the chart can be checked under the real CSP."""
    from utils import market as market_module

    instrument = market_module.BY_SYMBOL.get(str(symbol).strip().upper())
    if instrument is None:
        return f"<h1>404 {symbol}</h1>", 404
    return render_template(
        "market_live.html",
        symbol=instrument.symbol,
        name=instrument.name,
        kind=instrument.kind,
        periods=list(market_module.PERIODS.items()),
        default_period=market_module.DEFAULT_PERIOD,
    )


@app.route("/api/market/quotes")
def api_market_quotes():
    return jsonify({"success": True, "quotes": {}})


@app.route("/api/market/candles")
def api_market_candles():
    return jsonify({"success": True, "candles": []})


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
    return jsonify({
        "success": bool(sent),
        "results": [],
        "sent": sent,
        "failed": failed,
        "ignored_mentions": [],
    })


@app.route("/__last-send")
def last_send():
    """The ids the browser actually posted, verbatim."""
    return jsonify(LAST_SEND)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8099")))
