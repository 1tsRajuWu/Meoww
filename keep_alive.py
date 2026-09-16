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
from utils import features, modules, scheduled

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


def generate_magic_token(guild_id: int | None = None, user_id: int | None = None) -> str:
    token = secrets.token_urlsafe(32)
    with _magic_lock:
        _purge_magic_tokens()
        MAGIC_TOKENS[token] = {
            "expires_at": time.time() + MAGIC_TTL_SECONDS,
            "guild_id": int(guild_id) if guild_id else None,
            # Who asked for the link. One dashboard can be open on three admins'
            # screens at once, and for a message that is posted hours later this is
            # the only record of who queued it.
            "user_id": int(user_id) if user_id else None,
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
    # Nothing here is worth a stale copy. The pages and their APIs are per-session
    # and change on every deploy, and a browser holding yesterday's HTML is
    # indistinguishable from a feature that never shipped — which is exactly what
    # a rewritten dashboard looks like when the cache answers instead of the
    # server, deploy after deploy, with no error anywhere to show for it. The two
    # public market endpoints say what they want to be cached; anything that has
    # already spoken is left alone.
    if "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
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
            if meta.get("user_id"):
                session["user_id"] = meta["user_id"]
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
        # The picker's cap comes from the same constant the endpoint enforces, so
        # the page cannot offer a sixth channel the send would then refuse.
        composer_max_channels=MESSAGE_MAX_CHANNELS,
        # The scheduling controls read their limits from the store itself, so the
        # page cannot offer a repeat or a queue depth the endpoint would refuse.
        schedule_repeat_choices=[
            {"minutes": minutes, "label": label}
            for minutes, label in scheduled.REPEAT_CHOICES
        ],
        schedule_max_pending=scheduled.MAX_PENDING_PER_GUILD,
        schedule_catchup_hours=scheduled.MISS_CATCHUP_SECONDS // 3600,
        server_now=int(time.time()),
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
#: How many members the tag picker lists when the box is empty. Bounded because
#: this payload is fetched while you type; a query still searches the live member
#: list, so a member past the cap is found by typing rather than unreachable.
MENTION_MEMBER_LIST_MAX = 500
SEND_WINDOW_SECONDS = 60
SEND_MAX_PER_WINDOW = 30
#: How often the delivery loop looks for a message that has come due. The panel
#: schedules to the minute, so a quarter-minute tick is never the reason a
#: message is late — the only thing it costs is a cheap indexed query.
SCHEDULER_TICK_SECONDS = 15

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


def _mention_entry(member) -> dict:
    """One row of the tag picker. Ids cross as strings (see ``/api/channels``)."""
    return {
        "id": str(member.id),
        "label": member.display_name or member.name,
        "sub": f"@{member.name}",
        "kind": "user",
    }


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
    # picker empty.
    #
    # Voice channels are offered too, and not out of tidiness: a voice channel has
    # a text chat of its own, ``discord.VoiceChannel`` is ``Messageable``, and
    # ``.send()`` posts into that chat — which is where the people in the call are
    # actually looking. Leaving them out made the one channel people sit in the
    # one channel a message could not reach. Forums are still left out, because a
    # forum takes threads rather than messages (``ForumChannel`` is not
    # Messageable), and so are categories, which take neither.
    listed = [*guild.text_channels, *guild.voice_channels]
    for channel in listed:
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
            # ``can_send`` is a permission answer, and for a voice channel that is
            # only half the question: Discord also requires the channel's text chat
            # to be switched on, and no field in the API reports whether it is. So
            # a voice channel whose chat is off can only be found out by being
            # refused, and ``_deliver_dashboard_message`` says exactly that when it
            # is — rather than sending an administrator hunting for a role
            # overwrite that is not the problem.
            "kind": _channel_kind(channel),
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
            users.append(_mention_entry(member))
            if len(users) >= limit * 4:
                break
        users.sort(key=lambda u: (not u["label"].lower().startswith(query), u["label"].lower()))
        users = users[:limit]
    else:
        # An empty box browses the server rather than showing nothing: the point
        # of the picker is to tag people, and a member you cannot find is a member
        # you cannot tag. Alphabetical, capped, and the cap is reported in
        # ``member_total`` instead of applied quietly — a list that drops people
        # without saying so is how "everyone" goes missing.
        users = [_mention_entry(member) for member in guild.members]
        users.sort(key=lambda u: u["label"].lower())
        users = users[:MENTION_MEMBER_LIST_MAX]

    return jsonify({
        "success": True,
        "guild_id": guild_id,
        "query": query,            "users": users,
            # What the server says it has, and what the bot can actually see: with
            # the Members intent off the cache is a fraction of the server, and a
            # picker that does not admit that is how "everyone" goes missing.
            "member_cache": len(guild.members),
            "member_total": getattr(guild, "member_count", None) or len(guild.members),
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


def _channel_kind(channel) -> str:
    """``"text"`` or ``"voice"`` — which kind of place this channel is.

    Read from ``channel.type``, the field discord.py itself discriminates on, so a
    voice channel is recognised however it was reached: the picker lists
    ``Guild.voice_channels``, but a send resolves its id with ``get_channel`` and
    only what the object itself carries survives.
    """
    import discord

    return "voice" if getattr(channel, "type", None) == discord.ChannelType.voice else "text"


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
            name = getattr(channel, "name", None) or str(cid)
            # A voice channel's text chat can be switched off, and no field in the
            # API says whether it is: the refusal is the first thing that knows.
            # "Missing permission" would point an administrator at role
            # overwrites that are not the problem.
            if _channel_kind(channel) == "voice":
                reason = (
                    f"#{name} is a voice channel whose text chat is turned off — "
                    f"switch its text chat on, or send to a text channel instead"
                )
            else:
                reason = f"Discord refused the post in #{name} (missing permission)"
            results.append({
                "channel_id": cid,
                "name": name,
                "ok": False,
                "error": reason,
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


def _build_message(guild, data):
    """The composer's form turned into the exact payload the bot will post.

    "Send now" and "send later" are the same message with a different delivery
    time, so both come through here. A second copy of these rules for scheduling
    would drift, and the copy that drifts is the one nobody notices until a
    scheduled tag pings the wrong people.

    Returns ``(payload, channel_ids, error)``. ``error`` is the sentence to show
    instead of sending, and it is the *server's* sentence every time — nothing the
    browser says about a message is taken on trust.
    """
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
        return None, [], "Pick at least one channel."
    if len(channel_ids) > MESSAGE_MAX_CHANNELS:
        return None, [], f"At most {MESSAGE_MAX_CHANNELS} channels per send."

    body = str(data.get("content") or "").strip()
    title = str(data.get("title") or "").strip()[:MESSAGE_TITLE_MAX]
    as_embed = bool(data.get("embed"))

    raw_mentions = data.get("mentions") or []
    if isinstance(raw_mentions, (str, int)):
        raw_mentions = [raw_mentions]
    if not isinstance(raw_mentions, list):
        raw_mentions = []
    if len(raw_mentions) > MESSAGE_MAX_MENTIONS:
        return None, [], f"At most {MESSAGE_MAX_MENTIONS} tags per message."

    if not body and title and not as_embed:
        # Outside an embed the title is not sent at all, so a message that was
        # nothing but a title would have reached Discord empty and come back as a
        # refusal. Name the missing half instead.
        return None, [], (
            'A title is only posted with an embed — tick "Send as an embed", '
            "or write a message."
        )
    if not body and not title:
        return None, [], "Write something to send."
    if len(body) > MESSAGE_MAX_CHARS:
        return None, [], (
            f"That message is {len(body)} characters; the limit is {MESSAGE_MAX_CHARS}."
        )

    # Every tag is resolved against *this* guild's live cache, so an id posted by
    # the browser can only ever become a ping if it really is a member or a role
    # here. Anything else is reported back rather than silently dropped.
    members, roles, keywords, ignored, unresolved = [], [], [], [], []
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
        if target not in unresolved:
            unresolved.append(target)

    # A member the cache has never seen is still a member. The cache only holds
    # people the bot has happened to run into unless the Members intent is on, and
    # asking Discord for the one id is the difference between tagging somebody and
    # telling the administrator they are "not in this server".
    for target in unresolved:
        try:
            fetched = _run_on_bot_loop(guild.fetch_member(target), timeout=10.0)
        except Exception:
            fetched = None
        if fetched is not None:
            members.append(fetched)
        else:
            ignored.append(str(target))

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
            return None, [], (
                f"{len(content)} characters once the tags are added; the limit is "
                f"{MESSAGE_MAX_CHARS}. Shorten it, or send it as an embed."
            )

    payload = {
        "content": content,
        "embed": embed_spec,
        "users": [m.id for m in members],
        "roles": [r.id for r in roles],
        "everyone": everyone,
        # Stored with the payload rather than only returned: a scheduled message
        # is delivered long after this request, and "this tag matched nobody" is
        # part of what has to be reported about it then.
        "ignored_mentions": ignored,
    }
    return payload, channel_ids, None


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

    payload, channel_ids, error = _build_message(guild, data)
    if error:
        return jsonify({"success": False, "error": error}), 400

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
        "ignored_mentions": payload.get("ignored_mentions", []),
    })


# ---------------------------------------------------------------------------
# Scheduled messages
# ---------------------------------------------------------------------------
# A schedule is a promise the panel makes *after* the browser is closed, so the
# rules that matter are about time and persistence rather than about the form:
# when a message fires is decided by this server's clock (never the browser's),
# and the queued message itself is stored, so nothing depends on the page that
# created it still being open — or on the process that queued it still running.


def _epoch_seconds(data):
    """The requested delivery time in epoch seconds, from millis or seconds.

    The browser sends milliseconds because that is what ``Date.getTime()``
    returns; seconds are accepted so a script can say the obvious thing. Anything
    that is not a finite positive number is refused rather than coerced: ``nan``
    is truthy and ``inf`` compares greater than every deadline, which is how one
    bad number becomes a message scheduled for never.
    """
    for key, scale in (("run_at_ms", 1000.0), ("run_at", 1.0)):
        raw = data.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        return int(value / scale)
    return None


def _utc_text(seconds) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(seconds)))


def _human_delay(seconds: int) -> str:
    """"3 days", "2 hours", "5 minutes" — for the log, not the page."""
    seconds = max(0, int(seconds))
    for unit, step in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= step:
            count = seconds // step
            return f"{count} {unit}{'s' if count != 1 else ''}"
    return f"{seconds} seconds"


def _schedule_timing(data, now: int):
    """``(run_at, repeat_minutes, error)`` for a scheduling request."""
    run_at = _epoch_seconds(data)
    if run_at is None:
        return None, 0, "Pick a date and time for this message."
    if run_at < now - scheduled.PAST_GRACE_SECONDS:
        # Refused with *this server's* clock in the message, because the browser's
        # clock is the thing that usually disagrees — "that is in the past" is
        # otherwise unexplainable when the page reads 09:00 and this process 09:05.
        return None, 0, (
            f"That time has already passed — this server's clock reads {_utc_text(now)}. "
            "The page shows the same moment in your own time zone."
        )
    if run_at > now + scheduled.MAX_HORIZON_SECONDS:
        return None, 0, "That is more than a year away — pick a nearer date."
    repeat = _int_or_none(data.get("repeat_minutes") or 0) or 0
    if repeat not in {value for value, _ in scheduled.REPEAT_CHOICES}:
        return None, 0, "Unknown repeat — choose once, daily or weekly."
    # A time that has already arrived is not an error: a browser clock behind this
    # one makes "now" look like the past, and "now" is a legitimate thing to ask
    # for. It is queued a few seconds out so the loop finds it on its next pass.
    return max(run_at, now + scheduled.MIN_LEAD_SECONDS), repeat, None


def _schedule_view(guild, msg: dict) -> dict:
    """A stored message, plus the channel names this server has *now*.

    Names are resolved at read time on purpose: a message scheduled three days ago
    names a channel that may since have been renamed or deleted, and the list is
    only honest if it shows what is true at the moment it is read.
    """
    channels = []
    for raw in msg.get("channel_ids", []):
        cid = _int_or_none(raw)
        channel = guild.get_channel(cid) if (guild is not None and cid is not None) else None
        channels.append({
            "id": str(raw),
            "name": getattr(channel, "name", None) if channel is not None else None,
            "kind": _channel_kind(channel) if channel is not None else "",
            "exists": channel is not None,
        })
    creator = _int_or_none(msg.get("created_by"))
    member = guild.get_member(creator) if (guild is not None and creator is not None) else None

    view = dict(msg)
    view["channels"] = channels
    # The name is resolved now rather than stored: a person who has since changed
    # their nickname should be named as they are, and one who has left simply has
    # no name to show.
    view["created_by_name"] = (member.display_name or member.name) if member is not None else ""
    view["results"] = [
        {
            "channel_id": str(item.get("channel_id")),
            "name": item.get("name"),
            "ok": bool(item.get("ok")),
            "error": item.get("error") or "",
            "jump_url": item.get("jump_url") or "",
        }
        for item in msg.get("results", [])
        if isinstance(item, dict)
    ]
    return view


def _scheduled_counts(guild_id: int) -> dict:
    return {
        "pending": scheduled.pending_count(guild_id),
        "max_pending": scheduled.MAX_PENDING_PER_GUILD,
        "catchup_hours": scheduled.MISS_CATCHUP_SECONDS // 3600,
    }


@app.route("/api/scheduled")
def api_scheduled_list():
    """What this server has queued, and what it last did."""
    denied = _require_auth_api()
    if denied:
        return denied

    guild_id, guild = _composer_guild(request.args.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server available — is the bot online?"}), 400

    data = scheduled.list_for_guild(guild_id)
    return jsonify({
        "success": True,
        "guild_id": guild_id,
        # The server's clock, so the page can say how far away each time is
        # without trusting the clock of the machine looking at it.
        "server_now": int(time.time()),
        "upcoming": [_schedule_view(guild, m) for m in data["upcoming"]],
        "recent": [_schedule_view(guild, m) for m in data["recent"]],
        **_scheduled_counts(guild_id),
    })


@app.route("/api/schedule-message", methods=["POST"])
def api_schedule_message():
    """Queue a composed message to be posted later, once or on a repeat."""
    denied = _require_auth_api()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    guild_id, guild = _composer_guild(data.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server available — is the bot online?"}), 400

    now = int(time.time())
    run_at, repeat, error = _schedule_timing(data, now)
    if error:
        return jsonify({"success": False, "error": error, "server_now": now}), 400

    # The same builder "send now" uses, so a queued message is validated exactly
    # as an immediate one is — including resolving every tag against this guild.
    payload, channel_ids, error = _build_message(guild, data)
    if error:
        return jsonify({"success": False, "error": error, "server_now": now}), 400

    # Channels are checked *now*, which "send now" does not need to do: there, a
    # bad channel fails visibly in the very next second. Here the failure would
    # arrive days later with nobody watching, and a channel that did not exist at
    # queue time is a stale page or a crafted request rather than a typo.
    missing = [
        cid
        for cid in channel_ids
        if (guild.get_channel(cid) or guild.get_thread(cid)) is None
    ]
    if missing:
        return jsonify({
            "success": False,
            "error": "That channel no longer exists — reload the page and pick one again.",
            "server_now": now,
        }), 400

    msg_id = scheduled.create(
        guild_id,
        channel_ids,
        payload,
        run_at,
        repeat_minutes=repeat,
        created_by=_int_or_none(session.get("user_id")),
        now=now,
    )
    if msg_id is None:
        return jsonify({
            "success": False,
            "error": (
                f"This server already has {scheduled.MAX_PENDING_PER_GUILD} scheduled "
                "messages — cancel one before adding another."
            ),
            "server_now": now,
            **_scheduled_counts(guild_id),
        }), 429

    # The periodic backup runs every 25s, and a restart inside that window would
    # restore a snapshot from before this message existed.
    _schedule_cloud_backup()

    row = scheduled.get(guild_id, msg_id)
    print(
        f"[SCHEDULE] Queued #{msg_id} for {_utc_text(run_at)} "
        f"({scheduled.repeat_label(repeat)}) in {guild.name}.",
        flush=True,
    )
    return jsonify({
        "success": True,
        "server_now": now,
        "message": _schedule_view(guild, row) if row else None,
        # Tags that matched nobody are resolved at queue time, so they are reported
        # here — otherwise the message simply arrives days later missing a ping.
        "ignored_mentions": payload.get("ignored_mentions", []),
        **_scheduled_counts(guild_id),
    })


@app.route("/api/scheduled/<int:msg_id>/cancel", methods=["POST"])
def api_cancel_scheduled(msg_id: int):
    """Stop a queued message. Only the server it belongs to can do this."""
    denied = _require_auth_api()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    guild_id = _resolve_guild_id(data.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No guild available"}), 400

    if scheduled.get(guild_id, msg_id) is None:
        return jsonify({"success": False, "error": "That is not a scheduled message in this server."}), 404

    if not scheduled.cancel(guild_id, msg_id):
        # Read the row again rather than guessing why the conditional update
        # matched nothing: it may have fired between the two statements.
        after = scheduled.get(guild_id, msg_id) or {}
        return jsonify({
            "success": False,
            "error": (
                f"That message is already {after.get('status', 'gone')} — it is not "
                "waiting to be cancelled any more."
            ),
            "server_now": int(time.time()),
        }), 409

    _schedule_cloud_backup()
    print(f"[SCHEDULE] Cancelled #{msg_id}. {scheduled.describe(scheduled.get(guild_id, msg_id) or {})}", flush=True)
    return jsonify({"success": True, "id": msg_id, "server_now": int(time.time())})


@app.route("/api/scheduled/<int:msg_id>/fire", methods=["POST"])
def api_fire_scheduled(msg_id: int):
    """Send a queued message immediately, through the normal send path."""
    denied = _require_auth_api()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    guild_id, guild = _composer_guild(data.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server available — is the bot online?"}), 400

    row = scheduled.get(guild_id, msg_id)
    if row is None:
        return jsonify({"success": False, "error": "That is not a scheduled message in this server."}), 404
    if row["status"] != "pending":
        return jsonify({
            "success": False,
            "error": f"That message is already {row['status']}.",
        }), 409

    # Counted *before* claiming: claiming a message and then refusing to send it
    # would leave it stuck mid-flight, and the sweep would later report it as a
    # message the bot died while sending — which would be a lie this code told.
    if not _send_allowed(_client_ip()):
        return jsonify({
            "success": False,
            "error": "That is a lot of messages at once. Wait a minute and try again.",
        }), 429
    if not scheduled.claim(msg_id):
        return jsonify({
            "success": False,
            "error": "The delivery loop picked that message up first — check the list.",
        }), 409

    now = int(time.time())
    try:
        outcome = _run_on_bot_loop(_deliver_scheduled(row, now), timeout=30.0)
    except Exception as e:
        scheduled.mark_failed(msg_id, now, f"Discord did not accept it: {e}")
        print(f"[SCHEDULE] Sending #{msg_id} now failed: {e}", flush=True)
        return jsonify({"success": False, "error": f"Discord did not accept it: {e}"}), 502

    sent = outcome["sent"]
    failed = outcome["failed"]
    return jsonify({
        "success": bool(sent),
        "error": None if sent else outcome["error"],
        "results": outcome["results"],
        "sent": [
            {"channel_id": str(r["channel_id"]), "name": r.get("name"), "jump_url": r.get("jump_url")}
            for r in sent
        ],
        "failed": [
            {"channel_id": str(r["channel_id"]), "name": r.get("name"), "error": r.get("error")}
            for r in failed
        ],
        "ignored_mentions": (row.get("payload") or {}).get("ignored_mentions", []),
        "server_now": now,
    })


async def _deliver_scheduled(msg: dict, now: int) -> dict:
    """Post one claimed message and write down what happened. Never raises.

    The message is already claimed by the caller, so this function's one job is to
    end that claim: every path through it records an outcome, or the row would sit
    in ``sending`` until the stale sweep mistook an ordinary failure for a crash.
    """
    payload = msg.get("payload") or {}
    channel_ids = [cid for cid in (_int_or_none(c) for c in msg.get("channel_ids", [])) if cid]
    guild = BOT_INSTANCE.get_guild(msg.get("guild_id")) if BOT_INSTANCE else None

    results = []
    error = ""
    try:
        if guild is None:
            raise LookupError("I am not in that server any more")
        results = await _deliver_dashboard_message(msg["guild_id"], channel_ids, payload)
    except Exception as e:
        error = f"Discord did not accept it: {e}"

    sent = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    if sent and failed:
        # A partial delivery is its own outcome: most of the announcement went
        # out, and the one place it did not is the thing worth reading later.
        error = f"{len(failed)} channel(s) refused it: {failed[0].get('error')}"
        scheduled.mark_sent(msg["id"], now, results, error=error)
    elif sent:
        scheduled.mark_sent(msg["id"], now, results)
    else:
        error = error or (failed[0].get("error") if failed else "Nothing was sent.")
        # A server the bot has left is final: a daily message that can never be
        # delivered must not come back every morning to fail again.
        scheduled.mark_failed(msg["id"], now, error, results, final=guild is None)

    name = getattr(guild, "name", msg.get("guild_id"))
    print(
        f"[SCHEDULE] {scheduled.describe(msg)}: sent to {len(sent)}/{len(sent) + len(failed)} "
        f"channel(s) in {name}{'' if not error else ' — ' + error}",
        flush=True,
    )
    return {"sent": sent, "failed": failed, "results": results, "error": error}


_last_prune = 0.0


async def _scheduler_tick(now: int | None = None) -> list[dict]:
    """Deliver whatever is due, once. Returns what it did, for the log and tests."""
    global _last_prune
    if not _bot_ready():
        # Nothing is consumed while the bot is offline: the messages stay queued,
        # and the catch-up window below is what decides whether a late one is
        # still worth sending.
        return []

    now = int(now if now is not None else time.time())
    scheduled.sweep_stale(now)
    if now - _last_prune > 3600:
        _last_prune = now
        pruned = scheduled.prune(now)
        if pruned:
            print(f"[SCHEDULE] Pruned {pruned} finished message(s) older than "
                  f"{scheduled.HISTORY_DAYS} days.", flush=True)

    done = []
    for msg in scheduled.due(now):
        if not scheduled.claim(msg["id"], now):
            # Somebody else has it — another bot process, or the previous tick
            # still finishing. The claim is the only thing that decides who posts.
            continue
        lateness = now - int(msg.get("run_at") or now)
        if lateness > scheduled.MISS_CATCHUP_SECONDS:
            reason = (
                f"skipped — it was due {_human_delay(lateness)} ago, which is past the "
                f"{scheduled.MISS_CATCHUP_SECONDS // 3600}h catch-up window"
            )
            scheduled.mark_missed(msg["id"], now, reason)
            print(f"[SCHEDULE] {scheduled.describe(msg)}: {reason}", flush=True)
            done.append({"id": msg["id"], "status": "missed", "error": reason, "results": []})
            continue
        try:
            outcome = await _deliver_scheduled(msg, now)
        except Exception as e:
            # ``_deliver_scheduled`` is written not to raise, but a message that
            # somehow does must not take the rest of the queue down with it — and
            # leaving it claimed would hide a real failure behind a stale sweep.
            scheduled.mark_failed(msg["id"], now, f"Delivery failed: {e}")
            print(f"[SCHEDULE] {scheduled.describe(msg)}: delivery raised: {e}", flush=True)
            done.append({"id": msg["id"], "status": "failed", "error": str(e), "results": []})
            continue
        done.append({
            "id": msg["id"],
            "status": "sent" if outcome["sent"] else "failed",
            "error": outcome["error"],
            "results": outcome["results"],
        })
    return done


async def scheduler_loop():
    """Deliver scheduled messages until the process stops."""
    import asyncio

    print(
        f"[SCHEDULE] Delivery loop started — checking every {SCHEDULER_TICK_SECONDS}s.",
        flush=True,
    )
    while True:
        try:
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)
            await _scheduler_tick()
        except Exception as e:
            # One bad row must not stop every future message from being sent.
            print(f"[SCHEDULE] Loop error (will retry): {e}", flush=True)
            await asyncio.sleep(60)


def start_scheduler(bot):
    """Start the delivery loop on the bot's own event loop."""
    if bot is None or getattr(bot, "loop", None) is None:
        print("[SCHEDULE] No event loop — nothing will be delivered.", flush=True)
        return None
    return bot.loop.create_task(scheduler_loop())


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
