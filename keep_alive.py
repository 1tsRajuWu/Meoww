"""
Real-Time Web Admin Dashboard & Keep-Alive Server for Meow Discord Bot.
Enables administrators to toggle modules and features ON/OFF with 0-second (1:1 instant) latency.
"""
import math
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


def dashboard_base_url() -> str:
    """Where *this* process is reachable, for the one-time sign-in link.

    The token lives in this process's memory, so a link that names a different
    host is dead on arrival — and that is exactly how this project is run: the
    bot is deployed on Render *and* run from the development Mac, where
    ``DASHBOARD_BASE_URL`` is unset and every link pointed at the Render URL,
    whose process had never heard of the token. "That link has expired" was the
    symptom, and nothing had expired.
    """
    explicit = (os.environ.get("DASHBOARD_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    # Render sets this to the service's public URL automatically.
    hosted = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if hosted:
        return hosted
    if os.environ.get("RENDER") or os.environ.get("RAILWAY_ENVIRONMENT"):
        return "https://meow-discord-bot.onrender.com"
    # Anywhere else the dashboard is a local one, and the admin is on the same
    # machine as the process that minted the token.
    return f"http://localhost:{os.environ.get('PORT', '8080')}"


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


def _bot_ping_ms():
    """Gateway latency in whole milliseconds, or ``None`` when there is none.

    ``discord.Client.latency`` is ``nan`` until the first heartbeat is
    acknowledged and ``inf`` whenever the socket is rebuilding — and ``nan`` is
    *truthy*, so the old ``if latency:`` guard let ``round(nan * 1000)`` through
    and opening the dashboard during a reconnect answered **500**, which is the
    page an administrator reaches for when something looks wrong.
    """
    if BOT_INSTANCE is None:
        return None
    try:
        latency = float(BOT_INSTANCE.latency)
    except (AttributeError, TypeError, ValueError):
        return None
    if not math.isfinite(latency) or latency < 0:
        return None
    return round(latency * 1000)


def _json_number(value):
    """A JSON-safe number: ``NaN``/``inf`` are not valid JSON at all.

    ``json.dumps`` happily writes a bare ``NaN``, which every browser's
    ``JSON.parse`` rejects — so a voice client that has not answered a heartbeat
    yet made ``/api/voice-debug`` unreadable rather than reporting "unknown".
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 3)


def _resolve_guild_id(requested=None):
    """The one guild this request is allowed to act on, or ``None``.

    A session minted from a server's one-time link is **scoped**: it may only
    ever touch the server that link was created in, and only while the bot is
    still there. Everything else — the passkey session — may pick any server the
    bot is in. The scope test reads the scope alone, deliberately: it used to
    also require a truthy ``guild_id``, and ``dashboard()`` wrote the ``0``
    fallback of a failed lookup back into the session, so one page load demoted a
    scoped session to an unscoped one and it could then toggle *any* server.
    """
    known = {g["id"] for g in _known_guilds()}

    if session.get("auth_scope") == "guild":
        gid = _int_or_none(session.get("guild_id"))
        if gid is None:
            print("[DASHBOARD] Guild-scoped session with no server; refusing.", flush=True)
            return None
        if known and gid not in known:
            # The link was minted for a server the bot is no longer in. Falling
            # through to "first known guild" would quietly move the session onto
            # somebody else's server, showing and toggling modules that were
            # never granted. A scoped session gets its own guild or nothing.
            print(
                f"[DASHBOARD] Refusing a guild-scoped session for {gid}: the bot "
                "is not in that server.",
                flush=True,
            )
            return None
        return gid

    session_guild = session.get("guild_id")
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
        # Deterministic, because a set's iteration order is not a choice anyone
        # can predict — the same server should come up on every request.
        return min(known)
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
    # Original scripts only, except for the one third-party the living market page
    # genuinely needs: its chart library, pinned to an exact version and hosted by
    # the two CDNs the page tries in turn. Without them named here our own header
    # blocked both copies, so the chart could never draw on any deployment and the
    # card blamed the library. styles/fonts are the dashboard's Google Fonts.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
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
                "latency": _json_number(getattr(vc, "latency", None)),
                "average_latency": _json_number(getattr(vc, "average_latency", None)),
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


def _run_on_bot_loop(coro, timeout: float = 15.0):
    """Run a coroutine on the bot's own event loop from a Flask thread.

    Anything the dashboard does to Discord has to run there: the gateway objects
    belong to that loop, and a second loop would hold a second, disagreeing view
    of the cache. A request must never hang on it, hence the timeout.
    """
    import asyncio

    loop = getattr(BOT_INSTANCE, "loop", None)
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)
    return asyncio.run(coro)


def _run_market(coro, timeout: float = 15.0):
    """Run a market coroutine from a Flask thread.

    On the bot's own loop when there is one, so the snapshot cache and the
    in-flight refresh guard are the same ones the commands use — a second event
    loop would happily fetch the board twice at once.
    """
    return _run_on_bot_loop(coro, timeout=timeout)


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
    if guild_id:
        session["guild_id"] = guild_id
    else:
        # Never persist the failure: writing ``0`` here is what let a scoped
        # session look unscoped on its next request.
        guild_id = 0

    guild_name = "Unknown server"
    member_count = 0
    bot_ping = _bot_ping_ms()
    bot_ready = bool(BOT_INSTANCE and getattr(BOT_INSTANCE, "_has_started", False))

    if BOT_INSTANCE:
        guild = BOT_INSTANCE.get_guild(guild_id)
        if guild:
            guild_name = guild.name
            member_count = guild.member_count or len(guild.members)

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


# ---------------------------------------------------------------------------
# Message composer
# ---------------------------------------------------------------------------
# This is the most powerful thing the dashboard can do: it speaks as the bot, in
# any channel, tagging anyone. So nothing it accepts is trusted from the browser.
# The session only ever proves *an administrator signed in* — it says nothing
# about which guild or channel the bot can actually reach, and one click must not
# be able to turn into a mass ping of a server this panel was not pointed at.
MESSAGE_MAX_CHARS = 2000  # Discord's message limit, measured on the final text
MESSAGE_TITLE_MAX = 256  # embed title limit
MESSAGE_MAX_CHANNELS = 5  # one send may fan out to a handful of channels
MESSAGE_MAX_MENTIONS = 25
SEND_WINDOW_SECONDS = 60
SEND_MAX_PER_WINDOW = 30

_send_lock = Lock()
_send_attempts = defaultdict(deque)  # ip -> timestamps


def _int_or_none(raw):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _bot_ready() -> bool:
    return bool(BOT_INSTANCE and getattr(BOT_INSTANCE, "_has_started", False))


def _send_allowed(ip: str) -> bool:
    """Cap how fast one client can make the bot talk.

    A stuck retry loop (or a curious administrator holding Enter) could otherwise
    post hundreds of messages before anyone noticed.
    """
    now = time.time()
    with _send_lock:
        if len(_send_attempts) > 5000:
            cutoff = now - SEND_WINDOW_SECONDS
            for stale in [k for k, v in _send_attempts.items() if not v or v[-1] < cutoff]:
                _send_attempts.pop(stale, None)
        bucket = _send_attempts[ip]
        while bucket and now - bucket[0] > SEND_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= SEND_MAX_PER_WINDOW:
            return False
        bucket.append(now)
        return True


def _composer_guild(guild_id_raw):
    """``(guild_id, guild)`` for a request, or ``(None, None)``.

    ``_resolve_guild_id`` is the same guard the module toggles use: a session
    pinned to one server cannot be talked into touching another, and the id must
    be one the bot is actually in.
    """
    guild_id = _resolve_guild_id(guild_id_raw)
    if guild_id is None or not _bot_ready():
        return None, None
    guild = BOT_INSTANCE.get_guild(guild_id)
    if guild is None:
        return None, None
    return guild_id, guild


@app.route("/api/channels")
def api_channels():
    """Every channel in this guild a message could land in, for the picker."""
    denied = _require_auth_api()
    if denied:
        return denied

    guild_id, guild = _composer_guild(request.args.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server available — is the bot online?"}), 400

    me = guild.me
    if me is None and BOT_INSTANCE and BOT_INSTANCE.user:
        me = guild.get_member(BOT_INSTANCE.user.id)

    channels = []
    # ``Guild.text_channels`` is every text channel in the guild, and an
    # announcement channel *is* one: discord.py models it as a TextChannel whose
    # type is news (``channel.is_news()``) rather than as a class of its own, and
    # there is no ``Guild.news_channels`` to read. Reading one raised
    # AttributeError, which is what turned this endpoint into a 500 and left the
    # picker empty. Forum and voice channels are still not offered — a message
    # cannot land in a forum, and a voice channel is reached by joining it.
    for channel in guild.text_channels:
        can_send = True
        if me is not None:
            perms = channel.permissions_for(me)
            if not perms.view_channel:
                continue
            can_send = bool(perms.send_messages)
        channels.append({
            # A Discord snowflake is larger than a JavaScript number can hold:
            # 1548437081120833657 is read back by JSON.parse as 1548437081120833536,
            # and an id the picker sends back that way matches no channel, so every
            # send answered "That channel no longer exists". Ids cross to the
            # browser as strings and come home through ``_int_or_none``.
            "id": str(channel.id),
            "name": channel.name,
            "category": channel.category.name if channel.category else "",
            "is_news": bool(getattr(channel, "is_news", lambda: False)()),
            "can_send": can_send,
            "slowmode": int(getattr(channel, "slowmode_delay", 0) or 0),
        })

    channels.sort(key=lambda c: (c["category"].lower(), c["name"].lower()))
    return jsonify({"success": True, "guild_id": guild_id, "channels": channels})


@app.route("/api/mentionables")
def api_mentionables():
    """Members and roles that can be tagged, filtered by ``?q=``.

    Members are only searched as the box is typed, so the panel never ships a
    whole member list to the browser.
    """
    denied = _require_auth_api()
    if denied:
        return denied

    guild_id, guild = _composer_guild(request.args.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server available — is the bot online?"}), 400

    query = str(request.args.get("q") or "").strip().lower()
    limit = 25

    roles = [
        # Strung for the same reason the channel ids are: a tag sent back rounded
        # would resolve to nobody and be dropped as "not in this server".
        {"id": str(role.id), "label": role.name, "kind": "role"}
        for role in guild.roles
        # @everyone is not a role you tag like a role; it is offered on its own.
        if not role.is_default() and (not query or query in role.name.lower())
    ]
    roles.sort(key=lambda r: (not r["label"].lower().startswith(query), r["label"].lower()))

    users = []
    if query:
        for member in guild.members:
            name = member.display_name or member.name
            if query not in name.lower() and query not in member.name.lower():
                continue
            users.append({
                "id": str(member.id),
                "label": name,
                "sub": f"@{member.name}",
                "kind": "user",
            })
            if len(users) >= limit * 4:
                break
        users.sort(key=lambda u: (not u["label"].lower().startswith(query), u["label"].lower()))

    return jsonify({
        "success": True,
        "guild_id": guild_id,
        "query": query,
        "users": users[:limit],
        "roles": roles[:limit],
        # Matched the way a role or a member is — what was typed is looked for
        # *in* the label — so @everyone and @here stay on offer while they are
        # being typed. The old test asked whether the query already contained the
        # whole keyword, so "ev" offered nothing and the option appeared to be
        # missing until the word was finished.
        "special": [
            {"id": keyword, "label": keyword, "sub": subtitle, "kind": "special"}
            for keyword, subtitle in (
                ("@everyone", "everyone in the server"),
                ("@here", "everyone online right now"),
            )
            if not query or query in keyword
        ],
    })


async def _deliver_dashboard_message(guild_id: int, channel_ids, payload: dict) -> list[dict]:
    """Send the composed message, one channel at a time, on the bot's loop.

    One result per channel, so a partial failure is reported as a partial failure
    rather than as a success with a missing message.
    """
    import discord

    guild = BOT_INSTANCE.get_guild(guild_id) if BOT_INSTANCE else None
    if guild is None:
        return [
            {"channel_id": cid, "ok": False, "error": "I am not in that server"}
            for cid in channel_ids
        ]

    embed = None
    spec = payload.get("embed")
    if spec:
        embed = discord.Embed(description=spec.get("description") or None, color=0x5865F2)
        if spec.get("title"):
            embed.title = spec["title"]

    allowed = discord.AllowedMentions(
        users=[discord.Object(id=uid) for uid in payload.get("users", [])],
        roles=[discord.Object(id=rid) for rid in payload.get("roles", [])],
        everyone=bool(payload.get("everyone")),
        replied_user=False,
    )

    results = []
    for cid in channel_ids:
        channel = guild.get_channel(cid) or guild.get_thread(cid)
        if channel is None:
            results.append({"channel_id": cid, "ok": False, "error": "That channel no longer exists"})
            continue
        me = guild.me
        if me is not None and not channel.permissions_for(me).send_messages:
            results.append({
                "channel_id": cid,
                "name": getattr(channel, "name", str(cid)),
                "ok": False,
                "error": f"I cannot send messages in #{getattr(channel, 'name', cid)}",
            })
            continue
        try:
            message = await channel.send(
                content=payload.get("content"), embed=embed, allowed_mentions=allowed
            )
        except discord.Forbidden:
            results.append({
                "channel_id": cid,
                "name": getattr(channel, "name", str(cid)),
                "ok": False,
                "error": f"Discord refused the post in #{getattr(channel, 'name', cid)} (missing permission)",
            })
            continue
        except Exception as e:
            results.append({
                "channel_id": cid,
                "name": getattr(channel, "name", str(cid)),
                "ok": False,
                "error": str(e)[:200],
            })
            continue
        results.append({
            "channel_id": cid,
            "name": getattr(channel, "name", str(cid)),
            "ok": True,
            "message_id": message.id,
            "jump_url": getattr(message, "jump_url", None),
        })

    sent = [r for r in results if r.get("ok")]
    print(
        f"[DASHBOARD] Sent a composed message to {len(sent)}/{len(results)} channel(s) "
        f"in {guild.name} ({guild.id}).",
        flush=True,
    )
    return results


@app.route("/api/send-message", methods=["POST"])
def api_send_message():
    """Post a dashboard-written message as the bot."""
    denied = _require_auth_api()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    guild_id, guild = _composer_guild(data.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server available — is the bot online?"}), 400

    raw_channels = data.get("channel_ids") or []
    if isinstance(raw_channels, (str, int)):
        raw_channels = [raw_channels]
    if not isinstance(raw_channels, list):
        raw_channels = []
    channel_ids = []
    for raw in raw_channels:
        cid = _int_or_none(raw)
        if cid is not None and cid not in channel_ids:
            channel_ids.append(cid)
    if not channel_ids:
        return jsonify({"success": False, "error": "Pick at least one channel."}), 400
    if len(channel_ids) > MESSAGE_MAX_CHANNELS:
        return jsonify({
            "success": False,
            "error": f"At most {MESSAGE_MAX_CHANNELS} channels per send.",
        }), 400

    body = str(data.get("content") or "").strip()
    title = str(data.get("title") or "").strip()[:MESSAGE_TITLE_MAX]
    as_embed = bool(data.get("embed"))

    raw_mentions = data.get("mentions") or []
    if isinstance(raw_mentions, (str, int)):
        raw_mentions = [raw_mentions]
    if not isinstance(raw_mentions, list):
        raw_mentions = []
    if len(raw_mentions) > MESSAGE_MAX_MENTIONS:
        return jsonify({
            "success": False,
            "error": f"At most {MESSAGE_MAX_MENTIONS} tags per message.",
        }), 400

    if not body and title and not as_embed:
        # Outside an embed the title is not sent at all, so a message that was
        # nothing but a title would have reached Discord empty and come back as a
        # refusal. Name the missing half instead.
        return jsonify({
            "success": False,
            "error": 'A title is only posted with an embed — tick "Send as an embed", or write a message.',
        }), 400
    if not body and not title:
        return jsonify({"success": False, "error": "Write something to send."}), 400
    if len(body) > MESSAGE_MAX_CHARS:
        return jsonify({
            "success": False,
            "error": f"That message is {len(body)} characters; the limit is {MESSAGE_MAX_CHARS}.",
        }), 400

    # Every tag is resolved against *this* guild's live cache, so an id posted by
    # the browser can only ever become a ping if it really is a member or a role
    # here. Anything else is reported back rather than silently dropped.
    members, roles, keywords, ignored = [], [], [], []
    for raw in raw_mentions:
        token = str(raw).strip()
        # The picker offers @everyone and @here side by side, and they notify
        # different people, so the keyword that was asked for is the keyword that
        # is posted. Both used to raise one shared flag and go out as @everyone,
        # which answered a request for "everyone online right now" by pinging the
        # whole server.
        if token in ("@everyone", "everyone"):
            if "@everyone" not in keywords:
                keywords.append("@everyone")
            continue
        if token in ("@here", "here"):
            if "@here" not in keywords:
                keywords.append("@here")
            continue
        target = _int_or_none(token)
        if target is None:
            ignored.append(token)
            continue
        member = guild.get_member(target)
        if member is not None:
            members.append(member)
            continue
        role = guild.get_role(target)
        if role is not None:
            roles.append(role)
            continue
        ignored.append(token)

    # A ping only notifies from the message *content*: a mention inside an embed
    # silently does nothing, which is the one thing a tag-someone feature must not
    # get wrong. So the tags go in the content either way, and an embed carries
    # the text.
    # ``everyone`` is the flag ``allowed_mentions`` needs — it covers @here as
    # well — and it travels with the payload as before. The text, though, is the
    # exact keyword that was picked.
    everyone = bool(keywords)
    ping_parts = list(keywords)
    ping_parts += [m.mention for m in members] + [r.mention for r in roles]
    ping_text = " ".join(ping_parts)

    if as_embed:
        content = ping_text or None
        embed_spec = {"title": title, "description": body}
    else:
        content = f"{ping_text}\n{body}".strip() if ping_text else body
        embed_spec = None
        if len(content) > MESSAGE_MAX_CHARS:
            return jsonify({
                "success": False,
                "error": (
                    f"{len(content)} characters once the tags are added; the limit is "
                    f"{MESSAGE_MAX_CHARS}. Shorten it, or send it as an embed."
                ),
            }), 400

    payload = {
        "content": content,
        "embed": embed_spec,
        "users": [m.id for m in members],
        "roles": [r.id for r in roles],
        "everyone": everyone,
    }

    # Counted here rather than on arrival: a request that was going to be refused
    # anyway costs Discord nothing, and an administrator who mistyped a channel
    # deserves to be told that instead of being told to slow down.
    if not _send_allowed(_client_ip()):
        return jsonify({
            "success": False,
            "error": "That is a lot of messages at once. Wait a minute and try again.",
        }), 429

    try:
        results = _run_on_bot_loop(
            _deliver_dashboard_message(guild_id, channel_ids, payload), timeout=30.0
        )
    except Exception as e:
        print(f"[DASHBOARD] Composed message failed: {e}", flush=True)
        return jsonify({"success": False, "error": f"Discord did not accept it: {e}"}), 502

    sent = [r for r in results if r.get("ok")]
    failures = [r for r in results if not r.get("ok")]
    return jsonify({
        # A partial send is a success with a warning, never a silent one.
        "success": bool(sent),
        "error": None if sent else (failures[0].get("error") if failures else "Nothing was sent."),
        "results": results,
        "sent": [
            {"channel_id": r["channel_id"], "name": r.get("name"), "jump_url": r.get("jump_url")}
            for r in sent
        ],
        "failed": [
            {"channel_id": r["channel_id"], "name": r.get("name"), "error": r.get("error")}
            for r in failures
        ],
        "ignored_mentions": ignored,
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
    try:
        app.run(host="0.0.0.0", port=port)
    except OSError as e:
        # The dashboard thread dying inside itself is a stack trace nobody reads,
        # and the symptom is only "the dashboard does not load". An older instance
        # (or anything else) holding the port is the usual cause.
        print(
            f"[DASHBOARD] Could not listen on port {port} ({e}). Something else is "
            "serving it — an older instance of this bot, most likely. The dashboard "
            "is NOT available from this process.",
            flush=True,
        )


def keep_alive(bot=None):
    global BOT_INSTANCE
    BOT_INSTANCE = bot
    t = Thread(target=run)
    t.daemon = True
    t.start()
