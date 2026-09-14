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
import html as _html
from flask import Flask, render_template, request, redirect, session, jsonify, Response
from utils import features, modules

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
# The passkey must come from the environment. A hardcoded default is a published
# credential: anyone who read this repository could sign into any deployment that
# never set the variable, and /api/set-module accepts the same passkey with no
# session and no CSRF token. When it is unset the passkey is now empty, which
# disables passkey login and every passkey endpoint (they all reject an empty
# credential) while the /dashboard magic link keeps working.
ADMIN_PASSKEY = (os.environ.get("ADMIN_PASSKEY") or "").strip()
if not ADMIN_PASSKEY:
    print(
        "[DASHBOARD] ADMIN_PASSKEY is not set — passkey login and the passkey API "
        "endpoints are disabled. Admins can still sign in with the one-time link from "
        "/dashboard in Discord. Set ADMIN_PASSKEY to enable passkey login.",
        flush=True,
    )


def _passkey_ok() -> bool:
    """Validate ADMIN_PASSKEY from header or JSON body — never from the query string."""
    if not ADMIN_PASSKEY:
        return False
    provided = request.headers.get("X-Admin-Passkey") or ""
    if not provided and request.method in ("POST", "PUT", "PATCH"):
        data = request.get_json(silent=True) or {}
        provided = str(data.get("passkey") or "")
    if not provided:
        return False
    return secrets.compare_digest(provided, ADMIN_PASSKEY)


_SECRETISH = __import__("re").compile(
    r"(?i)(token|passkey|password|authorization|bearer|api[_-]?key|secret)\s*[:=]\s*\S+"
)

MAGIC_TTL_SECONDS = 15 * 60
MAGIC_TOKENS = {}
_magic_lock = Lock()
_login_lock = Lock()
_login_attempts = defaultdict(deque)  # ip -> timestamps
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5

# Card metadata and the allow-list are derived from the feature registry so the
# dashboard can never advertise a module the bot does not have (the old
# hand-written copy is what let a removed module keep showing as a green card).
AVAILABLE_MODULES = features.dashboard_modules()
ALLOWED_MODULE_IDS = set(features.TOGGLEABLE_IDS)


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
    connection. Each proxy *appends* the address it saw, so the rightmost hop
    is the safer choice (leftward entries can be client-spoofed). Prefer that
    hop over ``parts[0]``.
    """
    if _trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            # Safer hop: rightmost address appended by our trusted proxy.
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
        # One deque per source address, forever, is a slow memory leak when a
        # scanner walks through many addresses. Drop buckets that are outside the
        # window once the map grows past anything a real deployment needs.
        if len(_login_attempts) > 5000:
            now = time.time()
            for stale_ip in [
                key for key, stamps in _login_attempts.items()
                if not stamps or now - stamps[-1] > LOGIN_WINDOW_SECONDS
            ]:
                _login_attempts.pop(stale_ip, None)
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
    # Prefer dashboard session; allow header passkey for ops tooling.
    if is_authenticated():
        if not validate_csrf() and request.method != "GET":
            return jsonify({"error": "CSRF"}), 403
    elif not _passkey_ok():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        content = "".join(str(x) for x in list(LOG_RING))
        content = _SECRETISH.sub(r"\1=[redacted]", content)
        return Response(content, mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return Response(f"Error fetching logs: {e}", mimetype="text/plain; charset=utf-8")


@app.route("/api/set-module", methods=["POST"])
def api_set_module():
    """Passkey-authenticated module toggle — no CSRF required (internal use only)."""
    if not _passkey_ok():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}

    guild_id_raw = data.get("guild_id")
    module_id = data.get("module", "")
    enabled = bool(data.get("enabled", False))

    try:
        guild_id = int(guild_id_raw)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid guild_id"}), 400

    if module_id not in ALLOWED_MODULE_IDS:
        return jsonify({"success": False, "error": f"Unknown module. Valid: {sorted(ALLOWED_MODULE_IDS)}"}), 400

    if not modules.set_enabled(guild_id, module_id, enabled):
        return jsonify({"success": False, "error": "DB write failed"}), 500

    _schedule_cloud_backup()
    return jsonify({"success": True, "guild_id": guild_id, "module": module_id, "enabled": enabled})


@app.route("/api/voice-debug")
def api_voice_debug():
    if not _passkey_ok():
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
            # ``_player`` only exists on the music cog's VoiceClient subclass,
            # which is currently archived. Reading it directly raised
            # AttributeError on a plain client, so go through getattr — this
            # endpoint must report on any voice client without assuming music.
            player = getattr(vc, "_player", None)
            connection = getattr(vc, "_connection", None)
            vcs.append({
                "guild": vc.guild.name,
                "channel": getattr(vc.channel, "name", None),
                "connected": vc.is_connected(),
                "playing": vc.is_playing(),
                "paused": vc.is_paused(),
                "latency": vc.latency,
                "average_latency": vc.average_latency,
                "mode": getattr(vc, "mode", None),
                "player_alive": bool(player and player.is_alive()),
                "player_error": str(player._current_error) if player else None,
                "dave_version": getattr(connection, "dave_protocol_version", None),
                "can_encrypt": getattr(connection, "can_encrypt", None),
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


# ---------------------------------------------------------------------------
# Live market pages
# ---------------------------------------------------------------------------
# These serve **public market data and nothing else**: no member, no balance, no
# position, no session, which is why they need no login. The interactive chart
# lives here because this process already holds the price snapshot in memory.


def _run_market(coro, timeout: float = 15.0):
    """Run a market coroutine from a Flask thread.

    On the bot's own loop when there is one, so the snapshot cache and the
    in-flight refresh guard are the same ones the commands use — a second event
    loop would happily fetch the board twice at once. A request must never hang
    on a slow provider, hence the timeout.
    """
    import asyncio

    loop = getattr(BOT_INSTANCE, "loop", None)
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)
    return asyncio.run(coro)


@app.route("/market")
def market_index():
    """Bare ``/market`` shows the first instrument rather than a 404."""
    return redirect("/market/BTC")


@app.route("/market/<symbol>")
def market_page(symbol: str):
    """The live, interactive chart for one instrument."""
    from utils import market as market_module

    raw = str(symbol or "")
    instrument = market_module.BY_SYMBOL.get(raw.strip().upper())
    if instrument is None:
        safe = _html.escape(raw, quote=True)
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


@app.route("/api/market/quotes")
def api_market_quotes():
    """Every instrument's price, with its age and source, as JSON."""
    import time as _time

    from utils import market as market_module

    snap = _run_market(market_module.snapshot())
    response = jsonify(market_module.quotes_payload(snap, _time.time()))
    # Short enough that the page is live, long enough that a wall of tabs is not
    # a wall of requests.
    response.headers["Cache-Control"] = "public, max-age=5"
    return response


@app.route("/api/market/candles")
def api_market_candles():
    """One instrument's history as JSON, for the interactive chart."""
    from utils import market as market_module

    symbol = request.args.get("symbol", "BTC")
    period = request.args.get("period", market_module.DEFAULT_PERIOD)
    if period not in market_module.PERIODS:
        period = market_module.DEFAULT_PERIOD
    instrument = market_module.BY_SYMBOL.get(str(symbol).strip().upper())
    if instrument is None:
        return jsonify({"error": f"{symbol} is not on the market"}), 404
    series = _run_market(
        market_module.cached_candles(instrument.symbol, period), timeout=20.0
    )
    payload = market_module.series_payload(instrument.symbol, series, period)
    response = jsonify(payload)
    response.headers["Cache-Control"] = "public, max-age=30"
    return response


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
        # The same cache the bot reads — not a second, independently-loaded copy.
        states = modules.get_states(guild_id) if guild_id else modules.defaults()
    except Exception as e:
        print(f"[DASHBOARD] ⚠️ Could not read module states: {e}", flush=True)
        states = modules.defaults()
    modules_data = []
    active_count = 0

    for m in AVAILABLE_MODULES:
        m_id = m["id"]
        is_enabled = bool(states.get(m_id, features.default_enabled(m_id)))

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
    if not modules.set_enabled(guild_id, module_id, enabled):
        return jsonify({"success": False, "error": "Could not save that change. Please try again."}), 500

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

    failed = modules.set_many(guild_id, {m["id"]: enabled for m in AVAILABLE_MODULES})

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
