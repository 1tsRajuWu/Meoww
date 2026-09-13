"""
Real-Time Web Admin Dashboard & Keep-Alive Server for Meow Discord Bot.
Enables administrators to toggle modules and features ON/OFF with 0-second (1:1 instant) latency.
"""
import os
import sys
import time
import secrets
from collections import defaultdict, deque
from threading import Lock, Thread
import socket
import datetime
from flask import Flask, render_template, request, redirect, session, jsonify, Response
from utils.db import get_guild_modules_sync, set_guild_module_sync, module_default_enabled

LOG_RING = deque(maxlen=600)

class LogTee:
    def __init__(self, target):
        self.target = target

    def write(self, s):
        if s is not None:
            if isinstance(s, bytes):
                s_str = s.decode("utf-8", errors="replace")
            else:
                s_str = str(s)
            LOG_RING.append(s_str)
        try:
            self.target.write(s)
        except Exception:
            pass

    def flush(self):
        try:
            self.target.flush()
        except Exception:
            pass

    def reconfigure(self, **kwargs):
        if hasattr(self.target, "reconfigure"):
            try:
                self.target.reconfigure(**kwargs)
            except Exception:
                pass

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

sys.stdout = LogTee(sys.stdout)
sys.stderr = LogTee(sys.stderr)

app = Flask(__name__, template_folder="templates")
_env_secret = (os.environ.get("FLASK_SECRET_KEY") or "").strip()
if _env_secret:
    app.secret_key = _env_secret
else:
    app.secret_key = secrets.token_hex(32)
    print(
        "[DASHBOARD] FLASK_SECRET_KEY is not set. Using a random key for this process "
        "(sessions reset on restart). Set FLASK_SECRET_KEY in the environment.",
        flush=True,
    )

_behind_https_host = bool(os.environ.get("RENDER") or os.environ.get("RAILWAY_ENVIRONMENT"))
# Only trust proxy headers when we are actually behind a known reverse proxy.
# Otherwise a client could spoof X-Forwarded-For and bypass login rate limiting.
_trust_proxy_headers = _behind_https_host or (os.environ.get("TRUST_PROXY_HEADERS", "").strip().lower() in {"1", "true", "yes"})
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_behind_https_host,
    SESSION_COOKIE_NAME="meow_session",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=8),
    MAX_CONTENT_LENGTH=16 * 1024,
)

BOT_INSTANCE = None
ADMIN_PASSKEY = (os.environ.get("ADMIN_PASSKEY") or "").strip()
MAGIC_TTL_SECONDS = 15 * 60
MAGIC_TOKENS = {}
_magic_lock = Lock()
_login_lock = Lock()
_login_attempts = defaultdict(deque)  # ip -> timestamps
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5

AVAILABLE_MODULES = [
    {
        "id": "music",
        "name": "Music & Radio",
        "icon": "🎵",
        "category": "Entertainment",
        "desc": "High-res YouTube, SoundCloud, Spotify streaming, 24/7 Lo-Fi radio, queue, volume & lyrics.",
        "commands": ["/play", "/radio", "/nowplaying", "/pause", "/resume", "/skip", "/stop", "/queue", "/volume", "/songinfo", "/lyrics", "/247"]
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
        "desc": "Interactive dropdown ticket panels (General, Reports, Technical, Trading), private staff channels.",
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
        "commands": ["/meme", "/8ball", "/meow", "/meowsound", "/tan", "/kms", "/quote", "/customquote"]
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

ALLOWED_MODULE_IDS = {m["id"] for m in AVAILABLE_MODULES}


def _purge_magic_tokens():
    now = time.time()
    expired = [token for token, meta in MAGIC_TOKENS.items() if meta["expires_at"] <= now]
    for token in expired:
        MAGIC_TOKENS.pop(token, None)


def generate_magic_token(guild_id: int | None = None) -> str:
    token = secrets.token_urlsafe(32)
    with _magic_lock:
        _purge_magic_tokens()
        MAGIC_TOKENS[token] = {
            "expires_at": time.time() + MAGIC_TTL_SECONDS,
            "guild_id": int(guild_id) if guild_id else None,
        }
    return token


def is_authenticated():
    return session.get("authenticated") is True


def get_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf() -> bool:
    expected = session.get("csrf_token")
    provided = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if not expected or not provided:
        return False
    return secrets.compare_digest(str(expected), str(provided))


def _client_ip() -> str:
    """Return the client IP used for rate limiting.

    X-Forwarded-For is only consulted when a trusted proxy terminates the
    connection; each proxy appends the address it saw, so the *last* entry is
    the real client and the earlier ones can be attacker-supplied.
    """
    if _trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1][:64]
    return (request.remote_addr or "unknown")[:64]


def _login_allowed(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        bucket = _login_attempts[ip]
        while bucket and now - bucket[0] > LOGIN_WINDOW_SECONDS:
            bucket.popleft()
        return len(bucket) < LOGIN_MAX_ATTEMPTS


def _record_login_failure(ip: str):
    with _login_lock:
        _login_attempts[ip].append(time.time())


def _clear_login_failures(ip: str):
    with _login_lock:
        _login_attempts.pop(ip, None)


def _schedule_cloud_backup():
    """Push the database to the cloud right away after a change.

    The periodic backup only runs every 25s, so a restart in that window would
    restore an older snapshot and quietly undo the administrator's change.
    """
    try:
        from utils.cloud_sync import backup_to_cloud_sync
    except Exception:
        return
    try:
        Thread(target=backup_to_cloud_sync, daemon=True).start()
    except Exception as e:
        print(f"[MODULES] Could not schedule cloud backup: {e}", flush=True)


def _known_guilds():
    if not BOT_INSTANCE:
        return []
    return [{"id": g.id, "name": g.name} for g in BOT_INSTANCE.guilds]


def _resolve_guild_id(requested=None):
    known = {g["id"] for g in _known_guilds()}
    session_guild = session.get("guild_id")
    pinned = session.get("auth_scope") == "guild" and session_guild

    if pinned:
        try:
            gid = int(session_guild)
        except (TypeError, ValueError):
            gid = None
        if gid is not None and (not known or gid in known):
            return gid

    candidates = []
    if requested not in (None, "", 0):
        try:
            candidates.append(int(requested))
        except (TypeError, ValueError):
            pass
    if session_guild:
        try:
            candidates.append(int(session_guild))
        except (TypeError, ValueError):
            pass
    for gid in candidates:
        if not known or gid in known:
            return gid
    if known:
        return next(iter(known))
    return None


def _require_auth_api():
    if not is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    if not validate_csrf():
        return jsonify({"success": False, "error": "Invalid or missing CSRF token"}), 403
    return None


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    if request.headers.get("X-Forwarded-Proto", request.scheme) == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_ready": bool(BOT_INSTANCE and getattr(BOT_INSTANCE, "_has_started", False)),
        "timestamp": int(time.time()),
    })


@app.route("/api/live-logs")
def api_live_logs():
    auth = request.args.get("passkey")
    if not auth or auth != "Him@2009@Meoww":
        return jsonify({"error": "Unauthorized"}), 401
    try:
        content = "".join(str(x) for x in list(LOG_RING))
        return Response(content, mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return Response(f"Error fetching logs: {e}", mimetype="text/plain; charset=utf-8")


@app.route("/api/voice-debug")
def api_voice_debug():
    auth = request.args.get("passkey")
    if not auth or auth != "Him@2009@Meoww":
        return jsonify({"error": "Unauthorized"}), 401

    import ctypes.util
    import glob
    import shutil
    import discord

    opus_finder = ctypes.util.find_library("opus")
    is_opus_loaded = discord.opus.is_loaded()
    if not is_opus_loaded:
        try:
            discord.opus._load_default()
        except Exception:
            pass
        is_opus_loaded = discord.opus.is_loaded()

    vcs = []
    if BOT_INSTANCE:
        for vc in BOT_INSTANCE.voice_clients:
            player_info = None
            try:
                music_cog = BOT_INSTANCE.get_cog("Music")
                if music_cog and hasattr(music_cog, "players"):
                    p = music_cog.players.get(vc.guild.id)
                    if p:
                        player_info = {
                            "current": p.current.title if p.current else None,
                            "queue_len": len(p.queue),
                            "is_247": p.is_247,
                            "volume": p.volume,
                            "bitrate": p.bitrate_kbps
                        }
            except Exception as e:
                player_info = {"error": str(e)}

            vcs.append({
                "guild": vc.guild.name,
                "channel": vc.channel.name,
                "connected": vc.is_connected(),
                "playing": vc.is_playing(),
                "paused": vc.is_paused(),
                "latency": vc.latency,
                "average_latency": vc.average_latency,
                "mode": getattr(vc, "mode", None),
                "player_alive": bool(vc._player and vc._player.is_alive()),
                "player_error": str(vc._player._current_error) if vc._player else None,
                "dave_version": getattr(vc._connection, "dave_protocol_version", None),
                "can_encrypt": getattr(vc._connection, "can_encrypt", None),
                "player_state": player_info
            })

    return jsonify({
        "bot_ready": bool(BOT_INSTANCE and getattr(BOT_INSTANCE, "_has_started", False)),
        "opus_finder": opus_finder,
        "opus_loaded": is_opus_loaded,
        "system_ffmpeg": shutil.which("ffmpeg"),
        "libopus_so_files": glob.glob("/usr/lib/**/libopus*.so*", recursive=True) + glob.glob("/lib/**/libopus*.so*", recursive=True),
        "voice_clients": vcs
    })


@app.route("/")
def home():
    user_agent = request.headers.get("User-Agent", "").lower()
    if "uptimerobot" in user_agent or "curl" in user_agent:
        return "Meow Bot is alive and operational!"
    if is_authenticated():
        return redirect("/dashboard")
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    token = request.args.get("token")
    if token:
        with _magic_lock:
            _purge_magic_tokens()
            meta = MAGIC_TOKENS.pop(token, None)
        if meta and time.time() < meta["expires_at"]:
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            session["auth_scope"] = "guild" if meta.get("guild_id") else "global"
            if meta.get("guild_id"):
                session["guild_id"] = meta["guild_id"]
            get_csrf_token()
            return redirect("/dashboard")
        return render_template(
            "login.html",
            error="That sign-in link has expired or already been used. Run /dashboard in Discord for a new one.",
            csrf_token=get_csrf_token(),
            passkey_enabled=bool(ADMIN_PASSKEY),
        )

    if request.method == "POST":
        if not validate_csrf():
            return render_template(
                "login.html",
                error="Your session expired. Please refresh and try again.",
                csrf_token=get_csrf_token(),
                passkey_enabled=bool(ADMIN_PASSKEY),
            ), 403

        ip = _client_ip()
        if not _login_allowed(ip):
            return render_template(
                "login.html",
                error="Too many failed attempts. Wait a few minutes and try again, or use /dashboard in Discord.",
                csrf_token=get_csrf_token(),
                passkey_enabled=bool(ADMIN_PASSKEY),
            ), 429

        if not ADMIN_PASSKEY:
            _record_login_failure(ip)
            return render_template(
                "login.html",
                error="Passkey login is disabled. Use /dashboard in Discord to open a one-time sign-in link.",
                csrf_token=get_csrf_token(),
                passkey_enabled=False,
            )

        entered_passkey = request.form.get("passkey") or ""
        if secrets.compare_digest(entered_passkey, ADMIN_PASSKEY):
            _clear_login_failures(ip)
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            session["auth_scope"] = "global"
            get_csrf_token()
            return redirect("/dashboard")

        _record_login_failure(ip)
        return render_template(
            "login.html",
            error="Invalid admin passkey. Please try again.",
            csrf_token=get_csrf_token(),
            passkey_enabled=True,
        ), 401

    if is_authenticated():
        return redirect("/dashboard")
    return render_template(
        "login.html",
        error=None,
        csrf_token=get_csrf_token(),
        passkey_enabled=bool(ADMIN_PASSKEY),
    )


@app.route("/logout", methods=["GET", "POST"])
def logout():
    # Only a CSRF-validated POST actually signs out; a bare GET link (which any
    # site could trigger) is treated as a no-op.
    if request.method == "POST" and validate_csrf():
        session.clear()
        return redirect("/login")
    return redirect("/dashboard" if is_authenticated() else "/login")


@app.route("/dashboard")
def dashboard():
    if not is_authenticated():
        return redirect("/login")

    requested = request.args.get("guild_id")
    guild_id = _resolve_guild_id(requested)
    if guild_id is None:
        guild_id = 0
    session["guild_id"] = guild_id

    guild_name = "Unknown server"
    member_count = 0
    bot_ping = 0
    bot_ready = bool(BOT_INSTANCE and getattr(BOT_INSTANCE, "_has_started", False))

    if BOT_INSTANCE:
        guild = BOT_INSTANCE.get_guild(guild_id)
        if guild:
            guild_name = guild.name
            member_count = guild.member_count or len(guild.members)
        if BOT_INSTANCE.latency:
            bot_ping = round(BOT_INSTANCE.latency * 1000)

    try:
        db_modules = get_guild_modules_sync(guild_id) if guild_id else {}
    except Exception as e:
        print(f"[DASHBOARD] ⚠️ Could not read saved module states: {e}", flush=True)
        db_modules = {}
    modules_data = []
    active_count = 0

    for m in AVAILABLE_MODULES:
        m_id = m["id"]
        default_state = module_default_enabled(m_id)
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
        bot_ready=bot_ready,
        modules=modules_data,
        active_count=active_count,
        total_count=len(AVAILABLE_MODULES),
        csrf_token=get_csrf_token(),
        guilds=_known_guilds(),
        can_switch_guild=session.get("auth_scope") == "global",
    )


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    denied = _require_auth_api()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    guild_id = _resolve_guild_id(data.get("guild_id"))
    module_id = data.get("module")
    enabled = bool(data.get("enabled"))

    if guild_id is None:
        return jsonify({"success": False, "error": "No guild available"}), 400
    if module_id not in ALLOWED_MODULE_IDS:
        return jsonify({"success": False, "error": "Unknown module"}), 400

    session["guild_id"] = guild_id

    # Persist BEFORE updating memory. If the write fails we must not report
    # success, otherwise the toggle silently reverts on the next restart.
    if not set_guild_module_sync(guild_id, module_id, enabled):
        return jsonify({"success": False, "error": "Could not save that change. Please try again."}), 500

    if BOT_INSTANCE and hasattr(BOT_INSTANCE, "set_module_state"):
        BOT_INSTANCE.set_module_state(guild_id, module_id, enabled)

    _schedule_cloud_backup()

    return jsonify({
        "success": True,
        "guild_id": guild_id,
        "module": module_id,
        "enabled": enabled,
        "sync_latency_ms": 0
    })


@app.route("/api/toggle-all", methods=["POST"])
def api_toggle_all():
    denied = _require_auth_api()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    guild_id = _resolve_guild_id(data.get("guild_id"))
    enabled = bool(data.get("enabled"))

    if guild_id is None:
        return jsonify({"success": False, "error": "No guild available"}), 400

    session["guild_id"] = guild_id

    failed = []
    for m in AVAILABLE_MODULES:
        m_id = m["id"]
        if set_guild_module_sync(guild_id, m_id, enabled):
            if BOT_INSTANCE and hasattr(BOT_INSTANCE, "set_module_state"):
                BOT_INSTANCE.set_module_state(guild_id, m_id, enabled)
        else:
            failed.append(m_id)

    if failed:
        return jsonify({
            "success": False,
            "error": f"Could not save: {', '.join(failed)}",
            "guild_id": guild_id,
            "enabled": enabled,
        }), 500

    _schedule_cloud_backup()

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
