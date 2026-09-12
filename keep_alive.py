"""
Real-Time Web Admin Dashboard & Keep-Alive Server for Meow Discord Bot.
Enables administrators to toggle modules and features ON/OFF with 0-second (1:1 instant) latency.
"""
import os
import time
import secrets
from threading import Thread
import socket
from flask import Flask, render_template, request, redirect, session, jsonify
from utils.db import get_guild_modules_sync, set_guild_module_sync

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "meow_dashboard_secret_key_2026_secure")

BOT_INSTANCE = None
ADMIN_PASSKEY = os.environ.get("ADMIN_PASSKEY", "meow_admin_2026")
MAGIC_TOKENS = {}

AVAILABLE_MODULES = [
    {
        "id": "music",
        "name": "Music & Radio",
        "icon": "🎵",
        "category": "Entertainment",
        "desc": "High-res YouTube, SoundCloud, Spotify streaming, 24/7 Lo-Fi radio, queue, volume & lyrics.",
        "commands": ["/play", "/radio", "/nowplaying", "/pause", "/resume", "/skip", "/stop", "/queue", "/volume", "/songinfo", "/lyrics"]
    },
    {
        "id": "leveling",
        "name": "Leveling & XP",
        "icon": "🏆",
        "category": "Progression",
        "desc": "Message length & voice XP rewards, level-up cards routed to #bot-cmd, rank titles & /rank.",
        "commands": ["/rank", "/leaderboard", "/levelsettings", "/setlevel", "/givexp", "/addlevelrole", "/removelevelrole", "/levelroles", "/resetlevels"]
    },
    {
        "id": "economy",
        "name": "Economy & Casino",
        "icon": "💰",
        "category": "Economy",
        "desc": "Daily streak rewards, gambling, slots, blackjack, coinflip, shop, and inventory items.",
        "commands": ["/balance", "/daily", "/weekly", "/work", "/rob", "/pay", "/deposit", "/withdraw", "/shop", "/buy", "/inventory", "/richlist", "/eco-admin"]
    },
    {
        "id": "moderation",
        "name": "Moderation & Logs",
        "icon": "🛡️",
        "category": "Management",
        "desc": "Administrative bans, kicks, timeouts, warnings, message purges, and automated audit logs in #logs.",
        "commands": ["/ban", "/kick", "/mute", "/unmute", "/warn", "/warnings", "/clearwarnings", "/purge", "/modlog", "/lockdown", "/unlock", "/slowmode", "/addrole", "/removerole", "/announce"]
    },
    {
        "id": "automod",
        "name": "Automod & Shield",
        "icon": "🤖",
        "category": "Security",
        "desc": "Real-time anti-spam flood protection, Discord invite filter, unauthorized link blocking.",
        "commands": ["/automod invites", "/automod antispam"]
    },
    {
        "id": "giveaway",
        "name": "Giveaway System",
        "icon": "🎁",
        "category": "Community",
        "desc": "Interactive button entries, live countdown timers, cryptographically secure winner selection.",
        "commands": ["/giveaway start", "/giveaway reroll", "/giveaway end"]
    },
    {
        "id": "colors",
        "name": "Color Studio & Roles",
        "icon": "🎨",
        "category": "Customization",
        "desc": "Interactive aesthetic dropdown palette with 11 curated tones & custom #HEX role coloring.",
        "commands": ["/color palette", "/color custom", "/color remove"]
    },
    {
        "id": "tempvoice",
        "name": "TempVoice (Join-to-Create)",
        "icon": "🔊",
        "category": "Voice",
        "desc": "Dynamic private voice room creation on join, room locking, user limits, and auto-deletion.",
        "commands": ["/tempvoice setup", "/tempvoice lock", "/tempvoice limit"]
    },
    {
        "id": "starboard",
        "name": "Starboard",
        "icon": "⭐",
        "category": "Community",
        "desc": "Automated star reaction tracking, community showcase cards, attachment previews.",
        "commands": ["/starboard set", "/starboard limit", "/starboard status"]
    },
    {
        "id": "tickets",
        "name": "Support Tickets",
        "icon": "🎟️",
        "category": "Support",
        "desc": "Interactive dropdown ticket panels (Game Cracking, Trading, General), private staff channels.",
        "commands": ["/ticket setup"]
    },
    {
        "id": "social",
        "name": "Social Roleplay",
        "icon": "🐾",
        "category": "Community",
        "desc": "Expressive action roleplay commands (hug, pat, cuddle, kiss, slap), /uwu text styler.",
        "commands": ["/action", "/uwu", "/marry", "/divorce", "/ship", "/hunt", "/zoo", "/sellanimals"]
    },
    {
        "id": "fun",
        "name": "Fun & Memes",
        "icon": "🎭",
        "category": "Entertainment",
        "desc": "Real-time viral memes scraped from Reddit top feeds, 8ball, inspirational quotes.",
        "commands": ["/meme", "/8ball", "/meow", "/meowsound", "/tan", "/kms", "/quote", "/addquote", "/customquote", "/customquotes", "/delquote"]
    },
    {
        "id": "games",
        "name": "Mini-Games Hub",
        "icon": "🎮",
        "category": "Games",
        "desc": "Interactive Discord game sessions: TicTacToe, Rock-Paper-Scissors, Trivia, Mines, Crash, HighLow.",
        "commands": ["/gamble", "/slots", "/blackjack", "/roulette", "/coinflip", "/dice", "/mines", "/crash", "/highlow", "/tictactoe", "/rps", "/trivia"]
    },
    {
        "id": "welcome",
        "name": "Welcome System",
        "icon": "👋",
        "category": "Community",
        "desc": "New member join greetings and role assignment (kept OFF if using dedicated welcomer bot).",
        "commands": ["/welcome set", "/welcome goodbye", "/welcome disable"]
    }
]

def generate_magic_token() -> str:
    token = secrets.token_urlsafe(32)
    MAGIC_TOKENS[token] = time.time() + 86400
    return token

def is_authenticated():
    return session.get("authenticated") is True

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_ready": bool(BOT_INSTANCE and getattr(BOT_INSTANCE, "_has_started", False)),
        "guilds": len(BOT_INSTANCE.guilds) if BOT_INSTANCE else 0,
        "latency_ms": round(BOT_INSTANCE.latency * 1000) if BOT_INSTANCE and BOT_INSTANCE.latency else None,
        "timestamp": time.time()
    })

@app.route("/")
def home():
    user_agent = request.headers.get("User-Agent", "").lower()
    if "uptimerobot" in user_agent or "bot" in user_agent or "curl" in user_agent:
        return "Meow Bot is alive and operational!"
    if is_authenticated():
        return redirect("/dashboard")
    return redirect("/login")

@app.route("/login", methods=["GET", "POST"])
def login():
    token = request.args.get("token")
    if token:
        expires_at = MAGIC_TOKENS.get(token)
        if expires_at and time.time() < expires_at:
            session["authenticated"] = True
            session.permanent = True
            MAGIC_TOKENS.pop(token, None)
            return redirect("/dashboard")
        return render_template("login.html", error="Magic link has expired or is invalid.")

    if request.method == "POST":
        entered_passkey = request.form.get("passkey")
        if entered_passkey == ADMIN_PASSKEY:
            session["authenticated"] = True
            session.permanent = True
            return redirect("/dashboard")
        return render_template("login.html", error="Invalid admin passkey. Please try again.")

    if is_authenticated():
        return redirect("/dashboard")
    return render_template("login.html", error=None)

@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    return redirect("/login")

@app.route("/dashboard")
def dashboard():
    if not is_authenticated():
        return redirect("/login")

    guild_id = 1542278896148422676
    guild_name = "Hmm"
    member_count = 1
    bot_ping = 28

    if BOT_INSTANCE:
        guild = BOT_INSTANCE.get_guild(guild_id)
        if guild:
            guild_name = guild.name
            member_count = guild.member_count or len(guild.members)
        bot_ping = round(BOT_INSTANCE.latency * 1000) if BOT_INSTANCE.latency else 28

    db_modules = get_guild_modules_sync(guild_id)
    modules_data = []
    active_count = 0

    for m in AVAILABLE_MODULES:
        m_id = m["id"]
        default_state = False if m_id == "welcome" else True
        if BOT_INSTANCE and hasattr(BOT_INSTANCE, "module_states"):
            is_enabled = BOT_INSTANCE.module_states.get(guild_id, {}).get(m_id, db_modules.get(m_id, default_state))
        else:
            is_enabled = db_modules.get(m_id, default_state)

        if is_enabled:
            active_count += 1

        modules_data.append({
            "id": m_id,
            "name": m["name"],
            "icon": m["icon"],
            "category": m["category"],
            "desc": m["desc"],
            "commands": m["commands"],
            "enabled": is_enabled
        })

    return render_template(
        "dashboard.html",
        guild_id=guild_id,
        guild_name=guild_name,
        member_count=member_count,
        bot_ping=bot_ping,
        modules=modules_data,
        active_count=active_count,
        total_count=len(AVAILABLE_MODULES)
    )

@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    if not is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json() or {}
    guild_id = data.get("guild_id", 1542278896148422676)
    module_id = data.get("module")
    enabled = bool(data.get("enabled"))

    if not module_id:
        return jsonify({"success": False, "error": "Missing module ID"}), 400

    if BOT_INSTANCE and hasattr(BOT_INSTANCE, "set_module_state"):
        BOT_INSTANCE.set_module_state(guild_id, module_id, enabled)

    set_guild_module_sync(guild_id, module_id, enabled)

    return jsonify({
        "success": True,
        "guild_id": guild_id,
        "module": module_id,
        "enabled": enabled,
        "sync_latency_ms": 0
    })

@app.route("/api/toggle-all", methods=["POST"])
def api_toggle_all():
    if not is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json() or {}
    guild_id = data.get("guild_id", 1542278896148422676)
    enabled = bool(data.get("enabled"))

    for m in AVAILABLE_MODULES:
        m_id = m["id"]
        if BOT_INSTANCE and hasattr(BOT_INSTANCE, "set_module_state"):
            BOT_INSTANCE.set_module_state(guild_id, m_id, enabled)
        set_guild_module_sync(guild_id, m_id, enabled)

    return jsonify({
        "success": True,
        "guild_id": guild_id,
        "enabled": enabled,
        "sync_latency_ms": 0
    })

def find_free_port(start=8080):
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return start

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive(bot=None):
    global BOT_INSTANCE
    BOT_INSTANCE = bot
    t = Thread(target=run)
    t.daemon = True
    t.start()

