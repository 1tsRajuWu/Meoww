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
import time

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
        # The real scheduling limits, so the page cannot look right here while the
        # endpoint would refuse what it offers.
        schedule_repeat_choices=[
            {"minutes": minutes, "label": label}
            for minutes, label in keep_alive.scheduled.REPEAT_CHOICES
        ],
        schedule_max_pending=keep_alive.scheduled.MAX_PENDING_PER_GUILD,
        schedule_catchup_hours=keep_alive.scheduled.MISS_CATCHUP_SECONDS // 3600,
        server_now=int(time.time()),
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
        "status": "pending",
        "created_by_name": "RAJ",
        "last_run_at": now - 86400,
        "last_error": "1 channel(s) refused it: I cannot send messages in #mod-logs",
        "results": [
            {"channel_id": "1548437081120833657", "ok": True, "error": "",
             "jump_url": "https://discord.com/channels/777001/1548437081120833657/1"},
            {"channel_id": "1548437081120833661", "ok": False,
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
