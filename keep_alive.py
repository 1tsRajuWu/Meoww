"""
Real-Time Web Admin Dashboard & Keep-Alive Server for Meow Discord Bot.
Enables administrators to toggle modules and features ON/OFF with 0-second (1:1 instant) latency.

Code Architecture & Navigation:
===============================
1. CONFIG & SETUP (Lines 1-100):
   Flask initialization, session cookies (HttpOnly, Lax, Secure), memory body limits.
2. AUTH & CREDENTIALS (Lines 100-340):
   Passkey comparison, magic single-use links, Discord OAuth2 sessions.
3. RATE LIMITING & SECURITY (Lines 340-750):
   Sliding-window IP rate limiters, CSRF validation, CSP & Cache-Control security headers.
4. HEALTH & UTILITY ROUTES (Lines 750-990):
   /health, /api/voice-debug, /api/live-logs, /api/config.
5. PUBLIC PAGES (Lines 990-1100):
   Landing page (/), /privacy, /terms, and /pricing.
6. SESSION & OAUTH FLOWS (Lines 1100-1380):
   /login, /logout, /login/discord, /login/discord/callback, /servers.
7. MARKET VISUALIZATION (Lines 1380-1440):
   /market, /market/<symbol>, /api/market/quotes, /api/market/candles.
8. ADMIN DASHBOARD & MODULE CONTROL (Lines 1440-1820):
   /dashboard, /api/toggle, /api/command-scope, /api/toggle-all.
9. BOT IDENTITY API (Lines 1820-2000):
   /api/identity, /api/identity/image/<kind>.
10. COMPOSER & MENTIONABLES (Lines 2000-2620):
    /api/channels, /api/mentionables, /api/send-message.
11. MESSAGE SCHEDULER (Lines 2620-3020):
    /api/scheduled, /api/schedule-message, cancellation, manual triggers, delivery alerts.
12. RUNNER & BACKGROUND LOOPS (Lines 3020-3300):
    Background scheduled message daemon and keep_alive() entrypoint.
"""
import json
import math
import os
import re
import sys
import time
import secrets
import urllib.parse
from collections import defaultdict, deque
from threading import Lock, Thread
import socket
import datetime
import html as _html
from flask import (
    Flask, render_template, request, redirect, send_from_directory, session, jsonify,
    Response,
)
from utils import (
    command_publish, command_scope, config_audit, db, digest, discord_auth, features,
    figures, guild_directory, hub, identity, modules, plans, scheduled, site_guard,
    uptime_status, webdocs,
)

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


#: Static assets are served with a **one-year immutable** cache by the `_headers`
#: file the static build emits, which is only safe when the URL changes as soon
#: as the file does. Hand-written `?v=` strings do not survive contact with this
#: repository: the sheet had none at all while the build's own comment claimed
#: they were everywhere, so a year of CSS edits would never have reached a
#: returning visitor on the CDN — the worst kind of bug, because the site looks
#: right for whoever deployed it and stale for everybody else. The token is
#: derived from the file itself instead of being maintained by hand.
def asset_version(name: str) -> str:
    """A short token that changes whenever ``static/<name>`` changes."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", name)
    try:
        stat = os.stat(path)
    except OSError:
        return "0"
    return f"{int(stat.st_mtime):x}{stat.st_size:x}"


# Four environments render these templates in this repository — this app, the
# `scripts/preview_dashboard.py` harness, and two test helpers that build a bare
# Jinja environment to render a page and read it — and Jinja 3 gives each of them
# its own globals dict, so there is no single place to put this. Each registers
# the same function, and a missing registration fails loudly the moment anything
# renders that page rather than quietly shipping an unversioned URL.
app.jinja_env.globals["asset_version"] = asset_version
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
# A self-hosted reverse proxy cannot be fingerprinted the way Render can, and the
# server binds 0.0.0.0 — so without this switch a deployment behind its own TLS
# terminator issued a session cookie with no Secure flag, and a plaintext request
# to port 8080 would carry it. DASHBOARD_HTTPS=1 says "this host is HTTPS only".
_https_only_override = (os.environ.get("DASHBOARD_HTTPS") or "").strip().lower() in {"1", "true", "yes"}
#: Almost every body here is a small JSON object — a message of at most 2000
#: characters, a switch, a schedule — and 16 KB is generous for that.
JSON_BODY_LIMIT = 16 * 1024
#: The one exception is ``/api/identity``, which carries two images of up to
#: ``identity.MAX_IMAGE_BYTES`` each plus the rest of the form.
UPLOAD_BODY_LIMIT = 2 * identity.MAX_IMAGE_BYTES + 64 * 1024
#: The app-level cap is the **outer** bound Werkzeug enforces while a body
#: streams, so it has to be the largest legitimate request. The per-route limit
#: is what actually applies (see ``_enforce_body_limit``).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_behind_https_host or _https_only_override,
    SESSION_COOKIE_NAME="meow_session",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=8),
    MAX_CONTENT_LENGTH=UPLOAD_BODY_LIMIT,
)

BOT_INSTANCE = None
#: The bot's own global avatar URL, cached at boot so the dashboard template
#: can show it without a per-request lookup. Set once in ``keep_alive()``
#: after the bot connects.
_BOT_AVATAR_URL: str | None = None
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
        "endpoints are disabled. Admins can still sign in with the one-time link "
        "from the ⚡ Open Dashboard button on /help. Set ADMIN_PASSKEY to enable "
        "passkey login.",
        flush=True,
    )


def _passkey_ok() -> bool:
    """Validate ADMIN_PASSKEY from header or JSON body — never from the query string.

    Throttled exactly like the login form, which guards the same secret: this was
    the *unlimited* way to guess it. The three passkey endpoints answer an
    exhausted window with 429 via ``_passkey_denied()``.
    """
    if not ADMIN_PASSKEY:
        return False
    if _passkey_attempts_left() <= 0:
        return False
    provided = request.headers.get("X-Admin-Passkey") or ""
    if not provided and request.method in ("POST", "PUT", "PATCH"):
        data = request.get_json(silent=True) or {}
        provided = str(data.get("passkey") or "")
    if not provided:
        _record_passkey_failure()
        return False
    if secrets.compare_digest(provided, ADMIN_PASSKEY):
        _clear_passkey_failures()
        return True
    _record_passkey_failure()
    return False


def _passkey_denied():
    """The passkey endpoints' refusal: 401 for a wrong secret, 429 once spent.

    The attempt that spends the last slot gets the 429, because it is refused for
    the window being closed rather than for the secret being wrong. Either way at
    most ``PASSKEY_MAX_ATTEMPTS`` guesses are ever evaluated, and a blocked client
    is not recorded again — so the counter cannot be grown by waiting.
    """
    if not ADMIN_PASSKEY:
        return jsonify({"error": "Passkey authentication is disabled"}), 401
    if _passkey_attempts_left() <= 0:
        return jsonify({"error": "Too many attempts. Wait a few minutes and try again."}), 429
    return jsonify({"error": "Unauthorized"}), 401


_SECRETISH = __import__("re").compile(
    r"(?i)(token|passkey|password|authorization|bearer|api[_-]?key|secret)\s*[:=]\s*\S+"
    r"|[\w-]{24,}\.[\w-]{6}\.[\w-]{25,}"  # Discord bot token shape
    r"|Bearer\s+[A-Za-z0-9._-]{20,}"
)

#: When this process came up. The deploy is a single process, so this is the only
#: honest answer to "how long has the bot been running" that does not require
#: asking Discord — and it is the number an operator looks at after a restart.
PROCESS_STARTED_AT = time.time()

MAGIC_TTL_SECONDS = 15 * 60
MAGIC_TOKENS = {}
_magic_lock = Lock()
_login_lock = Lock()
_login_attempts = defaultdict(deque)  # ip -> timestamps
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5

# The API's passkey is the same credential the login form protects, so it gets the
# same treatment: without a window, /api/set-module was an unlimited oracle for a
# long-lived secret that is typed once into a host's dashboard and never rotated.
PASSKEY_WINDOW_SECONDS = 15 * 60
PASSKEY_MAX_ATTEMPTS = 10
_passkey_lock = Lock()
_passkey_attempts = defaultdict(deque)  # ip -> timestamps

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
    try:
        from flask import has_request_context, request
        if has_request_context() and _trust_proxy_headers:
            forwarded_host = (request.headers.get("X-Forwarded-Host") or "").strip()
            if forwarded_host:
                proto = (request.headers.get("X-Forwarded-Proto") or "https").strip()
                return f"{proto}://{forwarded_host}".rstrip("/")
    except Exception:
        pass
    # Render sets this to the service's public URL automatically.
    hosted = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if hosted:
        return hosted
    if os.environ.get("RENDER") or os.environ.get("RAILWAY_ENVIRONMENT"):
        return "https://meow-discord-bot.onrender.com"
    # Anywhere else the dashboard is a local one, and the admin is on the same
    # machine as the process that minted the token.
    return f"http://localhost:{os.environ.get('PORT', '8080')}"


def oauth_redirect_uri() -> str:
    """The callback URL Discord must have registered for this deployment.

    Built from where *this* process is reachable rather than from a constant:
    the same mistake as the one-time link (a token minted here and redeemed
    against another host) is available to OAuth too, and Discord refuses a
    callback that does not match what the Developer Portal has on file — which
    is the single most common reason a sign-in "just says no".
    """
    return f"{dashboard_base_url().rstrip('/')}/login/discord/callback"


def _oauth_setup_hint() -> str:
    """Why Discord sign-in is off — and this text goes on a *public* page.

    It used to name the two settings it is about, one of which is a credential,
    which is the sentence this project's own rule forbids everywhere else: a
    count is safe, a name is a reconnaissance hint, and `/health` was rewritten
    for exactly this reason ("this deployment has no sign-in secret" is not
    something to hand a stranger). It is also the "/login" page, so it is what a
    visitor sees when they try to sign in and cannot.

    The operator loses nothing: the reason they are reading this is that sign-in
    does not work, the Developer Portal is where OAuth credentials live and
    nothing else is, and the exact list of what this deployment is missing is on
    the audit — behind the sign-in that works.
    """
    if discord_auth.enabled():
        return ""
    return (
        "Signing in with Discord is switched off on this deployment: its OAuth "
        "credentials are not set. Whoever runs it adds them from the Developer "
        "Portal's OAuth2 page, registering the callback URL shown below."
    )


def _safe_next(raw) -> str | None:
    r"""A return path we are willing to follow after signing in.

    ``next`` arrives in a URL, so it is attacker-controlled: only a same-site
    absolute path is allowed, which excludes ``//evil.example`` (a protocol-
    relative URL browsers treat as another host) and anything with a scheme.

    Control characters are refused as well, and that is not extra caution — it
    was a real open redirect. A *tab* is stripped when the redirect's ``Location``
    header is built, so ``/\t/evil.example`` reached the browser as
    ``//evil.example``: the person completed Discord's sign-in on this domain and
    was then handed to somebody else's site, which is the whole shape of a
    phishing link. A newline or carriage return is worse again — the header setter
    refuses to write the value at all, so the callback raised and answered **500**
    *after* the sign-in had succeeded. A backslash is refused for the same reason
    as a tab one layer down: browsers normalise it to ``/`` for http(s), so
    ``/\evil.example`` is another way to spell ``//evil.example``.
    """
    if not raw:
        return None
    candidate = str(raw)
    if not candidate.startswith("/") or candidate.startswith("//"):
        return None
    if "\\" in candidate:
        return None
    for char in candidate:
        code = ord(char)
        if code < 0x20 or code == 0x7F:
            return None
    return candidate[:512]


def _login_page(error=None, status: int | None = None):
    """The sign-in page, with every method's availability read from the environment."""
    name = _bot_display_name()
    body = render_template(
        "login.html",
        error=error,
        csrf_token=get_csrf_token(),
        passkey_enabled=bool(ADMIN_PASSKEY),
        oauth_enabled=discord_auth.enabled(),
        oauth_callback=oauth_redirect_uri(),
        oauth_hint=_oauth_setup_hint(),
        bot_name=name,
        bot_initial=(name[:1] or "M").upper(),
    )
    return (body, status) if status else body


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


def _passkey_attempts_left() -> int:
    """How many passkey guesses this client has left in the current window."""
    now = time.time()
    ip = _client_ip()
    with _passkey_lock:
        # A scanner walking through addresses would otherwise grow this map forever.
        if len(_passkey_attempts) > 5000:
            for stale in [
                key for key, stamps in _passkey_attempts.items()
                if not stamps or now - stamps[-1] > PASSKEY_WINDOW_SECONDS
            ]:
                _passkey_attempts.pop(stale, None)
        bucket = _passkey_attempts[ip]
        while bucket and now - bucket[0] > PASSKEY_WINDOW_SECONDS:
            bucket.popleft()
        return PASSKEY_MAX_ATTEMPTS - len(bucket)


def _record_passkey_failure() -> None:
    with _passkey_lock:
        _passkey_attempts[_client_ip()].append(time.time())


def _clear_passkey_failures() -> None:
    with _passkey_lock:
        _passkey_attempts.pop(_client_ip(), None)


def _schedule_cloud_backup():
    """Push the database to the cloud right away after a change.

    The periodic backup only runs every 25s, so a restart in that window would
    restore an older snapshot and quietly undo the administrator's change.

    One worker, coalesced: this used to start a thread per click, each one reading
    and uploading the whole database, so holding Enter on a toggle queued a pile of
    concurrent full-database uploads. A request that arrives while one is already
    pending is the same request.
    """
    global _backup_pending
    try:
        from utils.cloud_sync import backup_to_cloud_sync
    except Exception:
        return
    with _backup_pending_lock:
        if _backup_pending:
            return
        _backup_pending = True

    def _worker():
        global _backup_pending
        try:
            backup_to_cloud_sync()
        except Exception as e:
            print(f"[MODULES] Could not schedule cloud backup: {e}", flush=True)
        finally:
            with _backup_pending_lock:
                _backup_pending = False

    try:
        Thread(target=_worker, daemon=True).start()
    except Exception as e:
        _backup_pending = False
        print(f"[MODULES] Could not schedule cloud backup: {e}", flush=True)


def _known_guilds():
    """Every server the bot is in — live where the gateway has answered, and
    remembered where it has not yet.

    ``BOT_INSTANCE.guilds`` is Discord's cache, and it is **empty in a fresh
    process** until the handshake finishes. That window is tens of seconds on
    every restart, and on a host that stops the process when idle it is the
    window a visitor is most likely to arrive in — the consoles read "Unknown
    server" and an operator signing in with Discord was offered no servers at
    all. ``utils/guild_directory.py`` writes the names down at boot for exactly
    this moment.

    Live rows win, and keep their place at the front, so a server renamed in
    Discord shows its new name immediately and the picker keeps the bot's own
    order while the cache is warm.
    """
    known: dict[int, dict] = {}
    if BOT_INSTANCE:
        for guild in BOT_INSTANCE.guilds:
            known[guild.id] = {"id": guild.id, "name": guild.name}
    for row in guild_directory.read_all():
        # ``setdefault``, not assignment: a live row must not be replaced by the
        # remembered name it might have had before somebody renamed the server.
        known.setdefault(row["id"], {"id": row["id"], "name": row["name"]})
    return list(known.values())


def _visible_guilds():
    """The servers **this session** may act on, in the order it may see them.

    A passkey session is the operator's and sees every server the bot is in. A
    Discord sign-in sees the servers that person administers; a one-time link
    sees the one server it was minted for. The list is derived from the bot's
    live guilds every time rather than trusted from the session, so a server the
    bot has been removed from stops being offered immediately.
    """
    known = {g["id"]: g for g in _known_guilds()}
    scope = session.get("auth_scope")
    if scope == "user":
        # Only the *ids* live in the session. The session is a signed cookie, and
        # a name per server would push a person who administers a few dozen of
        # them past the 4 KB a browser will store — a session that vanishes on
        # sign-out with no error anywhere, which is the worst kind of bug to
        # debug. Names come from the bot's live cache instead, so a renamed
        # server shows its new name and one the bot has left disappears by
        # itself.
        wanted = []
        for raw in session.get("admin_guild_ids") or []:
            gid = _int_or_none(raw)
            if gid is not None:
                wanted.append(gid)
        return [known[gid] for gid in wanted if gid in known]
    if scope == "guild":
        gid = _int_or_none(session.get("guild_id"))
        return [known[gid]] if gid in known else []
    if scope == "global":
        return list(known.values())
    return []


def _guild_icon(guild_id) -> str:
    """A server's picture as a URL **this origin** can serve, or empty.

    Read from the gateway's own cache rather than stored in the session, for the
    same reason the names are: a cookie is not a place to keep a copy of
    Discord's state.

    The address returned is our own ``/api/image`` rather than Discord's CDN, and
    that is the whole point of the function. The dashboard sets
    ``img-src 'self' data:``, which is worth keeping — it is what stops a page
    whose template was tampered with from dialling out — but it also **blocked
    every ``cdn.discordapp.com`` URL the templates emitted**, so server icons, a
    signed-in person's picture and the bot's own avatar all rendered as a broken
    image. Uploaded pictures worked only because ``/api/identity/image`` already
    served them from here. This is that same idea for the pictures we do not own.

    ``v`` is Discord's own icon hash, so replacing a server's picture changes the
    URL and no stale copy survives the change.
    """
    if not BOT_INSTANCE or not guild_id:
        return ""
    try:
        guild = BOT_INSTANCE.get_guild(int(guild_id))
        icon = getattr(guild, "icon", None) if guild else None
        if icon is None or not str(getattr(icon, "url", "") or ""):
            return ""
        version = str(getattr(icon, "key", "") or "")
        query = f"kind=guild&guild_id={int(guild_id)}"
        if version:
            query += f"&v={urllib.parse.quote(version, safe='')}"
        return f"/api/image?{query}"
    except Exception:
        return ""


def _discord_image_url(kind: str, raw_guild_id=None) -> str:
    """Which Discord picture a name means, as an address to fetch — or ``""``.

    Every branch reads a **cached Discord object**, never a URL from the query
    string, so the only addresses that can reach the fetch are ones this process
    built from Discord's own answers. ``discord_auth.fetch_image`` re-checks the
    host regardless, because a check that only lives at the call site is one
    refactor away from being deleted.
    """
    if kind == "me":
        return str((session.get("discord_user") or {}).get("avatar_cdn") or "")
    if kind == "bot":
        return str(_BOT_AVATAR_URL or "")
    if kind == "guild":
        # The server has to be one this session may see at all, or the route
        # becomes a way to enumerate the bot's servers by picture.
        guild_id = _resolve_guild_id(raw_guild_id)
        if guild_id is None or guild_id not in {g["id"] for g in _visible_guilds()}:
            return ""
        guild = BOT_INSTANCE.get_guild(int(guild_id)) if BOT_INSTANCE else None
        icon = getattr(guild, "icon", None) if guild else None
        return str(getattr(icon, "url", "") or "") if icon is not None else ""
    return ""


@app.route("/api/image")
def api_discord_image():
    """A Discord picture, served from our own origin.

    Not a caching proxy and not an image host: it answers for exactly three
    pictures — the bot's avatar, the signed-in person's avatar, and a server icon
    the session may already see — and returns nothing for anything else. That
    whitelist, plus the host check inside ``fetch_image``, is what keeps a route
    that makes outbound requests from being an outbound-request gadget.

    A 404 here is a **normal** answer, not a failure: it is what tells the page to
    draw the server's initial instead of an image, which is why the templates put
    the letter underneath and let the picture cover it.
    """
    if not is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    url = _discord_image_url(
        str(request.args.get("kind") or ""), request.args.get("guild_id")
    )
    if not url:
        return jsonify({"success": False, "error": "No picture to show"}), 404
    fetched = discord_auth.fetch_image(url)
    if fetched is None:
        return jsonify({"success": False, "error": "Discord did not return that picture"}), 404
    data, mime = fetched
    response = Response(data, mimetype=mime)
    # Private, because the answer depends on who is asking. Ten minutes is short
    # enough that a changed picture appears on its own and long enough that a
    # list of servers is one request per picture per visit, not per repaint.
    response.headers["Cache-Control"] = "private, max-age=600"
    return response


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


def _foreign_guild_refusal(requested):
    """A refusal when a *write* names a server this session does not run, else ``None``.

    ``_resolve_guild_id`` deliberately falls back to a server the session does
    run, and that is right for a page: a stale bookmark should land somewhere
    real rather than on nothing, and it can never land on a server the person
    does not administer. It is wrong for a **change**, because it silently
    retargets it — asking to disable moderation in one server disabled it in
    another and answered ``success``, so the caller was told the server it named
    had changed when a different one had. Measured, not theorised: a session that
    administers only server *Mine* asked ``/api/toggle`` for server *Hmm* and the
    reply was ``{success: true, guild_id: <Mine>, ...}``.

    Reads keep the fallback. Writes get a refusal that says which server was
    refused and why, and the two scopes that name a server at all — a Discord
    sign-in and a one-time link — are both covered.
    """
    if requested in (None, "", 0):
        # No server named: the fallback is the whole point of the call.
        return None
    if session.get("auth_scope") not in ("user", "guild"):
        # The operator's scopes see every server the bot is in, by design.
        return None
    gid = _int_or_none(requested)
    if gid is None:
        # Not a number at all; resolution answers that case in its own words.
        return None
    if gid in {g["id"] for g in _visible_guilds()}:
        return None
    print(
        f"[DASHBOARD] Refused a write naming server {gid}: this session may act "
        "only on the servers it was signed in for.",
        flush=True,
    )
    return jsonify({
        "success": False,
        "error": "You do not administer that server.",
    }), 403


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

    if session.get("auth_scope") == "user":
        # A Discord sign-in reaches only the servers it was told this person
        # administers, and the set is recomputed from the bot's live guilds on
        # every request — so a server they were demoted from, or one the bot has
        # left, stops being reachable the moment either becomes true rather than
        # when their session happens to expire.
        allowed = {g["id"] for g in _visible_guilds()}
        wanted_candidates = []
        if requested not in (None, "", 0):
            try:
                wanted_candidates.append(int(requested))
            except (TypeError, ValueError):
                pass
        stored = _int_or_none(session.get("guild_id"))
        if stored is not None:
            wanted_candidates.append(stored)
        for gid in wanted_candidates:
            if gid in allowed:
                return gid
        # Someone who manages none of them is not an error: they are signed in,
        # and the picker's job is to tell them what to do about it.
        return min(allowed) if allowed else None

    if session.get("auth_scope") != "global":
        return None

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


_backup_pending = False
_backup_pending_lock = Lock()


def _require_auth_api():
    if not is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    if not validate_csrf():
        return jsonify({"success": False, "error": "Invalid or missing CSRF token"}), 403
    return None


def _body_limit_for(path: str) -> int:
    """This route's real limit. Only the picture upload is allowed to be big."""
    return UPLOAD_BODY_LIMIT if path == "/api/identity" else JSON_BODY_LIMIT


def _body_too_large_error(path: str, limit: int) -> str:
    """The refusal, naming the limit that was actually hit.

    One sentence used to cover both cases, and it talked about messages — so an
    image upload that was too big was answered with "That message is larger than
    the dashboard accepts (16 KB). Shorten it or split the send.", which names
    neither the file nor a limit it could ever have met.
    """
    if path == "/api/identity":
        return (
            f"That upload is {limit // (1024 * 1024)} MB at most — two pictures of "
            f"{identity.MAX_IMAGE_BYTES // (1024 * 1024)} MB each, plus the form. "
            "Pick a smaller file."
        )
    return (
        f"That message is larger than the dashboard accepts ({limit // 1024} KB). "
        "Shorten it or split the send."
    )


@app.before_request
def _enforce_body_limit():
    """Apply each route's own body limit, before the view reads anything.

    Flask's ``MAX_CONTENT_LENGTH`` is a single number for the whole app and it is
    checked *before* any view runs, so the one route that legitimately carries
    megabytes made the app-level cap that number — and then the 16 KB the
    dashboard is happy to accept everywhere else had to be re-stated per route,
    which is what this does. Without it the "picture for this server" panel could
    only ever accept a file smaller than a favicon, which is exactly what it did:
    a 20 KB PNG was refused where a 48-byte one was applied.
    """
    limit = _body_limit_for(request.path)
    try:
        # Flask 3.1's per-request switch: the bound actually applied while the
        # body streams. Harmless where it does not exist, because the explicit
        # check below and the app-level cap still hold.
        request.max_content_length = limit
    except Exception:
        pass
    length = request.content_length
    if length is not None and length > limit:
        return jsonify({"success": False, "error": _body_too_large_error(request.path, limit)}), 413
    return None


@app.errorhandler(413)
def _payload_too_large(_error):
    """A JSON answer for the JSON-only client.

    Flask's default 413 is HTML, so the page's ``response.json()`` threw and the
    catch-all turned it into "Request failed (413)" — honest, but it never said
    which limit was hit or what to do about it. This is the backstop for a body
    that announced no length (or lied about it) and was caught while streaming.
    """
    limit = _body_limit_for(request.path)
    return jsonify({
        "success": False,
        "error": _body_too_large_error(request.path, limit),
    }), 413


@app.errorhandler(429)
def _rate_limited(_error):
    return jsonify({
        "success": False,
        "error": "Too many requests at once — wait a moment and try again.",
    }), 429


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
    # card blamed the library.
    #
    # `style-src` and `font-src` no longer name Google Fonts. They were allowed
    # for the dashboard's Google Fonts and **nothing in the tree ever loaded
    # one** — the stylesheet sets a system font stack on purpose, which is
    # written down in its own header. A permission kept for a resource that no
    # page requests is a third-party origin a compromised page could reach, held
    # open by a comment rather than by a need.
    # The hub is tighter than everything else, and deliberately: its pages carry
    # no inline script and no `style` attribute at all, so it does not need the
    # `'unsafe-inline'` the dashboard and the market page do. Overriding it here
    # means the same strict policy governs the hub on *this* host and on the
    # static one, instead of the CDN copy being held to a rule the app never
    # applies — and it means an inline script added to a hub template later fails
    # in development rather than only in production.
    if request.path == "/hub" or request.path.startswith("/hub/"):
        response.headers["Content-Security-Policy"] = hub.CSP
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
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
    # Static assets (css, svg, js) require revalidation so new deploys are instantly picked up
    if request.path.startswith("/static/") or request.path.startswith("/hub/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    if request.headers.get("X-Forwarded-Proto", request.scheme) == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def _gateway_state() -> tuple[bool, bool]:
    """(has this process ever logged in, is the gateway connected right now).

    ``_has_started`` is set once inside ``on_ready`` and never cleared, so on its
    own it answers "did the bot ever connect" — a bot that has since dropped off
    the gateway still reads as ready, which is the opposite of reassuring when
    somebody is asking whether it is online. ``is_ready()`` is the live answer,
    and the two disagree exactly in the case worth knowing about.
    """
    bot = BOT_INSTANCE
    if bot is None:
        return False, False
    started = bool(getattr(bot, "_has_started", False))
    try:
        return started, bool(bot.is_ready())
    except Exception:
        return started, False


def _gateway_label(started: bool, connected: bool) -> str:
    """One word for the state, so a monitor needs no knowledge of the flags."""
    if connected:
        return "connected"
    return "reconnecting" if started else "never_connected"


#: The badge's three states: label, class, and whether it pulses.
#:
#: Three rather than two because the flag both consoles compared — ``bot_ready``,
#: i.e. "this process has logged in at least once" — is a latch. It is set on the
#: first successful login and never cleared, so a badge built on it reads "Live"
#: while the gateway is reconnecting, which is the one case somebody looks at a
#: badge for. A bot that is reconnecting is not the same claim as one that has
#: never been up, and neither is "Live".
_BADGE_STATES: dict[str, tuple[str, str, bool]] = {
    "connected": ("Live", "badge-online", True),
    "reconnecting": ("Reconnecting", "badge-warn", False),
    "never_connected": ("Bot offline", "badge-offline", False),
}


def _badge_state() -> dict:
    """The topbar badge for the gateway right now — the live answer.

    Read by both consoles through ``templates/gateway_badge.html``, and kept
    current after the page loads by ``static/gateway.js``. One definition for the
    two of them, because the two copies this replaces were the same mistake
    written twice and only one of them had gone stale.
    """
    started, connected = _gateway_state()
    label, css, pulse = _BADGE_STATES[_gateway_label(started, connected)]
    return {"label": label, "css": css, "pulse": pulse, "connected": connected}


@app.route("/health")
def health():
    started, connected = _gateway_state()
    return jsonify({
        "status": "ok",
        # Kept for the monitors that already read it: "this process has logged in
        # at least once".
        "bot_ready": started,
        # The live answer, which is what "is it connected?" actually means.
        "gateway": _gateway_label(started, connected),
        # Whether this deployment is *configured*, which is a different question
        # from whether it is up — and the one there was previously no way to ask.
        # It does not touch the top-level ``status``: the process being healthy
        # and the deployment being fully configured are separate claims, and a
        # monitor that reads ``status`` should not be told the bot is down
        # because nobody pasted a webhook URL.
        #
        # Counts only, and no names: this endpoint is unauthenticated, and "this
        # deployment has no sign-in secret" is not a sentence to hand a stranger.
        # /api/config has the names, behind the administrator's sign-in, and so do
        # the two console payloads — never this one.
        "config": config_audit.health_json(),
        # Whether the public site is publishing this checkout. A separate
        # question again, and one with no symptom in the product: every page
        # looked correct while `/bot.py` and `/render.yaml` were served from the
        # site's own address. `unknown` until the first check has run in this
        # process, and `unreachable` rather than `clean` when the site did not
        # answer — a check that could not ask has not learned anything.
        #
        # Counts and no names, like `config` above: this endpoint is
        # unauthenticated. The alert that names the paths goes to Discord.
        #
        # It is deliberately **not** folded into the top-level `status`: the bot
        # being healthy and the site being wrongly configured are separate
        # claims, and a monitor that pages on `status` should not be woken
        # because a static host was given the wrong directory.
        "site_guard": site_guard.health_json(),
        # What UptimeRobot has seen of this host and anything else it watches.
        # A fourth separate claim, for the same reason as the third: the process
        # being healthy, the deployment being configured, the site publishing the
        # right directory and the service *having been* reliable are four answers
        # to four questions, and a monitor that pages on `status` should not be
        # woken by any of the other three.
        #
        # Counts and one ratio, no names — this endpoint is unauthenticated, and
        # the rows (monitor names and their URLs) are behind `/api/uptime`.
        "uptime": uptime_status.health_json(),
        "timestamp": int(time.time()),
    })


@app.route("/api/figures")
def api_figures():
    """The landing page's numbers, for the copy that was built without them.

    The published site is static: CI renders it once and a CDN serves it, so the
    figures baked into it are the figures of the day it was built. This is the
    other half of that — the same payload, live — so the page can keep up
    without a rebuild, which is the whole reason it exists.

    It carries a cross-origin allowance for the site's own address, and that is
    not decoration: the caller is a *browser* on the Pages host, and without the
    header the call is refused, the failure is invisible (a refused `fetch`
    reaches no banner), and the page silently keeps its build-time numbers
    forever — the exact defect this endpoint was added to remove.

    Counts and no names, like the `uptime` and `config` blocks beside it in
    `/health`: this endpoint is unauthenticated. The per-channel and per-member
    breakdowns are behind the dashboard's sign-in and stay there.
    """
    payload = _public_figures()
    response = jsonify(payload)
    origin = (request.headers.get("Origin") or "").strip().rstrip("/")
    allowed = site_guard.site_url().strip().rstrip("/")
    if origin and allowed and origin == allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    # A minute, the same minute the snapshot is cached for. A landing page open
    # in twenty tabs is a page being read, not a reason to read the database
    # twenty times.
    response.headers["Cache-Control"] = f"public, max-age={figures.CACHE_SECONDS}"
    return response


@app.route("/api/uptime")
def api_uptime():
    """UptimeRobot's monitors, as rows, for the dashboard's status panel.

    Signed-in only, and that is the whole difference between this and the
    ``uptime`` block in ``/health``: a monitor's name is usually the address it
    watches, and the operator's own monitors may include hosts that have nothing
    to do with this product.

    It never fetches. The background loop owns the network call, so this answers
    from the last snapshot — which is also why it can be polled by a page that is
    open all day without ever costing UptimeRobot a request.
    """
    denied = _require_auth_api()
    if denied:
        return denied
    current = uptime_status.snapshot()
    return jsonify({
        "success": True,
        "configured": uptime_status.configured(),
        **current,
    })


@app.route("/api/live-logs")
def api_live_logs():
    # Prefer dashboard session; allow header passkey for ops tooling.
    if is_authenticated():
        # The ring is process-wide, so only the unscoped (passkey) sign-in may read
        # it: a guild-scoped link is *one server's* session, and these lines were
        # written while serving every server. ``_resolve_guild_id`` draws exactly
        # this line for every other endpoint.
        if session.get("auth_scope") != "global":
            return jsonify({
                "error": "Logs are available only to an administrator sign-in (ADMIN_PASSKEY).",
            }), 403
        if not validate_csrf() and request.method != "GET":
            return jsonify({"error": "CSRF"}), 403
    elif not _passkey_ok():
        return _passkey_denied()
    try:
        content = "".join(str(x) for x in list(LOG_RING)[-200:])
        content = _SECRETISH.sub(r"\1=[redacted]", content)
        return Response(content, mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return Response(f"Error fetching logs: {e}", mimetype="text/plain; charset=utf-8")


@app.route("/api/config")
def api_config():
    """What this deployment is and is not configured to do, and what it costs.

    Administrator-only, and it can disclose nothing secret by construction: it
    reports each setting's *name*, what it turns on, and whether a value is
    present — never a value. That is why it can be answered without the
    redaction pass ``/api/live-logs`` needs on a log that other code wrote.

    A session opened by a one-time link counts as an administrator here, on
    purpose. The most useful thing this endpoint has to say is why the operator
    cannot get in the other way, and ``ADMIN_PASSKEY`` being unset is exactly
    that case — so requiring the passkey would hide the answer behind the
    question. A guild-scoped session is still refused: this describes the whole
    deployment, not one server.
    """
    if is_authenticated():
        if session.get("auth_scope") != "global":
            return jsonify({
                "error": "Configuration is available only to an administrator sign-in.",
            }), 403
    elif not _passkey_ok():
        return _passkey_denied()
    return jsonify(config_audit.full_json())


@app.route("/api/set-module", methods=["POST"])
def api_set_module():
    """Passkey-authenticated module toggle — no CSRF required (internal use only)."""
    if not _passkey_ok():
        return _passkey_denied()
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
        return _passkey_denied()

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

    started, connected = _gateway_state()
    return jsonify({
        "bot_ready": started,
        "gateway": _gateway_label(started, connected),
        "opus_finder": opus_finder,
        "opus_loaded": is_opus_loaded,
        "system_ffmpeg": shutil.which("ffmpeg"),
        "libopus_so_files": (
            glob.glob("/usr/lib/libopus*.so*")
            + glob.glob("/usr/lib/*-linux-*/libopus*.so*")
            + glob.glob("/usr/lib64/libopus*.so*")
            + glob.glob("/usr/local/lib/libopus*.so*")
            + glob.glob("/lib/libopus*.so*")
            + glob.glob("/lib/*-linux-*/libopus*.so*")
            + glob.glob("/opt/homebrew/lib/libopus*.dylib*")
            + glob.glob("/usr/local/lib/libopus*.dylib*")
        ),
        "voice_clients": vcs,
    })


# ---------------------------------------------------------------------------
# The front door
# ---------------------------------------------------------------------------
# Until now this was a redirect, so the first thing anybody saw — a server owner
# following an invite, somebody reading the link in the README — was a password
# box for a panel they had never heard of. What is served here instead is the
# page that answers the three questions they actually arrive with: what is this,
# what is it doing *now*, and how do I add it.

#: How long activity is kept, for the page's own sentence. Read from the module
#: that enforces it rather than restated, because a policy that quotes a number
#: the code no longer uses is a claim, not a fact.
def _retention_days() -> int:
    try:
        from cogs import tracker
        return int(tracker.RETENTION_DAYS)
    except Exception as exc:
        print(f"[HOME] could not read the retention window: {exc}", flush=True)
        return 0


def _bot_display_name() -> str:
    """What the bot calls itself, from the live application when there is one."""
    if BOT_INSTANCE is not None:
        user = getattr(BOT_INSTANCE, "user", None)
        name = getattr(user, "name", None)
        if name:
            return str(name)
    return "Meow Bot"


def _gateway_counts() -> dict:
    """What the live gateway knows, as counts and nothing else.

    Handed to :mod:`utils.figures`, which owns the tracker's totals and the
    cache; this is the half that needs a Discord connection. A cold or sleeping
    process answers ``None`` for each figure rather than a zero, because "nobody
    has told me yet" and "there is nobody" are different claims and only one of
    them is worth printing on a landing page.
    """
    counts = {
        # Servers and members are facts about *this* deployment and can only come
        # from the gateway. Systems and commands are facts about the code and
        # come from the registry, in the units every other surface uses — the
        # tree is not read for them, because the count someone is shown should
        # not depend on whether a gateway happened to be awake. See
        # :func:`utils.features.command_count` for what that cost the last time.
        "servers": None,
        "members": None,
        "systems": features.system_count(),
        "commands": features.command_count(),
        "connected": False,
    }
    started, connected = _gateway_state()
    counts["connected"] = bool(connected)
    if started and BOT_INSTANCE is not None:
        try:
            guilds = list(BOT_INSTANCE.guilds)
            counts["servers"] = len(guilds)
            counts["members"] = sum(g.member_count or 0 for g in guilds)
        except Exception as exc:
            print(f"[FIGURES] could not read the gateway's counts: {exc}", flush=True)
    return counts


def _public_figures(*, read_tracker: bool = True) -> dict:
    """The payload the landing page draws and `/api/figures` serves.

    ``read_tracker=False`` is the static build's case: this module reads a local
    SQLite file, and a build runs on a machine that is not the deployment, so its
    totals would be somebody else's numbers published as the deployment's. See
    :func:`utils.figures.deployment`.
    """
    return figures.snapshot(gateway=_gateway_counts(), read_tracker=read_tracker)


def _home_stats() -> list[dict]:
    """The figures on the landing page, and only the ones that are true.

    The registry always knows how many systems exist and which commands they
    declare, so those two are always shown. Servers and members can only come
    from the gateway — and on a host that sleeps, a cold start would otherwise
    render a confident **0 servers**, which reads as a bot nobody uses rather
    than as a process that has not woken up yet. So they are simply absent until
    there is something to say, and the status line above them says why.
    """
    stats = [{"value": f"{features.system_count():,}", "label": "systems"}]
    started, _connected = _gateway_state()
    if started and BOT_INSTANCE is not None:
        try:
            guilds = list(BOT_INSTANCE.guilds)
            members = sum(g.member_count or 0 for g in guilds)
            stats = [
                {"value": f"{len(guilds):,}", "label": "servers"},
                {"value": f"{members:,}", "label": "members reached"},
                *stats,
            ]
        except Exception as exc:
            print(f"[HOME] could not read the live figures: {exc}", flush=True)
    stats.append({"value": f"{features.command_count():,}", "label": "commands"})
    return stats


def _static_site() -> bool:
    """Is this the published copy rather than a running process?

    The status line below is a claim about *this process*. A static host serves
    files, so there is no process, nothing will ever connect, and "a sleeping
    free-tier host takes a moment to wake" becomes a promise that never resolves
    — a visitor reads a permanently half-started bot. Set only by
    ``scripts/build_site.py``; the bot never sets it.
    """
    return (os.environ.get("MEOW_STATIC_SITE") or "").strip().lower() in {"1", "true", "yes"}


def _home_status() -> dict:
    """The one line that says whether the bot is actually connected.

    Three states rather than two, because "never connected" and "reconnecting"
    are different things to a visitor and only one of them is worth waiting for.
    """
    if _static_site():
        return {
            "class": "pill-ok",
            "dot": "dot-live",
            "label": "Always on",
            "detail": (
                "Served by Cloudflare Pages, so it answers whether the bot is awake "
                "or not. Live status and figures are on the bot's own host."
            ),
        }
    started, connected = _gateway_state()
    # "Connected right now" and "has been connected lately" are different
    # claims, and the second one is the one a visitor is making. When
    # UptimeRobot has actually measured it, the sentence carries the measurement
    # and says which window it covers; when it has not, the sentence is what it
    # was. Nothing is invented either way.
    measured = ""
    if connected:
        current = uptime_status.snapshot()
        ratio = current.get("ratio_30d")
        if ratio is not None and current.get("total"):
            measured = (
                f" UptimeRobot has measured {ratio:.2f}% over the last 30 days "
                f"across {current['total']} monitor"
                f"{'s' if current['total'] != 1 else ''}."
            )
    if connected:
        return {
            "class": "pill-ok",
            "dot": "dot-live",
            "label": "Online",
            "detail": (
                "Connected to Discord right now — the figures below are live." + measured
            ),
        }
    if started:
        return {
            "class": "pill-warn",
            "dot": "dot-idle",
            "label": "Reconnecting",
            "detail": (
                "The bot is running but its gateway session is rebuilding — it answers "
                "commands again on its own."
            ),
        }
    return {
        "class": "",
        "dot": "dot-idle",
        "label": "Starting up",
        "detail": (
            "This process has not connected yet (a sleeping free-tier host takes a moment "
            "to wake). Live figures appear as soon as it does."
        ),
    }


def _invite_context(*, guild_id: int | str | None = None) -> dict:
    """Both invite links, or neither, for every page that offers to add the bot.

    Built here rather than at each call site because there are two of them now
    and they must always agree: the page that offers the Administrator link has
    to offer the least-privilege one beside it, or a careful owner is left with
    no way in that they are willing to take. An empty string when the application
    id is unset is the same "no invite" every template already tests for.
    """
    if not discord_auth.client_id():
        return {"invite_url": "", "invite_limited_url": "", "invite_here_url": ""}
    return {
        "invite_url": discord_auth.invite_url(),
        "invite_limited_url": discord_auth.invite_url(minimal=True),
        "invite_here_url": discord_auth.invite_url(guild_id=guild_id) if guild_id else "",
    }


def _inline_json(value) -> str:
    """Serialise a payload for a ``<script type="application/json">`` element.

    Serialised **here** rather than with Jinja's ``tojson``, because four
    environments render these templates in this repository — this app, the
    preview harness, and two test helpers that build a bare Jinja environment
    and read a page — and only Flask's registers that filter. A payload that only
    exists on one of the four is a landing page that renders in production and
    raises in the tooling written to check it.

    ``<`` is escaped because the result is injected into a ``<script>`` element:
    an unescaped ``</script`` anywhere in the data would close the tag and put the
    rest of the payload into the document as markup.
    """
    return json.dumps(value, separators=(",", ":")).replace("<", "\\u003c")


def _try_payload() -> str:
    """The systems as data, for the landing page's interactive panel."""
    payload = json.dumps(
        [
            {
                "id": module["id"],
                "name": module["name"],
                "icon": module["icon"],
                "category": module["category"],
                "commands": [str(c) for c in (module.get("commands") or [])],
            }
            for module in AVAILABLE_MODULES
        ],
        separators=(",", ":"),
    )
    return payload.replace("<", "\\u003c")


def _system_bars() -> list[dict]:
    """The system list as a chart: one bar per system, sized by its commands.

    The card's chart used to be nine hand-written heights — decoration that
    looked like a measurement, which is the one thing this project keeps
    removing. These are real: the registry knows exactly how many commands each
    system declares, so the chart is that count, and a bar can be clicked to say
    which system it is.

    Height is relative to the largest system rather than to the total, so the
    shape stays readable on a deployment with one system enabled and on one with
    eleven: the question the chart answers is "which of these is the big one?"
    """
    bars = [
        {
            "id": feature.id,
            "name": feature.name,
            "icon": feature.icon,
            "commands": len(feature.commands),
        }
        for feature in features.FEATURES
    ]
    top = max((bar["commands"] for bar in bars), default=0) or 1
    for bar in bars:
        # A floor, so a system with one command is still a bar rather than a
        # line at zero height that reads as "off".
        bar["pct"] = max(8, round(bar["commands"] * 100 / top))
    return bars


#: A figure that was never read. An em dash, not a zero: the modules above go to
#: some trouble to report "I do not know" separately from "none", and a card that
#: printed `0 servers` for a sleeping host would undo that in the last step.
NOT_READ = "—"


def _control_tiles(live: dict) -> list[dict]:
    """The four figures the Control panel card leads with.

    These replaced `_home_stats()[:3]`, which was the same idea with the wrong
    three: *systems* and *commands* are true of every copy of this bot ever
    started, so a visitor learned nothing about whether anybody uses it. Servers
    and the people in them are the numbers that answer that, and they are the
    ones a cold host can only say "not yet" about.
    """
    text = live.get("text") or {}
    return [
        {"key": key, "label": label, "value": text.get(key) or NOT_READ}
        for key, label in (
            ("servers", "servers"),
            ("members", "members reached"),
            ("systems", "systems"),
            ("commands", "commands"),
        )
    ]


def _activity_tiles(live: dict, period_id: str) -> list[dict]:
    """The Activity card's totals for one window.

    Every window is already in the payload the card is drawn from, so switching
    between them is a redraw rather than a request — four windows of two indexed
    reads is a minute's cache and a page that can be read offline, and a tab that
    had to fetch would be a tab that stalls on a sleeping host.
    """
    window = next(
        (p for p in (live.get("periods") or []) if p.get("id") == period_id),
        (live.get("periods") or [{}])[0],
    )
    return [
        # `NOT_READ`, not `"0"`, on the same rule `_control_tiles` above follows
        # and for a case that is not hypothetical: with no tracker tables — every
        # build of the published site — this used to print **0 messages** under a
        # card whose own module goes out of its way to report "I have no reading"
        # separately from "none". Zero is a measurement, and this was not one.
        {"key": key, "label": label, "value": window.get(key) or NOT_READ}
        for key, label in (
            ("messages_text", "messages"),
            ("voice_text", "voice hours"),
            ("people_text", "people seen"),
            ("channels_text", "channels used"),
        )
    ]


def _figures_url() -> str:
    """Where the page should ask for a fresh reading.

    Relative where the page is served by the process that *has* the figures, and
    where the published copy is reverse-proxied back to it (Cloudflare Pages, see
    ``scripts/build_site.py``). Absolute only in the one case left: the built
    copy talking to the live host directly, which is why `/api/figures` carries a
    cross-origin allowance for the site's own address.
    """
    live = (os.environ.get("MEOW_LIVE_APP_URL") or "").strip().rstrip("/")
    proxied = (os.environ.get("CLOUDFLARE_PROXY") or "").strip().lower() in {"1", "true", "yes"}
    if _static_site() and live and not proxied:
        return f"{live}/api/figures"
    return "/api/figures"


def _site_context() -> dict:
    """What every page of this site needs, built once instead of three times.

    There are three pages now — the front door, `/systems` and `/start` — and they
    share a navigation, a footer, a theme picker and an invite button. Every one of
    those reads names from here.

    The reason it is one function rather than three dictionaries is the failure the
    shared base template exists to prevent: **two places that answer the same
    question drift**. A nav link added to two pages and not the third, a Pricing
    link left standing on a page where selling is off, an invite button that knows
    a client id on one page and not the next — all three are the same defect, and
    all three are what `tests/test_selling_surface.py` and
    `tests/test_pages_site.py` walk the render calls looking for.
    """
    name = _bot_display_name()
    return {
        "bot_name": name,
        "bot_initial": (name[:1] or "M").upper(),
        "enter_url": "/login",
        "enter_label": "Sign in",
        "retention_days": _retention_days(),
        # Whether "Sign in with Discord" is a way in on *this* deployment. It
        # needs both the client id and the secret, and a page must not list a
        # method the dashboard does not offer — which `/start` did, for weeks.
        "oauth_enabled": discord_auth.enabled(),
        # Whether anything can be bought, which decides whether a Pricing link
        # exists at all. The tier prices themselves live on `/pricing`, from the
        # same model: this page used to carry its own hard-coded dollars
        # ($4.99/$14.99) and they had already drifted from the rupees the store
        # actually charges, which is the whole failure a single model prevents.
        "selling": plans.selling_enabled(),
        "source_repository": SOURCE_REPOSITORY,
        "kofi_url": KOFI_URL,
        # The registry's own answers, passed as numbers the template prints rather
        # than left for a template to count a list with. `features` is the
        # *switchable* subset; a page that shows both labels both.
        "system_count": features.system_count(),
        "command_count": features.command_count(),
        "features": features.dashboard_modules(),
        # The difference between the two counts above, by name. A page that prints
        # a total and then a shorter list has to say what the difference is; the
        # alternative is a reader counting cards and finding one missing.
        "always_on": features.always_on_names(),
        **_invite_context(),
    }


def _home_page():
    """The front door: what this is, and what it is doing right now.

    Deliberately the shortest of the three. The systems list and its demo are on
    `/systems`, the steps and the ways in are on `/start`, and this page's copies
    of those are a count, a list of names and three numbered lines — each with the
    link that says where the detail went.
    """
    return render_template(
        "home.html",
        **_site_context(),
        # Which nav link is this page. The server knows, so the rail says so with
        # `aria-current` and no script can disagree with it.
        current="home",
        status=_home_status(),
        stats=_home_stats(),
        meta_description=(
            f"{_bot_display_name()} — moderation, tickets, leveling, an economy, "
            "a live market and per-server activity tracking, driven from a "
            "real-time web dashboard."
        ),
    )


def _systems_page():
    """What is in the bot: every system, its commands, and the dashboard.

    This is where the live figures live, rather than on the front door, and that
    is a decision about conflict rather than about importance. The front door's
    band is counted up by `motion.js`, which captures each value, animates it and
    then **restores what it captured** — so a live refresh writing into the same
    `<dd>`s would be overwritten by the count-up's last frame with the figure the
    page was built with. Two writers, one node, and the loser is the live one.
    """
    # ``MEOW_STATIC_SITE`` means the tracker's SQLite file belongs to the machine
    # running the *build*, not to the deployment — and the preview panels below
    # draw their activity tiles from that snapshot server-side, so reading it here
    # published the operator's own laptop as the live figures: *467 messages this
    # week* on a public page, about a bot the reader is not running. The same
    # one-line rule the hub's context has always applied; this page never did.
    static_copy = os.environ.get("MEOW_STATIC_SITE", "").strip() == "1"
    live_figures = _public_figures(read_tracker=not static_copy)
    periods = live_figures.get("periods") or []
    # The window the Activity card opens on. Seven days, because it is the one a
    # visitor can read as "lately" without being told how old the deployment is,
    # and because it is the window the weekly digest uses.
    opened = next((p for p in periods if p["id"] == "week"), periods[0] if periods else {})
    opened_id = opened.get("id") or "week"
    return render_template(
        "systems.html",
        **_site_context(),
        current="systems",
        # What the deployment is actually doing, as counts — the figures the two
        # preview cards draw. Read once here and again by `/api/figures` for the
        # published copy, from the same cached snapshot, so the page and the
        # endpoint cannot disagree about the same minute.
        control_tiles=_control_tiles(live_figures),
        activity_tiles=_activity_tiles(live_figures, opened_id),
        activity_periods=[
            {"id": p["id"], "label": p["label"], "selected": p["id"] == opened_id}
            for p in periods
        ],
        system_bars=_system_bars(),
        figures_json=_inline_json(live_figures),
        figures_url=_figures_url(),
        # The same registry the list above is built from, as data — so the
        # interactive panel cannot advertise a system the bot does not have, and
        # cannot disagree with the static list beside it.
        try_payload=_try_payload(),
        meta_description=(
            f"Every system in {_bot_display_name()}: the commands each one declares, "
            "what switching one off does to a single server's picker, and the "
            "dashboard that comes with it."
        ),
    )


def _start_page():
    """How to add it, and how to get in afterwards."""
    return render_template(
        "start.html",
        **_site_context(),
        current="start",
        meta_description=(
            f"Add {_bot_display_name()} to a Discord server in three steps, and the "
            "ways to sign in to its dashboard."
        ),
    )


@app.route("/")
def home():
    user_agent = request.headers.get("User-Agent", "").lower()
    if "uptimerobot" in user_agent or "curl" in user_agent:
        # Uptime monitors are pointed at this process and parse this exact line;
        # the landing page above is for people, and a monitor must not be made to
        # fetch a whole document to learn one word.
        return "Meow Bot is alive and operational!"
    if is_authenticated():
        return redirect("/dashboard")
    return _home_page()


# ---------------------------------------------------------------------------
# The other two pages of the site
# ---------------------------------------------------------------------------
# No signed-in redirect on these two, deliberately: `/` sends an authenticated
# visitor to their dashboard because that is what they came back for, while a
# reader who opened `/systems` or `/start` is asking a question about the product
# and should be answered wherever they are signed in or not — the same way
# `/pricing` and the policies already behave.


@app.route("/systems")
def systems_page():
    return _systems_page()


@app.route("/start")
def start_page():
    return _start_page()


# ---------------------------------------------------------------------------
# The policies, as pages
# ---------------------------------------------------------------------------
# They were readable only inside Discord, where the whole document has to fit on
# one card inside 6000 characters — which is a budget that *forced* the wording
# to be tightened last pass, and a member reading on a phone cannot link to it.
# The same file renders here, by the same reader the Discord card uses.

#: Every policy we serve: file, page copy, and the Discord command that shows it.
POLICY_DOCUMENTS = {
    "privacy": {
        "filename": "PRIVACY.md",
        "eyebrow": "Privacy",
        "command": "/privacy",
    },
    "terms": {
        "filename": "TERMS.md",
        "eyebrow": "Terms",
        "command": "/tos",
    },
}

#: Shown in the footer, and the same repository the Discord card names.
#: Where this bot's source lives — **empty on purpose**, because it does not live
#: anywhere a visitor can read.
#:
#: The repository is private, and every surface that offered a "Source code" link
#: was handing a stranger GitHub's *"This is not the web page you are looking
#: for"* — on the footer of the page that asks them to trust this bot with their
#: server, next to links that work. A promise that resolves to a 404 is worse than
#: no promise: it reads as a project that has gone quiet, and it says so in the
#: one place a cautious visitor looks first.
#:
#: So the link is gone and the copy says the source is not published, which is
#: true. Every template that used to print it now asks whether there is one, so
#: publishing the repository again is this line and nothing else.
SOURCE_REPOSITORY = ""
KOFI_URL = "https://ko-fi.com/1tsraj"


@app.route("/support")
@app.route("/donate")
def route_support_redirect():
    return redirect(KOFI_URL)


def _policy_page(slug: str):
    """One policy document as a page, or a 404 when the file is missing."""
    from cogs import legal

    document = POLICY_DOCUMENTS[slug]
    text = legal.read_document(document["filename"])
    if not text.strip():
        return (
            f"<h1>404 — {_html.escape(document['filename'])} is missing from this deploy</h1>",
            404,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    return render_template(
        "legal.html",
        current=slug,
        body=webdocs.render(text),
        title=webdocs.title_from(text, document["filename"]),
        eyebrow=document["eyebrow"],
        command=document["command"],
        effective=_effective_date(text),
        bot_name=_bot_display_name(),
        bot_initial=(_bot_display_name()[:1] or "M").upper(),
        enter_url="/login",
        enter_label="Sign in",
        **_invite_context(),
        repository=SOURCE_REPOSITORY,
        # The nav is shared with the pages that do sell, so it asks the same
        # question they do rather than deciding for itself.
        selling=plans.selling_enabled(),
    )


def _effective_date(text: str) -> str:
    """The document's own effective date, so the page cannot state another one.

    The line reads ``**Effective Date:** September 19, 2026 · **Version 3**``, so
    the emphasis markers and the version that follows the date are stripped: the
    page says when the policy took effect and nothing it was not told.
    """
    match = re.search(r"(?i)effective\s*date\s*[:\-]\s*([^\n]+)", text or "")
    if not match:
        return "see the document"
    value = match.group(1).replace("*", "").replace("_", "").strip()
    value = re.split(r"\s*[·|(]", value, maxsplit=1)[0].strip().rstrip(".")
    return value[:40] or "see the document"


@app.route("/privacy")
def policy_privacy():
    return _policy_page("privacy")


@app.route("/terms")
def policy_terms():
    return _policy_page("terms")


@app.route("/pricing")
def pricing_page():
    name = _bot_display_name()
    # The prices and the limits come from the model the bot enforces, so the page
    # cannot advertise a tier or a number the code does not know about — and the
    # "is this enforced yet" sentence is read from the deployment rather than
    # written into the template, because it is a fact about this process.
    #
    # ``selling`` gates the premium surface itself. While it is off the page is a
    # free product's page: one plan, every system, no price for anything anybody
    # cannot buy. It used to keep advertising the tiers — prices, feature lists,
    # a "most popular" badge — with the buttons just switched off, which reads as
    # a shop whose door has been left open rather than as a product that is free.
    checkout_pro = plans.checkout_url(plans.PRO)
    checkout_enterprise = plans.checkout_url(plans.ENTERPRISE)
    return render_template(
        "pricing.html",
        bot_name=name,
        bot_initial=(name[:1] or "M").upper(),
        **_invite_context(),
        enter_url="/login",
        enter_label="Sign in",
        system_count=features.system_count(),
        features=features.dashboard_modules(),
        selling=plans.selling_enabled(),
        kofi_url=KOFI_URL,
        price_free=plans.FREE.price,
        price_pro=plans.PRO.price,
        price_enterprise=plans.ENTERPRISE.price,
        checkout_pro=checkout_pro,
        checkout_enterprise=checkout_enterprise,
        checkout_ready=bool(checkout_pro or checkout_enterprise),
        plans_enforced=plans.enforcement_enabled(),
    )


# ---------------------------------------------------------------------------
# 5b. THE HUB — the public map, and the one place everything is written down
#
# The hub is a second public surface and it is deliberately **read-only**. It has
# no database, no session and no credential: it renders from the bot's own
# registry, its own changelog and its own public readings, and every page that
# would need an action behind it hands off to the console. A static host is a
# host anybody can read, so putting a write path on one would be the mistake —
# the whole design falls out of that single decision.
#
# It is served from the real app as well as published statically, and that is the
# same reasoning `scripts/build_site.py` already follows: the page a visitor
# reads on the CDN and the page this process serves are the same template with
# the same context, so the two cannot drift into disagreeing about the product.
# ---------------------------------------------------------------------------

#: The hub's own assets. Separate from `static/` on purpose: the marketing site
#: copies the whole shared directory to its host, and the hub has no use for an
#: 80 KB stylesheet belonging to another surface.
HUB_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub", "static")
#: The project's own changelog, read as the hub's "what changed" page.
HUB_CHANGELOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CHANGELOG.md")
#: ``route`` -> the file `scripts/build_hub.py` turns it into. The paths are the
#: live app's own, and the builder rewrites each to a relative file, so a page's
#: address is written once in ``utils.hub.HUB_PAGES`` and both hosts agree.
HUB_PAGES_FOR_BUILD = tuple(
    (path, "index.html" if page == "overview" else f"{page}.html")
    for page, _label, path, _icon, _group, _blurb in hub.HUB_PAGES
)


@app.route("/hub/static/<path:name>")
def hub_static(name: str):
    """The hub's stylesheet and script.

    A second static directory, served rather than copied into ``static/`` so the
    two surfaces' assets stay legible as belonging to one surface or the other.
    ``send_from_directory`` refuses a path that escapes the directory, which is
    the only reason this is safe to expose at all.
    """
    return send_from_directory(HUB_STATIC_DIR, name)


def _hub_context(page: str) -> dict:
    """Everything one hub page is rendered from, computed the same way always.

    Every page gets the whole context rather than the part it draws, for the same
    reason the dashboard's seven pages do: a page that computed only its own slice
    would be seven chances for the hub to contradict itself. It is all local
    arithmetic and two cached reads, so the cost is a few milliseconds.
    """
    payload = hub.systems_payload()
    commands = hub.commands_payload()
    # The same reason the configuration counts are refused below: the tracker
    # lives in a local SQLite file, and the machine rendering a *static* build is
    # not the deployment. Reading it here would publish the builder's numbers as
    # the deployment's, which is worse than a stale figure — it is a wrong one,
    # and this page has no way to notice.
    static_copy = os.environ.get("MEOW_STATIC_SITE", "").strip() == "1"
    reading = _public_figures(read_tracker=not static_copy)
    started, connected = _gateway_state()
    guard = site_guard.health_json()
    uptime = uptime_status.health_json()

    # ``MEOW_STATIC_SITE`` means "this copy has no process behind it", which is
    # exactly the reason the hub cannot read its own environment: the build runs
    # on a machine that is not the deployment, so its `os.environ` is not the
    # deployment's. Reporting `1 of 50 settings present` from a CI runner would
    # not be a stale reading — it would be a wrong one, printed confidently on a
    # page with nothing to notice it by. The counts come back absent and the live
    # read fills them in from the host itself.
    config = hub.config_payload(readable=not static_copy)
    entries, changes_total = hub.read_changelog(HUB_CHANGELOG, limit=hub.CHANGES_SHOWN)

    # Computed once and used twice: the list the overview draws, and the count of
    # how many of those lines actually want action. Calling it twice would be two
    # chances to disagree about the same page. (It is a pure function of its
    # arguments, so they could not have — which is exactly the kind of "could
    # not" that stops being true when somebody adds a read to it.)
    watch = _hub_watch(payload, config, guard, uptime, connected)

    nav = [
        {
            "id": pid, "label": label, "href": path, "icon": icon,
            "group": group, "count": "" 
        }
        for pid, label, path, icon, group, _blurb in hub.HUB_PAGES
    ]
    # The rail shows how many of each there are, because a count is the one piece
    # of context that makes a nav item worth clicking.
    for item in nav:
        if item["id"] == "systems":
            item["count"] = str(payload["counts"]["systems"])
        elif item["id"] == "commands":
            # The *same* figure the page's own heading quotes. The rail showing
            # the gated-root count (83) beside a page headed "90 commands" is the
            # "two renderers, two numbers" shape this project keeps having to fix.
            item["count"] = str(payload["counts"]["commands"])

    return {
        "hub_page": page,
        # The `?v=` cache-buster on the hub's own assets. Computed from the files
        # rather than written into the template, because the static host caches
        # them immutably for a year and a buster that does not change is a
        # stylesheet that never updates.
        "hub_assets": hub.asset_version(),
        "hub_title": hub.HUB_PAGE_TITLES[page],
        "hub_kicker": HUB_KICKERS[page],
        "hub_lede": hub.HUB_PAGE_LEDES[page],
        "hub_nav": nav,
        "gateway_connected": connected,
        "gateway_started": started,
        "site": {
            "marketing_url": hub.marketing_url(),
            "live_url": hub.live_url(),
            **_invite_context(),
        },
        "systems": payload["systems"],
        "system_groups": payload["groups"],
        "fixed_systems": payload["fixed"],
        "counts": payload["counts"],
        "registry_problems": payload["problems"],
        "core_cogs": payload["core_cogs"],
        "shared_commands": commands["shared"],
        "always_allowed": commands["always_allowed"],
        "dm_allowed": commands["dm_allowed"],
        "figures": reading,
        "periods": reading["periods"],
        "config": config,
        "claims": _hub_claims(started, connected, guard, uptime, config),
        "watch": watch,
        "hosts": hub.HOSTS,
        "topology": hub.topology(connected),
        "pipeline": hub.pipeline(),
        "endpoints": hub.endpoints(),
        "trust_links": hub.trust_links(),
        "changes": entries,
        "changes_total": changes_total,
        "watch_attention": len([
            item for item in watch if item["tone"] in ("warn", "bad")
        ]),
        "canonical_url": "",
    }


#: The word above each page's title, so the header says what *kind* of page it is
#: before it says which one.
HUB_KICKERS = {
    "overview": "The whole thing at a glance",
    "systems": "The catalogue",
    "commands": "The reference",
    "operations": "Readings, and what each one means",
    "deployment": "Where it runs and why",
    "changes": "The project's own history",
    "about": "What this is, and what it will not do",
}


def _hub_claims(started, connected, guard, uptime, config) -> list[dict]:
    """The four separate health claims, each with its own sentence.

    Kept apart because they are genuinely four questions — ``/health`` says so at
    length, and the reason is worth repeating where a reader can see it: folding
    them into one word means a monitor that pages on "is it healthy" never fires
    for a process that is running perfectly and completely unconfigured.
    """
    if connected:
        gateway = ("ok", "connected", "The gateway is up and answering commands now.")
    elif started:
        gateway = ("warn", "not connected",
                   "It has logged in at least once in this process, but the gateway is not connected right now.")
    else:
        gateway = ("warn", "not yet",
                   "This process has not logged in yet — normal for a host that sleeps and has just woken up.")

    guard_status = guard.get("status") or "unknown"
    guard_tone = {"clean": "ok", "exposed": "bad", "unreachable": "warn"}.get(guard_status, "info")
    guard_detail = {
        "clean": "The published site answered for none of the paths the repository keeps private.",
        "exposed": "A repository path answered at the site's own address. It is caught, and the alert names it.",
        "unreachable": "The check could not reach the site, so it has learned nothing. Not the same as clean.",
        "unknown": "Not checked yet in this process.",
    }.get(guard_status, "Not checked yet in this process.")

    uptime_status_word = uptime.get("status") or "unknown"
    if uptime.get("monitors"):
        uptime_tone = "ok" if not uptime.get("down") else "warn"
        uptime_detail = (
            f"{uptime.get('up', 0)} of {uptime['monitors']} monitors answering. "
            "An outside process checks this from somewhere that is neither of the hosts."
        )
    else:
        uptime_tone = "info"
        uptime_detail = (
            "No monitor reading to show. The watcher is configured in the host's "
            "environment, not here, so this says nothing about whether the bot is up."
        )

    return [
        {
            "title": "Is the bot up?",
            "tone": gateway[0], "badge": gateway[1], "detail": gateway[2],
            "live": "gateway",
            "answer": f"gateway: {'connected' if connected else 'not connected'}",
        },
        {
            "title": "Is it configured?",
            "tone": ("ok" if config["attention"] == 0
                     else "info" if config["attention"] is None else "warn"),
            "badge": ("complete" if config["attention"] == 0
                      else "read live" if config["attention"] is None
                      else f"{config['attention']} to act on"),
            "detail": (
                "How many settings would stop something working if left absent. "
                "This one has to be read from the host itself — the machine that "
                "built this page has a different environment."
            ),
            "live": "config",
            "answer": ("read from this host" if config["readable"]
                       else "the live read fills this in"),
        },
        {
            "title": "Is the public site publishing safely?",
            "tone": guard_tone, "badge": guard_status, "detail": guard_detail,
            "live": "site",
            "answer": (f"{guard.get('probes', 0)} paths probed, "
                       f"{guard.get('exposed', 0)} readable"
                       if guard.get("probes") is not None else "no check recorded yet"),
        },
        {
            "title": "Has it been reliable?",
            "tone": uptime_tone,
            "badge": (f"{uptime.get('uptime_30d'):.1%}" if uptime.get("uptime_30d") is not None
                      else uptime_status_word),
            "detail": uptime_detail,
            "live": "",
            "answer": (f"last checked {uptime['checked_seconds_ago']}s ago"
                       if uptime.get("checked_seconds_ago") is not None
                       else "no reading recorded yet"),
        },
    ]


def _hub_watch(payload, config, guard, uptime, connected) -> list[dict]:
    """The lines the overview shows under "needs a look".

    Each line is a question the deployment can answer about itself, and each one
    is written so that a green reading and an amber one are both statements — a
    page that only ever lists problems is a page that goes unread the moment
    nothing is wrong, and then goes unread when something is.
    """
    if connected:
        gateway = ("ok", "Connected", "The gateway is answering commands now.", "", "") 
    else:
        gateway = (
            "warn", "Starting up",
            "The bot is not connected to Discord as of this page's reading. A free "
            "host sleeps when nobody knocks, so this is usually the first reading "
            "after a wake rather than an outage.",
            "/dashboard/status", "The console",
        )

    if config["attention"] is None:
        settings = (
            "info", "Read live",
            "This page was built somewhere other than the deployment, so it asks "
            "the host itself for how many settings are missing.",
            "", "",
        )
    elif config["attention"]:
        settings = (
            "warn", f"{config['attention']} to act on",
            f"{config['missing']} of {config['total']} settings are unset, and "
            f"{config['attention']} of those would stop something working. The "
            "named list is behind the console's sign-in.",
            "/dashboard/status", "See which",
        )
    else:
        settings = (
            "ok", "Complete",
            f"Every setting that matters is present, out of {config['total']} the "
            "code knows about.",
            "", "",
        )

    guard_status = guard.get("status") or "unknown"
    if guard_status == "clean":
        site = (
            "ok", "Clean",
            "The published site answered for none of the paths the repository keeps "
            "private. This is the check that caught the source tree being served "
            "from the site's own address.",
            "", "",
        )
    elif guard_status == "exposed":
        site = (
            "bad", "Exposed",
            "A repository path answered at the site's own address. The build has "
            "been told to publish only the pages it renders, and the alert names "
            "the path.",
            "/hub/operations", "What is checked",
        )
    else:
        site = (
            "info", guard_status,
            "No verdict recorded in this process. " + (
                "The site did not answer, so the check learned nothing — which is "
                "not the same as a clean result."
                if guard_status == "unreachable" else
                "A fresh process has not run the check yet."
            ),
            "/dashboard/status", "The console",
        )

    if payload["problems"]:
        registry = (
            "bad", f"{len(payload['problems'])} problem"
            + ("" if len(payload["problems"]) == 1 else "s"),
            "The registry that describes every system failed its own consistency "
            "check. The bot refuses to start on this, so seeing it here means "
            "something changed after boot.",
            "/hub/systems", "The systems",
        )
    else:
        registry = (
            "ok", "Consistent",
            f"All {payload['counts']['systems']} systems validate: no duplicate ids, "
            "no command claimed twice, no gate naming a system that does not exist.",
            "/hub/systems", "The systems",
        )

    reading = uptime.get("checked_seconds_ago")
    if reading is None:
        reliable = (
            "info", "No reading",
            "Nothing has been read from the watcher in this process yet.",
            "/hub/operations", "The readings",
        )
    elif uptime.get("down"):
        reliable = (
            "warn", f"{uptime['down']} down",
            f"{uptime.get('up', 0)} of {uptime.get('monitors', 0)} monitors are "
            "answering. An outside process watches this, so a reading here is a "
            "fact from somewhere else.",
            "/hub/operations", "The readings",
        )
    else:
        reliable = (
            "ok", "All answering",
            f"All {uptime.get('monitors', 0)} monitors are up, read {reading}s ago "
            "by a process that is neither of the hosts.",
            "/hub/operations", "The readings",
        )

    return [
        {
            "title": "The gateway",
            "detail": gateway[2], "tone": gateway[0], "badge": gateway[1],
            "href": gateway[3], "action": gateway[4], "live": "gateway",
        },
        {
            "title": "Configuration",
            "detail": settings[2], "tone": settings[0], "badge": settings[1],
            "href": settings[3], "action": settings[4], "live": "config",
        },
        {
            "title": "The public site's own check",
            "detail": site[2], "tone": site[0], "badge": site[1],
            "href": site[3], "action": site[4], "live": "site",
        },
        {
            "title": "The feature registry",
            "detail": registry[2], "tone": registry[0], "badge": registry[1],
            "href": registry[3], "action": registry[4], "live": "",
        },
        {
            "title": "Reliability",
            "detail": reliable[2], "tone": reliable[0], "badge": reliable[1],
            "href": reliable[3], "action": reliable[4], "live": "",
        },
    ]


@app.route("/hub")
@app.route("/hub/<page>")
def hub_page(page: str = "overview"):
    """One page of the hub.

    The page table lives in ``utils.hub`` and the builder imports it, so an
    address is written once: this route, the rail, the command palette and the
    static build all read the same tuple.
    """
    if page not in hub.HUB_PAGE_IDS:
        return redirect("/hub")
    return render_template(f"hub/{page}.html", **_hub_context(page))


@app.route("/hub/index.json")
def hub_search_index():
    """The command palette's index, built from the same data the pages render.

    Served rather than inlined because the hub ships **no inline script**: the
    policy on this host has no `unsafe-inline`, so a `<script type="application/\njson">` block would be a blocked request and a palette that never opens.
    """
    return jsonify({
        "entries": hub.search_index(hub.systems_payload(), hub.commands_payload())
    })


@app.route("/api/status")
def api_status():
    """Every figure the hub draws, merged into one small public payload.

    A merge of readings that are already public, and it adds no exposure of its
    own: ``/health``'s four blocks and ``/api/figures``' tracker totals, both of
    which are unauthenticated already and both of which answer in counts rather
    than names. The reason it exists is the same reason ``/api/figures`` does —
    the hub is a *static* page, so the numbers baked into it are the numbers of
    the day it was built, and this is the other half that lets it keep up
    without a rebuild.

    No CORS header, unlike ``/api/figures``: that one is called by a browser on
    the marketing site, while this is called either same-origin (when the hub is
    served by this process) or from the hub's own worker on the CDN, which is a
    server-side fetch and does not care about origins.
    """
    started, connected = _gateway_state()
    reading = _public_figures()
    weekly = next((row for row in reading["periods"] if row["id"] == "week"), None)
    # The configuration block is ``hub.config_payload()``'s own shape with the
    # *names* left out, and it is shaped here rather than by the caller because
    # the page reads it by key: a block that named the same numbers differently
    # would fill nothing and say nothing about why. The per-kind counts are what
    # the deployment page's table draws, and ``unset``/``attention`` are the two
    # the overview quotes.
    config = hub.config_payload()
    return jsonify({
        "ok": True,
        "at": reading["at"],
        "gateway": {
            "connected": bool(connected),
            "started": bool(started),
            "label": _gateway_label(started, connected),
        },
        "figures": reading,
        "period": weekly or {},
        "config": {
            "total": config["total"],
            "unset": config["missing"],
            "attention": config["attention"],
            "rows": [
                {"label": row["label"], "total": row["total"], "missing": row["missing"]}
                for row in config["rows"]
            ],
        },
        "site_guard": site_guard.health_json(),
        "uptime": uptime_status.health_json(),
    })


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
        return _login_page(
            "That sign-in link has expired or already been used. "
            "Tap ⚡ Open Dashboard on /help in Discord for a new one."
        )

    if request.method == "POST":
        if not validate_csrf():
            return _login_page(
                "Your session expired. Please refresh and try again."
            ), 403

        ip = _client_ip()
        if not _login_allowed(ip):
            return _login_page(
                "Too many failed attempts. Wait a few minutes and try again, "
                "sign in with Discord, or tap ⚡ Open Dashboard on /help in Discord."
            ), 429

        if not ADMIN_PASSKEY:
            _record_login_failure(ip)
            return _login_page(
                "Passkey login is disabled. Sign in with Discord, or tap "
                "⚡ Open Dashboard on /help in Discord for a one-time link."
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
        return _login_page("Invalid admin passkey. Please try again."), 401

    if is_authenticated():
        return redirect("/dashboard")
    return _login_page()


@app.route("/logout", methods=["GET", "POST"])
def logout():
    # Only a CSRF-validated POST actually signs out; a bare GET link (which any
    # site could trigger) is treated as a no-op.
    if request.method == "POST" and validate_csrf():
        session.clear()
        return redirect("/login")
    return redirect("/dashboard" if is_authenticated() else "/login")


# ---------------------------------------------------------------------------
# Signing in as yourself
# ---------------------------------------------------------------------------
# The two ways in above belong to the operator: a one-time link is scoped to the
# one server it was minted in, and the passkey is the whole bot. Neither can
# answer the question a member actually has — *which of my servers is this bot
# in, and which of those may I manage?* — because the answer depends on the
# person, not on a server. Discord sign-in answers it in one redirect: who you
# are, which servers you see, and whether each of them gives you Manage Server.
# The session then carries that list, and every server-scoped route is checked
# against it (see ``_resolve_guild_id``), so a hand-edited cookie cannot reach a
# server the person does not administer.


@app.route("/login/discord")
def login_discord():
    """Send somebody to Discord to prove who they are."""
    if not discord_auth.enabled():
        return _login_page(_oauth_setup_hint(), 503)
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    session["oauth_next"] = _safe_next(request.args.get("next"))
    return redirect(discord_auth.authorize_url(oauth_redirect_uri(), state))


@app.route("/login/discord/callback")
def login_discord_callback():
    """Discord sends the person back here, and the code is traded for who they are."""
    if not discord_auth.enabled():
        return _login_page(_oauth_setup_hint(), 503)

    # Pressing "Cancel" on Discord's own screen is a normal thing to do, and
    # Discord reports it through the same two parameters as a real refusal.
    refused = request.args.get("error_description") or request.args.get("error")
    if refused:
        return _login_page(f"Discord sign-in was not completed ({str(refused)[:200]}).")

    # Single-use and compared in constant time. Without this the callback is a
    # URL anybody can make a browser follow, so a code issued against somebody
    # else's state would be accepted here.
    expected = session.pop("oauth_state", None)
    given = request.args.get("state") or ""
    if not expected or not given or not secrets.compare_digest(str(expected), str(given)):
        print("[DASHBOARD] Refused a Discord callback whose state did not match.", flush=True)
        return _login_page("That sign-in did not come from this dashboard. Start again."), 400

    code = (request.args.get("code") or "").strip()
    if not code:
        return _login_page("Discord did not return a sign-in code. Start again."), 400

    next_path = _safe_next(session.get("oauth_next"))
    try:
        token = discord_auth.exchange_code(code, oauth_redirect_uri())
        user = discord_auth.fetch_user(token)
        guilds = discord_auth.fetch_user_guilds(token)
    except discord_auth.AuthError as exc:
        print(f"[DASHBOARD] Discord sign-in failed: {exc.reason}", flush=True)
        return _login_page(exc.reason)

    servers = discord_auth.servers_for(
        guilds, user.get("id"), [g["id"] for g in _known_guilds()]
    )
    session.clear()
    # Ids only — see ``_visible_guilds`` for why the names are not kept here.
    session["authenticated"] = True
    session.permanent = True
    session["auth_scope"] = "user"
    session["user_id"] = _int_or_none(user.get("id"))
    user_avatar = discord_auth.avatar_url(user)
    session["discord_user"] = {
        "id": str(user.get("id") or ""),
        "name": discord_auth.display_name(user),
        # What the page shows: our own address, because the dashboard's CSP
        # allows images from this origin and no other host. A Discord CDN URL
        # here was silently blocked, and a blocked image is a broken image.
        "avatar": "/api/image?kind=me" if user_avatar else "",
        # What that address fetches. Never rendered — the templates above only
        # ever see ``avatar``, so a CDN address cannot leak back into the page.
        "avatar_cdn": user_avatar,
    }
    session["admin_guild_ids"] = [int(entry["id"]) for entry in servers]
    get_csrf_token()
    print(
        f"[DASHBOARD] {session['discord_user']['name']} signed in — {len(servers)} "
        "server(s) they administer that this bot is in.",
        flush=True,
    )
    if len(servers) == 1:
        session["guild_id"] = int(servers[0]["id"])
    if len(servers) == 1 or next_path:
        return redirect(next_path or "/dashboard")
    return redirect("/servers")


def _module_summary(guild_id) -> dict:
    """What one server is currently running, so the picker can say which to fix.

    Counted from the same cache the dashboard reads, so the list and the page it
    opens cannot disagree about whether a system is on — and a server showing
    "2 of 11 on" is exactly the one an operator is looking for after a deploy
    that went wrong somewhere.
    """
    total = len(AVAILABLE_MODULES)
    try:
        states = modules.get_states(int(guild_id))
    except Exception as exc:
        print(f"[DASHBOARD] could not read module state for {guild_id}: {exc}", flush=True)
        return {"known": False, "on": 0, "total": total, "off": []}
    off = [
        module["name"]
        for module in AVAILABLE_MODULES
        if not states.get(module["id"], features.default_enabled(module["id"]))
    ]
    return {"known": True, "on": total - len(off), "total": total, "off": off}


def _servers_page(error=None, status: int | None = None):
    """The servers this session may manage, and how to add the bot to another."""
    operator = session.get("auth_scope") == "global"
    servers = [
        {**entry, "icon": _guild_icon(entry["id"])} for entry in _visible_guilds()
    ]
    if operator:
        # The operator's reach is every server the bot is in, so the list says
        # what each of them is currently running. Choosing which server to open
        # is otherwise a guess from a list of names.
        for entry in servers:
            entry["scope"] = _module_summary(entry["id"])
    name = _bot_display_name()
    body = render_template(
        "servers.html",
        servers=servers,
        error=error,
        operator=operator,
        bot_name=name,
        bot_initial=(name[:1] or "M").upper(),
        user=session.get("discord_user") or {},
        # The invite is offered even with nothing to pick: a person whose
        # servers do not have the bot yet is exactly who needs this page.
        **_invite_context(),
        current_guild_id=_int_or_none(session.get("guild_id")),
        csrf_token=get_csrf_token(),
    )
    return (body, status) if status else body


@app.route("/servers", methods=["GET", "POST"])
def servers():
    """Pick which server the dashboard is looking at."""
    if not is_authenticated():
        return redirect("/login")
    if session.get("auth_scope") == "guild":
        # A one-time link is scoped to exactly one server, so there is nothing to
        # pick. The operator's passkey reaches every server and wants this page
        # most of all: it is how a second server is fixed without first knowing
        # its id.
        return redirect("/dashboard")
    if request.method == "POST":
        if not validate_csrf():
            return _servers_page("Your session expired. Refresh and try again."), 403
        gid = _int_or_none(request.form.get("guild_id"))
        allowed = {g["id"] for g in _visible_guilds()}
        if gid is None or gid not in allowed:
            # The id came from a form, so it is a request and not a fact.
            return _servers_page("You do not manage that server."), 403
        session["guild_id"] = gid
        return redirect(f"/dashboard?guild_id={gid}")
    return _servers_page()


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
        # NOTE: this does *not* cancel the coroutine. A timeout therefore means
        # "unknown", never "did not happen" — callers must treat it that way
        # (see api_send_message).
        return future.result(timeout=timeout)
    if loop is not None:
        # A real loop object that exists but is not running: the bot is built and
        # not yet started (or has stopped). A second loop would give the cache a
        # disagreeing twin, and market's in-flight refresh guard is a module global
        # a second loop can re-enter — so this refuses instead of guessing.
        raise RuntimeError("The bot's event loop is not running — refusing to touch Discord.")
    # No loop at all is the stand-in harness's documented case (see
    # tests/test_composer.py), where there is no gateway state to disagree with.
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


#: The dashboard's pages: ``(id, label, path, anchor)``, in the order the rail
#: offers them. The dashboard was one page with a scroll spy, which is a shape
#: that can only ever *add*: the composer, the queue, the identity form and the
#: command list are four jobs, and the browser can only hold one of them at a
#: time as a scroll position. They are four addresses now — which is also what
#: makes the rail a place rather than a pointer, and what lets the palette jump
#: to a control that is on another page.
#:
#: The ids are what the template switches on and what the palette's cross-page
#: actions are built from; ``anchor`` is the one section that page renders.
DASHBOARD_PAGES = (
    ("overview", "Overview", "/dashboard", "overview"),
    ("systems", "Systems", "/dashboard/systems", "modules"),
    ("composer", "Composer", "/dashboard/composer", "composer"),
    ("schedule", "Scheduled", "/dashboard/schedule", "schedule"),
    ("identity", "Identity", "/dashboard/identity", "identity"),
    ("commands", "Commands", "/dashboard/commands", "command-scope"),
    ("status", "Status", "/dashboard/status", "status"),
)

#: ``/dashboard`` and its six siblings, keyed by path.
DASHBOARD_PAGE_BY_PATH = {path: page for page, _label, path, _anchor in DASHBOARD_PAGES}


@app.route("/dashboard")
@app.route("/dashboard/systems")
@app.route("/dashboard/composer")
@app.route("/dashboard/schedule")
@app.route("/dashboard/identity")
@app.route("/dashboard/commands")
@app.route("/dashboard/status")
def dashboard():
    """One page of one server's dashboard.

    Everything below is unchanged from when this was a single scroll; what
    changed is which of the seven sections the renderer is handed. The scope
    rules, the two readings of Discord's command list, the identity state and
    the composer's rulebook are all computed on every page, because a page that
    computed only what it draws would be seven chances to disagree with the
    others about the same server.
    """
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

    # The remembered name first, so the page is never headed "Unknown server"
    # for a server the bot is actually in. Discord's cache then overwrites it
    # when it has an answer, and that answer is written back down below.
    remembered = guild_directory.read(guild_id)
    guild_name = remembered["name"] if remembered else "Unknown server"
    member_count = remembered["member_count"] if remembered else 0
    bot_ping = _bot_ping_ms()
    bot_ready = bool(BOT_INSTANCE and getattr(BOT_INSTANCE, "_has_started", False))

    if BOT_INSTANCE:
        guild = BOT_INSTANCE.get_guild(guild_id)
        if guild:
            guild_name = guild.name
            member_count = guild.member_count or len(guild.members)
            # Write-through and idempotent: ``remember`` skips the row when
            # nothing changed, so a page load is a write only the first time a
            # server is seen or after a rename — which is what keeps the
            # directory current without a page becoming a write path.
            guild_directory.remember(
                guild_id,
                guild.name,
                # The displayed count and the stored one are asked separately:
                # ``len(guild.members)`` is 0 rather than unknown when members
                # are not chunked, and ``remember`` reads ``None`` as "Discord
                # did not say" and keeps what it already has.
                guild.member_count if guild.member_count is not None
                else (len(guild.members) or None),
            )

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

    # What this server's own command list looks like — computed from the same
    # rule the publisher uses, so the panel and the picker cannot disagree.
    tree_names = _command_tree_names()
    try:
        commands_data = command_scope.summarise(tree_names, states)
    except Exception as exc:
        print(f"[DASHBOARD] ⚠️ Could not summarise the command scope: {exc}", flush=True)
        commands_data = {"total": 0, "available": 0, "hidden": 0, "hidden_names": [], "modules": {}}

    # The other half of that panel, and a different claim: not what this server
    # asked for, but what Discord came back with the last time the bot published
    # it. Server-rendered rather than fetched, so the page tells the truth about
    # Discord before any script of its own has run — and so the line does not
    # change meaning depending on which of the two renderers painted it.
    #
    # ``guild_id`` is handed over as it is, including the ``0`` this route uses
    # for "no server": ``None`` means Discord's **global** lane, and mapping the
    # sentinel onto it handed an unscoped page the shared lane's reading — two
    # DM-safe commands — and printed it as a disagreement about a server the page
    # is not even showing ("Discord holds 2 — 2 more than this server's switches
    # say it should", under a heading whose own figures were the defaults). A
    # scope nobody has published has no reading, which is what ``0`` answers.
    publish_data = command_publish.describe(
        guild_id, expected=commands_data.get("available")
    )

    identity_data = _identity_state(guild_id) if guild_id else {
        "nick": "", "bio": "", "has_avatar": False, "has_banner": False,
        "updated_at": None,
    }
    identity_data.pop("updated_at", None)
    guild_obj = BOT_INSTANCE.get_guild(guild_id) if (BOT_INSTANCE and guild_id) else None
    me = _identity_member(guild_obj)
    # The bot's own global avatar: cached at boot so the template can show
    # the bot's real face instead of a letter tile when no per-server picture
    # has been uploaded. Served through /api/image rather than linked from
    # Discord, because the CSP allows this origin's images and no other host —
    # which is why the first version of this fallback showed a broken image
    # instead of the avatar it had correctly found.
    global_avatar = "/api/image?kind=bot" if _BOT_AVATAR_URL else ""

    identity_data.update({
        # The live name is what the server actually sees; the record above is
        # what was asked for, and the pair is what makes a silent failure visible.
        "live_name": (_live_name(me) if me is not None else ""),
        "live": me is not None,
        "can_edit": bool(guild_obj is not None and _may_edit_identity(guild_obj)),
        "max_nick": identity.MAX_NICK,
        "max_bio": identity.MAX_BIO,
        "global_avatar_url": global_avatar,
    })

    page = DASHBOARD_PAGE_BY_PATH.get(request.path, "overview")
    # Every page of one server, with this session's server carried across: a rail
    # that dropped `guild_id` would move an administrator from one page of their
    # server to another server's page.
    guild_query = f"?guild_id={guild_id}" if guild_id else ""
    return render_template(
        "dashboard.html",
        guild_id=guild_id,
        guild_name=guild_name,
        gateway=_badge_state(),
        page=page,
        page_anchor=_page_anchor(DASHBOARD_PAGES, page),
        # The open page with the server still to be appended — what the topbar's
        # server picker needs to move between servers *without* moving between
        # pages.
        page_href=next(
            f"{path}?guild_id="
            for candidate, _label, path, _anchor in DASHBOARD_PAGES
            if candidate == page
        ),
        dashboard_pages=_page_nav(DASHBOARD_PAGES, page, guild_query),
        guild_query=guild_query,
        # …and the same list as data, for the page's own script: the palette crosses
        # pages by address, so it needs the address of every page with this
        # session's server already in it. One list, rendered twice.
        pages_json=_inline_json({
            candidate: {"url": f"{path}{guild_query}", "label": label}
            for candidate, label, path, _anchor in DASHBOARD_PAGES
        }),
        member_count=member_count,
        server_identity=identity_data,
        command_scope=commands_data,
        command_scope_publish=publish_data,
        bot_ping=bot_ping,
        bot_ready=bot_ready,
        modules=modules_data,
        active_count=active_count,
        total_count=len(AVAILABLE_MODULES),
        csrf_token=get_csrf_token(),
        # Only the servers this session may act on, so the switcher cannot even
        # offer one it would then refuse.
        guilds=_visible_guilds(),
        can_switch_guild=session.get("auth_scope") in ("global", "user"),
        signed_in_as=session.get("discord_user") or {},
        pick_server_url=(
            "/servers" if session.get("auth_scope") in ("user", "global") else ""
        ),
        # The fleet console is the operator's alone, so the link to it is only
        # offered where it would actually open. A server's administrator seeing
        # a button that bounces them back to the picker is worse than no button.
        fleet_url=("/owner" if session.get("auth_scope") == "global" else ""),
        # Whether anything is on sale. The topbar and the palette both link to
        # `/pricing`, and while the product is free that page has nothing to
        # offer — so the link is not offered either. It was: three surfaces
        # pointed at a shop page that exists only to say there is no shop.
        selling=plans.selling_enabled(),
        **_invite_context(guild_id=guild_id),
        # The composer's whole rulebook, from the same constants the endpoint
        # validates with: the character limit, the tag cap, the channel cap and
        # the title length. One owner each, so the page's counter, its button and
        # the bot cannot disagree — it used to hand over the channel cap alone,
        # which left the other three written into the template as literals, where
        # the page would go on stating the old number the day one moved here.
        composer_limits={
            "chars": MESSAGE_MAX_CHARS,
            "tags": MESSAGE_MAX_MENTIONS,
            "channels": MESSAGE_MAX_CHANNELS,
            "title": MESSAGE_TITLE_MAX,
        },
        # The scheduling controls read their limits from the store itself, so the
        # page cannot offer a repeat or a queue depth the endpoint would refuse.
        schedule_repeat_choices=[
            {"minutes": minutes, "label": label}
            for minutes, label in scheduled.REPEAT_CHOICES
        ],
        schedule_max_pending=plans.scheduled_cap(guild_id, scheduled.MAX_PENDING_PER_GUILD),
        schedule_catchup_hours=scheduled.MISS_CATCHUP_SECONDS // 3600,
        # Offered from the registry the endpoint validates against, so the page can
        # only choose a content source the bot can actually build.
        schedule_content_choices=[
            {"value": "", "label": "Message I write", "blurb": ""},
            *[
                {"value": name, "label": entry["label"], "blurb": entry["blurb"]}
                for name, entry in digest.GENERATORS.items()
            ],
        ],
        server_now=int(time.time()),
        # The Status panel, server-rendered so the page can say "up for how
        # long" the moment it opens rather than after a round trip. All three
        # are read from state this process already holds — UptimeRobot's last
        # snapshot, the site check's last verdict, the configuration audit — so
        # none of them costs a request while somebody is waiting for a page.
        system_status=uptime_status.snapshot(),
        site_guard_status=site_guard.health_json(),
        config_status=config_audit.health_json(),
    )


# ---------------------------------------------------------------------------
# The operator's console
# ---------------------------------------------------------------------------
# ``/dashboard`` answers **"what is this one server doing"**, and it is built that
# way on purpose: every control on it is scoped to a single server, because that
# is the question an administrator has. The operator has a different question —
# *which* of these servers is the one that is wrong — and answering it from the
# same page means opening them one at a time and remembering what the last one
# said. This page is that answer: every server in one list beside the number that
# makes a bad one obvious, with the infrastructure the operator owns underneath.
#
# It is also the one page that cannot be shown to a server's administrator, so
# the scope test is the page's **first** line rather than a guard per control: a
# session without the operator's passkey does not get a reduced version of this
# page, it gets sent somewhere it can actually act.


def _fleet_rows() -> list[dict]:
    """Every server the bot is in, and the two numbers that make a bad one obvious.

    The module count is read from the same cache the dashboard reads, so a server
    showing "0 of 11 on" here is genuinely the one to open — and the name is read
    from the gateway rather than the session, because the bot's nickname *is* per
    server and is the one field that cannot be looked up anywhere else.
    """
    rows = []
    for entry in _visible_guilds():
        guild = BOT_INSTANCE.get_guild(entry["id"]) if BOT_INSTANCE else None
        summary = _module_summary(entry["id"])
        members = 0
        if guild is not None:
            try:
                members = int(guild.member_count or len(guild.members) or 0)
            except Exception:
                members = 0
        rows.append({
            "id": entry["id"],
            "name": entry["name"],
            "icon": _guild_icon(entry["id"]),
            "nick": (_live_name(_identity_member(guild)) if guild is not None else ""),
            "members": members,
            "on": summary["on"],
            "total": summary["total"],
            "known": summary["known"],
            "off": summary["off"],
        })
    # Biggest first: the server a change matters most in is the one with the most
    # people in it, and that is the order an operator scans for.
    rows.sort(key=lambda row: (-row["members"], row["name"].lower()))
    return rows


def _page_nav(pages, current: str, query: str = "") -> list[dict]:
    """One surface's pages, as its rail renders them.

    A function rather than a comprehension inside each view because *three*
    renderers need this list and they must not drift: the view itself, the
    preview harness, and the test suite's own Jinja environment. A rail built
    slightly differently in one of them is a page whose navigation is only
    correct in production — the least useful direction for a difference to
    exist in.

    ``query`` is a suffix every link carries, which is how the dashboard's rail
    keeps the open server: moving from one page of a server to another page of
    the same server should not require re-choosing it.
    """
    return [
        {
            "id": spec_id,
            "label": label,
            "href": path,
            "anchor": anchor,
            "url": f"{path}{query}",
            "current": spec_id == current,
        }
        for spec_id, label, path, anchor in pages
    ]


def _page_anchor(pages, current: str) -> str:
    """The one section the named page renders — what its skip link targets."""
    return next(anchor for spec_id, _label, _path, anchor in pages if spec_id == current)


#: The fleet console's pages: ``(id, label, path, anchor)``, in the order the rail
#: offers them. One list, because the rail, the routes and the skip link have to
#: agree — a console whose pages are linked in one place and routed in another is
#: how a section ends up reachable only by typing its address. The ids are the
#: ``page`` the template switches on, the label is what the rail calls it, and the
#: anchor is the one section that page renders, which is what the skip link targets.
OWNER_PAGES = (
    ("overview", "Overview", "/owner", "overview"),
    ("servers", "Servers", "/owner/servers", "servers"),
    ("systems", "Systems", "/owner/systems", "systems"),
    ("ops", "Ops", "/owner/ops", "ops"),
    ("changes", "Changes", "/owner/changes", "changes"),
)

#: Where the bundled console app is built to (``console/`` builds into it).
CONSOLE_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "console")


def _console_asset_stamp() -> str:
    """A cache-buster for the console's two files, read from disk.

    The bundle is written to stable filenames on purpose (see ``console/vite.config.ts``),
    so the only thing that can tell a browser a new build exists is the file's
    own modification time. A missing file yields ``"0"``, which is the honest
    answer and keeps the shell renderable on a checkout that has never built.
    """
    try:
        return str(int(os.path.getmtime(os.path.join(CONSOLE_DIST, "app.js"))))
    except OSError:
        return "0"


def _console_built() -> bool:
    """Whether the app has been built. If not, the shell says so."""
    return os.path.exists(os.path.join(CONSOLE_DIST, "app.js"))

@app.route("/owner")
@app.route("/owner/<path:sub>")
def owner_console(sub: str = ""):
    """The operator console's shell. Operator only — see the note above.

    This served four server-rendered pages. It now serves one HTML file: the
    shell for the bundled app in ``console/``, which owns every ``/owner`` path
    and draws its content from the ``/api/console/*`` endpoints below.

    Two things stay here rather than moving into the bundle, and both are the
    same reason: the server is the only party that knows them. The **CSRF token**
    is minted against this session, and Flask's session cookie is ``HttpOnly``,
    so a script could never read either — it has to be handed the token. And the
    **scope test** is the page's first line, unchanged: a one-time link is a real
    sign-in that belongs to one server, so it is sent to a place it can act
    instead of being given a fleet it may not touch.
    """
    if not is_authenticated():
        return redirect("/login")
    if session.get("auth_scope") != "global":
        # A Discord sign-in goes to the picker rather than to a dashboard, so
        # nobody is dropped onto a server they did not choose.
        return redirect("/servers" if _visible_guilds() else "/dashboard")

    return render_template(
        "console.html",
        csrf_token=get_csrf_token(),
        operator=session.get("discord_user") or {},
        bot_name=_bot_display_name(),
        asset_stamp=_console_asset_stamp(),
        built=_console_built(),
        gateway=_badge_state(),
        pages=_page_nav(OWNER_PAGES, "overview"),
    )


# ---------------------------------------------------------------------------
# The console's own API.
#
# Every route below opens with the same gate, and it is deliberately *not*
# ``_require_auth_api``. That one answers "is somebody signed in", which is the
# right question for a single server's dashboard and the wrong one here: this
# surface is the whole fleet, so a one-time link — a genuine sign-in that belongs
# to one server — must be refused. It is refused by scope, which is the same line
# ``/owner`` has always drawn on its first line, applied to the data behind it.
# ---------------------------------------------------------------------------


def _require_operator_api():
    if not is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    if session.get("auth_scope") != "global":
        return jsonify({"success": False, "error": "Operator sign-in required"}), 403
    if request.method != "GET" and not validate_csrf():
        return jsonify({"success": False, "error": "Invalid or missing CSRF token"}), 403
    return None


def _console_totals(fleet: list[dict]) -> dict:
    """The fleet's own sums, beside the two counts the registry owns.

    ``systems`` and ``commands`` come from ``features`` rather than from any
    running process, for the reason that module documents: a tree measured inside
    a live bot is a fact about that process, and this is a claim about the
    deployment. The *published* count is read live and reported separately,
    because a server with systems off genuinely publishes fewer.
    """
    return {
        "servers": len(fleet),
        "members": sum(row["members"] for row in fleet),
        "on": sum(row["on"] for row in fleet),
        "slots": sum(row["total"] for row in fleet),
        "systems": features.system_count(),
        "switchable": len(AVAILABLE_MODULES),
        "commands": features.command_count(),
        "published": len(_command_tree_names()),
    }


def _console_systems_for(guild_id: int) -> tuple[list[dict], bool]:
    """One server's systems with their switches, and whether the read worked.

    ``known`` is reported rather than swallowed: a page that showed every switch
    as *off* because the state could not be read would be telling the operator to
    fix eleven systems that are running.
    """
    try:
        states = modules.get_states(int(guild_id))
    except Exception as exc:
        print(f"[CONSOLE] could not read module state for {guild_id}: {exc}", flush=True)
        states, known = {}, False
    else:
        known = True
    return (
        [
            {
                "id": module["id"],
                "name": module["name"],
                "icon": module["icon"],
                "category": module["category"],
                "description": module["desc"],
                "commands": module["commands"],
                "enabled": bool(
                    states.get(module["id"], features.default_enabled(module["id"]))
                ),
                "allowed": bool(plans.feature_allowed(int(guild_id), module["id"])),
            }
            for module in AVAILABLE_MODULES
        ],
        known,
    )


@app.route("/api/console/overview")
def api_console_overview():
    """Everything the console's front page draws, in one request.

    One call rather than five because this page is the *first* thing an operator
    opens, usually on a host that has been asleep: five round trips to a waking
    free-tier instance is five chances to sit in front of a spinner.
    """
    denied = _require_operator_api()
    if denied:
        return denied

    fleet = _fleet_rows()
    figures = _public_figures()
    started, connected = _gateway_state()
    periods = [
        {"id": window["id"], "label": window["label"]}
        for window in (figures.get("periods") or [])
    ]
    return jsonify({
        "success": True,
        "totals": _console_totals(fleet),
        "gateway": {
            **_badge_state(),
            "started": started,
            "key": _gateway_label(started, connected),
        },
        "bot": {
            "name": _bot_display_name(),
            "ready": _bot_ready(),
            "ping_ms": _bot_ping_ms(),
        },
        "uptime_seconds": max(0, int(time.time() - PROCESS_STARTED_AT)),
        "selling": plans.selling_enabled(),
        # Every window ships at once: switching between them is then a redraw
        # rather than a request, so a period control cannot stall on the host.
        "figures": {
            "periods": periods,
            "tiles": {
                window["id"]: _activity_tiles(figures, window["id"])
                for window in periods
            },
        },
        "health": {
            "uptime": uptime_status.snapshot(),
            "site": site_guard.health_json(),
            # `health_json` counts and does not name, because `/health` is
            # public. These payloads are behind `_require_operator_api`, so the
            # names come with it: a card that says "attention" and stops is a
            # card that sends its reader somewhere else to find out what about,
            # and the operator reading it is the one person who can set them.
            # Presence only — `attention_json` never reads a value.
            "config": {**config_audit.health_json(), "named": config_audit.attention_json()},
        },
        "operator": session.get("discord_user") or {},
    })


@app.route("/api/console/fleet")
def api_console_fleet():
    """Every server the bot is in, ordered by how many people are in it."""
    denied = _require_operator_api()
    if denied:
        return denied
    fleet = _fleet_rows()
    return jsonify({"success": True, "servers": fleet, "totals": _console_totals(fleet)})


@app.route("/api/console/server/<int:guild_id>")
def api_console_server(guild_id: int):
    """One server: its row from the fleet list, plus every switch it owns.

    The name, icon and member count are taken from the *same* rows the list is
    drawn from, so the page an operator opens cannot disagree with the row they
    clicked to open it.
    """
    denied = _require_operator_api()
    if denied:
        return denied

    row = next((entry for entry in _fleet_rows() if entry["id"] == int(guild_id)), None)
    if row is None:
        return jsonify({"success": False, "error": "No such server"}), 404

    systems, known = _console_systems_for(guild_id)
    return jsonify({
        "success": True,
        "server": {**row, "known": known},
        "systems": systems,
        # The difference between the registry's total and its switchable count, by
        # name — printing two numbers about one word without naming the gap is the
        # defect ``features.always_on_names`` exists to stop.
        "always_on": list(features.always_on_names()),
        "publish_at": _last_publish_at(guild_id),
    })


@app.route("/api/console/systems")
def api_console_systems():
    """The whole fleet as a matrix: every system, and where it stands."""
    denied = _require_operator_api()
    if denied:
        return denied

    fleet = _fleet_rows()
    matrix: dict[str, dict[str, bool]] = {}
    unreadable: list[int] = []
    for row in fleet:
        try:
            states = modules.get_states(row["id"])
        except Exception as exc:
            print(f"[CONSOLE] could not read module state for {row['id']}: {exc}", flush=True)
            unreadable.append(row["id"])
            continue
        matrix[str(row["id"])] = {
            module["id"]: bool(
                states.get(module["id"], features.default_enabled(module["id"]))
            )
            for module in AVAILABLE_MODULES
        }

    return jsonify({
        "success": True,
        "systems": [
            {
                "id": module["id"],
                "name": module["name"],
                "icon": module["icon"],
                "category": module["category"],
                "description": module["desc"],
                "commands": module["commands"],
            }
            for module in AVAILABLE_MODULES
        ],
        "servers": [
            {
                "id": row["id"],
                "name": row["name"],
                "icon": row["icon"],
                "members": row["members"],
            }
            for row in fleet
        ],
        "matrix": matrix,
        "unreadable": unreadable,
        "always_on": list(features.always_on_names()),
    })


@app.route("/api/console/ops")
def api_console_ops():
    """What the process is doing: its own output tail, and its own readings."""
    denied = _require_operator_api()
    if denied:
        return denied

    # The same redaction pass `/api/live-logs` applies, for the same reason: this
    # ring is written by every part of the bot, and a token that reached a log
    # line must not reach a page.
    lines = [
        _SECRETISH.sub(r"\1=[redacted]", str(line)).rstrip("\n")
        for line in list(LOG_RING)[-200:]
    ]
    return jsonify({
        "success": True,
        "logs": lines,
        "uptime_seconds": max(0, int(time.time() - PROCESS_STARTED_AT)),
        "gateway": _badge_state(),
        "ping_ms": _bot_ping_ms(),
        "health": {
            "uptime": uptime_status.snapshot(),
            "site": site_guard.health_json(),
            # `health_json` counts and does not name, because `/health` is
            # public. These payloads are behind `_require_operator_api`, so the
            # names come with it: a card that says "attention" and stops is a
            # card that sends its reader somewhere else to find out what about,
            # and the operator reading it is the one person who can set them.
            # Presence only — `attention_json` never reads a value.
            "config": {**config_audit.health_json(), "named": config_audit.attention_json()},
        },
    })


@app.route("/api/console/changes")
def api_console_changes():
    """The project's own changelog, newest first — the same reader the hub uses."""
    denied = _require_operator_api()
    if denied:
        return denied
    entries, total = hub.read_changelog(HUB_CHANGELOG, limit=hub.CHANGES_SHOWN)
    return jsonify({"success": True, "entries": entries, "total": total})


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    denied = _require_auth_api()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    refused = _foreign_guild_refusal(data.get("guild_id"))
    if refused:
        return refused
    guild_id = _resolve_guild_id(data.get("guild_id"))
    module_id = data.get("module")
    enabled = bool(data.get("enabled"))

    if guild_id is None:
        return jsonify({"success": False, "error": "No guild available"}), 400
    if module_id not in ALLOWED_MODULE_IDS:
        return jsonify({"success": False, "error": "Unknown module"}), 400

    # A system this server's plan does not include is refused in its own words.
    # ``modules.set_enabled`` refuses it too (that is what keeps every reader
    # agreeing), but it can only answer "could not save", and a switch that
    # springs back with no reason is the kind of defect this dashboard has
    # already been reported for twice.
    if enabled and not plans.feature_allowed(guild_id, module_id):
        return jsonify({
            "success": False,
            "error": (
                f"{module_id} is part of a paid plan this server does not have. "
                "Activate a licence with /bot plan to switch it on."
            ),
            "plan_required": True,
        }), 403

    session["guild_id"] = guild_id

    # Persist BEFORE updating memory. If the write fails we must not report
    # success, otherwise the toggle silently reverts on the next restart.
    if not modules.set_enabled(guild_id, module_id, enabled):
        return jsonify({"success": False, "error": "Could not save that change. Please try again."}), 500

    # Read *before* the republish is asked for, and into a local rather than
    # inside the response below: this is the stamp the page has to beat, so it
    # must be the publish from before this switch. The queueing call returns at
    # once today — the publish happens three seconds later on the bot's loop —
    # but a page whose stamp came from the publish it just asked for would wait
    # out its whole timeout and then report that Discord had never been reread.
    publish_before = _last_publish_at(guild_id)
    _resync_commands(guild_id)
    _schedule_cloud_backup()

    return jsonify({
        "success": True,
        "guild_id": guild_id,
        "module": module_id,
        "enabled": enabled,
        "sync_latency_ms": 0,
        # The publish this switch supersedes. The page uses it to tell when
        # Discord has been read again: a later stamp on the scope panel means the
        # republish this click asked for has finished. Read here rather than in
        # the page so the comparison is between two readings of one clock.
        "publish_at": publish_before,
    })


def _last_publish_at(guild_id) -> float | None:
    """When Discord's copy of this server's list was last read, or ``None``.

    Handed to the page so it can wait for the *event* rather than for a guessed
    number of seconds: the scope panel asks again until this figure advances,
    which is true the moment the republish has finished and false forever if it
    was refused. Two stamps from this one clock, compared.
    """
    record = command_publish.read(guild_id)
    return record.get("at") if record else None


def _command_tree_names() -> list[str]:
    """Every top-level command the bot has, as Discord's picker would name it."""
    tree = getattr(BOT_INSTANCE, "tree", None) if BOT_INSTANCE is not None else None
    if tree is None:
        return []
    try:
        return [command.name for command in tree.get_commands()]
    except Exception as exc:
        print(f"[DASHBOARD] ⚠️ Could not read the command tree: {exc}", flush=True)
        return []


@app.route("/api/command-scope")
def api_command_scope():
    """This server's own command list — the same rule the publisher follows.

    Read after a module switch settles rather than guessed in the browser, so
    the panel and the server's registered commands cannot drift apart.
    """
    denied = _require_auth_api()
    if denied:
        return denied
    guild_id = _resolve_guild_id(request.args.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No guild available"}), 400
    try:
        states = modules.get_states(guild_id)
    except Exception as exc:
        # The same fallback the page load uses: show the defaults rather than an
        # empty panel that reads as "this server has no commands".
        print(f"[DASHBOARD] ⚠️ Could not read module states: {exc}", flush=True)
        states = modules.defaults()
    summary = command_scope.summarise(_command_tree_names(), states)
    return jsonify({
        "success": True,
        "guild_id": guild_id,
        # What Discord actually holds, beside what this server's switches say it
        # should. The two are not the same claim, and until this was here the
        # page had no way to tell them apart: the counts above are computed from
        # the local module cache, so they read the same whether the republish had
        # landed, was still behind its settle, or had been refused outright.
        "publish": command_publish.describe(guild_id, expected=summary["available"]),
        **summary,
    })


@app.route("/api/toggle-all", methods=["POST"])
def api_toggle_all():
    denied = _require_auth_api()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    refused = _foreign_guild_refusal(data.get("guild_id"))
    if refused:
        return refused
    guild_id = _resolve_guild_id(data.get("guild_id"))
    enabled = bool(data.get("enabled"))

    if guild_id is None:
        return jsonify({"success": False, "error": "No guild available"}), 400

    session["guild_id"] = guild_id

    # "Enable all" means every system this server *may* have: offering the ones
    # its plan withholds would fail the whole bulk save over modules that are
    # not available to it at all.
    wanted = {
        m["id"]: enabled
        for m in AVAILABLE_MODULES
        if not enabled or plans.feature_allowed(guild_id, m["id"])
    }
    failed = modules.set_many(guild_id, wanted)

    if failed:
        return jsonify({
            "success": False,
            "error": f"Could not save: {', '.join(failed)}",
            "guild_id": guild_id,
            "enabled": enabled,
        }), 500

    publish_before = _last_publish_at(guild_id)
    _resync_commands(guild_id)
    _schedule_cloud_backup()

    return jsonify({
        "success": True,
        "guild_id": guild_id,
        "enabled": enabled,
        "sync_latency_ms": 0,
        "publish_at": publish_before,
    })


def _resync_commands(guild_id: int) -> bool:
    """Ask the bot to republish one server's command list.

    A module switch **is** a command-set change: the market being off here is
    what takes `/market` out of this server's picker, because the command is
    registered per server (see ``bot.py``). Nothing is waited for — ninety
    Discord writes belong on the bot's loop, and the switch is already saved.
    """
    hook = getattr(BOT_INSTANCE, "resync_commands", None) if BOT_INSTANCE else None
    if hook is None:
        return False
    try:
        return bool(hook(guild_id))
    except Exception as exc:
        print(f"[SYNC] could not queue a republish for {guild_id}: {exc}", flush=True)
        return False


# ---------------------------------------------------------------------------
# The bot's name and picture, per server
# ---------------------------------------------------------------------------
# Discord keeps a nickname, avatar, banner and bio on the bot's own **member** in
# each guild, and nothing here can reach the application profile — renaming the
# bot in one server must not rename it in another, which is the entire point of
# these being member fields instead of application ones. `/bot avatar` and
# `/bot banner` stay the operator's, and stay global.
#
# Who may do it: a one-time link is minted by the `/help` panel for an
# **administrator of one server** and is scoped to that server, so that session
# may change its own server and no other; the passkey is the operator's own
# credential, so it may do the same in any server the bot is in.

_IMAGE_MIMES = (
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
)


def _image_mime(data: bytes) -> str:
    for magic, mime in _IMAGE_MIMES:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _may_edit_identity(guild) -> bool:
    if session.get("auth_scope") == "guild":
        return _int_or_none(session.get("guild_id")) == getattr(guild, "id", None)
    if session.get("auth_scope") == "user":
        # Discord OAuth users see only the servers they administer; the identity
        # panel should work for any of them, not just the one they landed on.
        return getattr(guild, "id", None) in session.get("admin_guild_ids", [])
    return session.get("auth_scope") == "global"


def _identity_member(guild):
    """The bot's own member in one server, a cold member cache included."""
    if guild is None:
        return None
    me = getattr(guild, "me", None)
    if me is not None:
        return me
    user = getattr(BOT_INSTANCE, "user", None) if BOT_INSTANCE is not None else None
    if user is None:
        return None
    return guild.get_member(user.id)


def _live_name(member) -> str:
    """What that server actually calls the bot, as plainly as the object offers."""
    for attribute in ("display_name", "global_name", "name"):
        value = getattr(member, attribute, None)
        if value:
            return str(value)
    return ""


async def _apply_identity(guild_id: int, wants: dict, updated_by) -> None:
    """Apply a change to Discord, then record it — Discord first, on purpose.

    Recording a change Discord refused would leave the panel showing a name the
    server does not have, which is worse than an error that says so.
    """
    guild = BOT_INSTANCE.get_guild(guild_id) if BOT_INSTANCE else None
    member = _identity_member(guild)
    if member is None:
        raise RuntimeError(
            "I could not read my own member in that server, so nothing was "
            "changed — try again in a moment."
        )
    await identity.apply(member, **wants)
    current = identity.read(guild_id)
    identity.write(
        guild_id,
        # An empty string is how Discord spells "none" for both of these, and the
        # record says the same thing rather than storing a blank where the server
        # has nothing.
        nick=(wants["nick"] or None) if "nick" in wants else current["nick"],
        bio=(wants["bio"] or None) if "bio" in wants else current["bio"],
        avatar=wants.get("avatar", identity.KEEP),
        banner=wants.get("banner", identity.KEEP),
        updated_by=updated_by,
    )


def _discord_refusal(exc: BaseException) -> str:
    """What to tell an administrator when Discord turns the change down."""
    if isinstance(exc, TimeoutError):
        return "Discord did not answer in time — the change may or may not have landed. Reload the page to see."
    name = type(exc).__name__
    text = getattr(exc, "text", None) or str(exc)
    if name == "Forbidden" or getattr(exc, "code", None) == 50013:
        return (
            "Discord refused: I need the **Change Nickname** permission in that "
            "server to rename myself, and I cannot change my own picture without it "
            "either."
        )
    if getattr(exc, "code", None) == 50035:
        return f"Discord refused that image or text: {text}"
    return f"Discord refused the change: {text}"


def _identity_state(guild_id: int) -> dict:
    """What the panel shows: this server's record, minus the image bytes."""
    try:
        profile = identity.read(guild_id)
    except Exception as exc:
        print(f"[IDENTITY] ⚠️ Could not read the profile for {guild_id}: {exc}", flush=True)
        profile = dict(identity.EMPTY_PROFILE)
    return {
        "nick": profile["nick"] or "",
        "bio": profile["bio"] or "",
        "has_avatar": profile["avatar"] is not None,
        "has_banner": profile["banner"] is not None,
        "updated_at": profile["updated_at"],
    }


@app.route("/api/identity", methods=["POST"])
def api_identity():
    """Set, change or clear the bot's profile **in this server only**."""
    denied = _require_auth_api()
    if denied:
        return denied

    # Multipart, because of the two images; the text rides along in the same form
    # so one save is one request and one apply.
    form = request.form if request.form else (request.get_json(silent=True) or {})
    refused = _foreign_guild_refusal(form.get("guild_id"))
    if refused:
        return refused
    guild_id = _resolve_guild_id(form.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server available — is the bot online?"}), 400
    guild = BOT_INSTANCE.get_guild(guild_id) if BOT_INSTANCE else None
    if guild is None:
        return jsonify({
            "success": False,
            "error": "The bot is not in that server, so it has no profile there to change.",
        }), 400
    session["guild_id"] = guild_id
    if not _may_edit_identity(guild):
        return jsonify({
            "success": False,
            "error": "Only an administrator of that server (or the bot's owner) can rename it there.",
        }), 403

    wants: dict = {}
    if "nick" in form:
        wants["nick"] = (form.get("nick") or "").strip()
        problem = identity.name_problem(wants["nick"])
        if problem:
            return jsonify({"success": False, "error": problem}), 400
    if "bio" in form:
        wants["bio"] = (form.get("bio") or "").strip()
        problem = identity.bio_problem(wants["bio"])
        if problem:
            return jsonify({"success": False, "error": problem}), 400
    for field in ("avatar", "banner"):
        upload = request.files.get(field)
        if upload is not None and upload.filename:
            data = upload.read()
            problem = identity.image_problem(data)
            if problem:
                return jsonify({"success": False, "error": problem}), 400
            wants[field] = data
        elif form.get(f"clear_{field}"):
            wants[field] = None

    if not wants:
        return jsonify({"success": False, "error": "Nothing to change."}), 400

    try:
        _run_on_bot_loop(
            _apply_identity(guild_id, wants, _int_or_none(session.get("user_id"))),
            timeout=25.0,
        )
    except Exception as exc:
        print(f"[IDENTITY] ⚠️ {guild.name}: {exc}", flush=True)
        return jsonify({"success": False, "error": _discord_refusal(exc)}), 502

    _schedule_cloud_backup()
    state = _identity_state(guild_id)
    return jsonify({"success": True, "guild_id": guild_id, **state})


@app.route("/api/identity/image/<kind>")
def api_identity_image(kind: str):
    """The picture this server chose, from our own record.

    Served rather than hot-linked from Discord: the dashboard's own CSP allows
    no third-party images, and the copy the server chose is what belongs beside
    the form that sets it.
    """
    if not is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    if kind not in ("avatar", "banner"):
        return jsonify({"success": False, "error": "Unknown image"}), 404
    guild_id = _resolve_guild_id(request.args.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server available"}), 400
    try:
        data = identity.read(guild_id)[kind]
    except Exception as exc:
        print(f"[IDENTITY] ⚠️ Could not read the {kind} for {guild_id}: {exc}", flush=True)
        data = None
    if not data:
        return jsonify({"success": False, "error": "Nothing set for this server"}), 404
    response = Response(bytes(data), mimetype=_image_mime(bytes(data)))
    # Per-session and changes on save; a stale copy here is a picture the server
    # has already replaced.
    response.headers["Cache-Control"] = "private, max-age=30"
    return response


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


def _parse_channel_ids(data) -> list:
    """The channel list from a request, keeping only things that are ids.

    Shared by every path that takes channels — a typed message and a *generated*
    one — so the rules about what counts as a channel cannot differ between them.
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
    return channel_ids


def _missing_channels(guild, channel_ids) -> list:
    """Which of these ids the bot cannot reach *now*.

    "Send now" does not need to ask — a bad channel fails visibly in the next
    second — but a scheduled message would fail days later with nobody watching,
    and a channel that does not exist at queue time is a stale page or a crafted
    request rather than a typo.
    """
    return [
        cid for cid in channel_ids
        if (guild.get_channel(cid) or guild.get_thread(cid)) is None
    ]


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
    channel_ids = _parse_channel_ids(data)
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
    refused = _foreign_guild_refusal(data.get("guild_id"))
    if refused:
        return refused
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
    except TimeoutError:
        # The coroutine is still running on the bot's loop and may post a second
        # from now. The old path answered "Discord did not accept it" — a claim it
        # could not support — and skipped the history row, so the natural retry
        # posted a second copy of an announcement that had already arrived. This is
        # the one outcome nothing can resolve, so it is recorded as unknown and the
        # operator is told to look before sending again.
        scheduled.record_unknown(
            guild_id,
            channel_ids,
            payload,
            created_by=_int_or_none(session.get("user_id")),
        )
        return jsonify({
            "success": False,
            "unknown": True,
            "error": (
                "Discord did not answer in time — the message may still have been "
                "posted. Check the channels before sending it again."
            ),
        }), 504
    except Exception as e:
        print(f"[DASHBOARD] Composed message failed: {e}", flush=True)
        return jsonify({"success": False, "error": f"Discord did not accept it: {e}"}), 502

    # Filed before the answer is built, because this is the only trace an
    # immediate send leaves: the per-channel report below goes to the page and dies
    # with the tab, and "who posted @everyone to five channels yesterday?" has to
    # still have an answer then. It is a finished history row, never a pending one,
    # so the delivery loop can never pick it up and post it a second time.
    scheduled.record_sent(
        guild_id,
        channel_ids,
        payload,
        results,
        created_by=_int_or_none(session.get("user_id")),
    )

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
    # What the queue row says it will post, when it is not the text that was typed.
    view["generator_label"] = digest.label(msg.get("generator") or "")
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
        "max_pending": plans.scheduled_cap(guild_id, scheduled.MAX_PENDING_PER_GUILD),
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
    refused = _foreign_guild_refusal(data.get("guild_id"))
    if refused:
        return refused
    guild_id, guild = _composer_guild(data.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server available — is the bot online?"}), 400

    now = int(time.time())
    run_at, repeat, error = _schedule_timing(data, now)
    if error:
        return jsonify({"success": False, "error": error, "server_now": now}), 400

    channel_ids = _parse_channel_ids(data)
    if not channel_ids:
        return jsonify({"success": False, "error": "Pick at least one channel.", "server_now": now}), 400
    if len(channel_ids) > MESSAGE_MAX_CHANNELS:
        return jsonify({
            "success": False,
            "error": f"At most {MESSAGE_MAX_CHANNELS} channels per send.",
            "server_now": now,
        }), 400
    if _missing_channels(guild, channel_ids):
        return jsonify({
            "success": False,
            "error": "That channel no longer exists — reload the page and pick one again.",
            "server_now": now,
        }), 400

    generator = str(data.get("generator") or "").strip()
    if generator and generator not in digest.GENERATORS:
        return jsonify({
            "success": False,
            "error": "Unknown content source — reload the page and pick one again.",
            "server_now": now,
        }), 400

    if generator:
        # A generated message has no text to validate: its content does not exist
        # until the moment it posts. Timing, channels, the queue cap and every
        # delivery guarantee still apply, and the generator itself is only ever a
        # name from ``digest.GENERATORS`` — never a callable the browser could name.
        payload = {
            "generator": generator,
            "content": None,
            "embed": None,
            "users": [],
            "roles": [],
            "everyone": False,
            "ignored_mentions": [],
        }
        preview = f"{digest.label(generator)} — built when it posts"
    else:
        # The same builder "send now" uses, so a queued message is validated
        # exactly as an immediate one is — including resolving every tag here.
        payload, channel_ids, error = _build_message(guild, data)
        if error:
            return jsonify({"success": False, "error": error, "server_now": now}), 400
        preview = ""

    msg_id = scheduled.create(
        guild_id,
        channel_ids,
        payload,
        run_at,
        repeat_minutes=repeat,
        generator=generator,
        preview=preview,
        created_by=_int_or_none(session.get("user_id")),
        now=now,
    )
    if msg_id is None:
        cap = plans.scheduled_cap(guild_id, scheduled.MAX_PENDING_PER_GUILD)
        return jsonify({
            "success": False,
            "error": (
                f"This server already has {cap} scheduled "
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
    refused = _foreign_guild_refusal(data.get("guild_id"))
    if refused:
        return refused
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
    refused = _foreign_guild_refusal(data.get("guild_id"))
    if refused:
        return refused
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
        # A generated message with nothing to report was not "sent" and did not
        # fail: the page says what it was, rather than drawing an empty receipt.
        "skipped": bool(outcome.get("skipped")),
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

    if guild is None:
        # Final: a message for a server the bot has left can never be delivered, and
        # a weekly one must not come back every week to fail again.
        reason = "I am not in that server any more"
        scheduled.mark_failed(msg["id"], now, reason, [], final=True)
        print(f"[SCHEDULE] {scheduled.describe(msg)}: {reason}", flush=True)
        return {"sent": [], "failed": [], "results": [], "error": reason, "skipped": False}

    if msg.get("generator"):
        # The content is built *now*, on the delivery loop — which is the whole
        # point of a generated message: it did not exist when the row was written.
        built, reason = digest.build(msg["generator"], guild, now)
        if built is None:
            # A quiet week is not a failure. Recorded as a missed run, so a repeat
            # keeps its slot and its failure streak, and nothing posts a card of
            # zeros just to have posted something.
            scheduled.mark_missed(msg["id"], now, reason)
            print(f"[SCHEDULE] {scheduled.describe(msg)}: not posted — {reason}", flush=True)
            return {"sent": [], "failed": [], "results": [], "error": reason, "skipped": True}
        payload = built

    results = []
    error = ""
    try:
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
    return {"sent": sent, "failed": failed, "results": results, "error": error, "skipped": False}


# ---------------------------------------------------------------------------
# An outcome nobody is watching has to reach somebody
#
# Everything else about a scheduled message is built to be trusted: what gets
# posted is what was stored, the claim is exactly-once, a repeat keeps its own
# slot. What was missing was the audience for a failure. The panel shows it to
# whoever opens the panel — and a daily announcement that quietly stopped is
# invisible until somebody notices the silence, which may be never. So the
# outcomes where nothing arrived, or only part of it did, are posted where the
# server already looks: the guild's log channel, tagging the person who queued
# it. With no usable log channel that person is told directly instead, and only
# when neither is possible does this become a line in the bot's own log.
# ---------------------------------------------------------------------------

#: What happened, and therefore what the card looks like.
_ALERT_TONES = {
    "failed": ("⚠️", "A scheduled message did not go out", 0xE74C3C),
    "stopped": ("⛔", "A repeating message has been stopped", 0xE74C3C),
    "interrupted": ("⚠️", "A scheduled message was interrupted by a restart", 0xE74C3C),
    "partial": ("⚠️", "Part of a scheduled message was refused", 0xE67E22),
    "missed": ("⏭️", "A scheduled message was skipped", 0xE67E22),
}


def _clamp(text, limit: int = 1000) -> str:
    """One embed field's worth of text. A long reason must not reject the card."""
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: limit - 1] + "…"


def _alert_tone(msg: dict, fresh: dict) -> str:
    """The failure of a one-shot is a failure; of a repeat that gave up, a stop."""
    if msg.get("repeat_minutes") and str(fresh.get("status")) != "pending":
        return "stopped"
    return "failed"


def _alert_next_step(tone: str, msg: dict) -> str:
    """What happens to this message now — the question the card exists to answer."""
    if str(msg.get("status")) == "pending":
        when = _utc_text(msg.get("run_at"))
        streak = int(msg.get("fail_streak") or 0)
        if tone == "interrupted":
            # A repeat keeps its next slot even after an interrupted send, so
            # "queue it again" would be wrong — and the message may already be half
            # posted, which only a person looking at the channel can tell.
            line = (
                f"It is still queued and posts again {when} — cancel it if part of "
                "it already arrived."
            )
        elif tone == "missed":
            line = f"It keeps its slot and posts again {when}."
        elif streak:
            line = (
                f"It tries again {when} — failure {streak} of {scheduled.FAIL_LIMIT}, "
                "then it stops."
            )
        else:
            # A *partial* delivery counts as a success for a repeat, so it keeps its
            # slot: "failure 0 of 3" would be counting a failure that did not happen.
            line = f"It stays queued and posts again {when}."
        if streak and tone in ("interrupted", "missed"):
            # A missed run does not count against the streak — that is part of what
            # keeping its slot means — while an interrupted one is recorded as a
            # failed attempt (three of those in a row stop a repeat). Either way the
            # count is the state, not this run's verdict: "failure 2 of 3" beside a
            # *skipped* run reads as though the skip had counted.
            line += (
                f" Failures recorded: {streak} of {scheduled.FAIL_LIMIT} — one more "
                "stops it."
            )
        return line
    if msg.get("repeat_minutes"):
        return "Stopped — it will not try again. Fix the reason above, then queue it again."
    return "Nothing else will be sent for it. Queue it again when the reason above is fixed."


def _schedule_alert_embed(tone: str, msg: dict, view: dict, reason: str):
    """The card itself, as a real ``discord.Embed``.

    Separate from the posting so it can be checked against Discord's own limits
    without a gateway in the way: every value here is text a failure produced, and
    a reason long enough to be refused would turn "your message did not go out"
    into "nothing happened at all".
    """
    import discord

    icon, headline, color = _ALERT_TONES.get(tone, _ALERT_TONES["failed"])
    generator = str(msg.get("generator") or "")
    body = (
        f"{view.get('generator_label')} — built when it posts"
        if generator
        else (msg.get("preview") or "(an embed on its own)")
    )
    channels = ", ".join(
        f"#{item['name']}" if item.get("exists") else f"#{item['id']} (gone)"
        for item in view.get("channels", [])
    ) or "no channel"

    embed = discord.Embed(
        title=f"{icon} {headline}", color=color, timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="📝 Message", value=_clamp(body), inline=False)
    embed.add_field(name="📢 Channels", value=_clamp(channels), inline=False)
    embed.add_field(name="❌ Why", value=_clamp(reason) or "no reason recorded", inline=False)
    embed.add_field(
        name="🔁 What happens next", value=_clamp(_alert_next_step(tone, msg)), inline=False
    )
    # The Members intent is off for this app, so whoever queued the message may be
    # someone the cache has never held: the mention stands on its own rather than
    # rendering as "<@123> ()".
    name = str(view.get("created_by_name") or "").strip()
    embed.add_field(
        name="👤 Queued by",
        value=(
            f"<@{msg['created_by']}>" + (f" ({name})" if name else "")
            if msg.get("created_by")
            else "nobody on record"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Scheduled message #{msg.get('id')} • the dashboard has the full history")
    return embed


async def _alert_schedule_outcome(guild_id: int, msg_id: int, tone: str, reason: str) -> str:
    """Tell the server that one of its scheduled messages did not arrive.

    Returns how it was delivered (``log``, ``dm`` or ``""``), for the log and the
    tests. Never raises: a notification is worth less than the delivery loop, and
    one unreadable card must not become one unprocessed message.

    Only the delivery *loop* calls this. A failure an administrator watched happen —
    the panel's own **Send now** — is already answered in the page's receipt, a
    second earlier, to the person who pressed it.
    """
    try:
        import discord

        from utils.style import send_card

        msg = scheduled.get(guild_id, msg_id)
        if msg is None:
            return ""
        guild = BOT_INSTANCE.get_guild(guild_id) if BOT_INSTANCE else None
        if guild is None:
            print(
                f"[SCHEDULE] No card for #{msg_id}: the bot is not in server {guild_id}.",
                flush=True,
            )
            return ""

        icon, headline, _ = _ALERT_TONES.get(tone, _ALERT_TONES["failed"])
        embed = _schedule_alert_embed(tone, msg, _schedule_view(guild, msg), reason)

        creator = _int_or_none(msg.get("created_by"))
        content = f"{icon} {headline}"
        if creator is not None:
            content += f" — <@{creator}>"
        # The only person this card may ping is the one who queued the message.
        allowed = discord.AllowedMentions(
            users=[discord.Object(id=creator)] if creator is not None else [],
            roles=False,
            everyone=False,
        )

        log_id = _int_or_none(await db.get_setting(guild_id, "log_channel_id"))
        log_channel = guild.get_channel(log_id) if log_id else None
        if log_channel is not None:
            try:
                await send_card(
                    log_channel, content=content, embed=embed, allowed_mentions=allowed, timeout=None
                )
                return "log"
            except Exception as e:
                print(
                    f"[SCHEDULE] Could not post the card for #{msg_id} in the log channel: {e}",
                    flush=True,
                )

        # No usable log channel, or it refused: tell the person who asked for the
        # message. A DM carries no ping, and someone who has closed their DMs
        # simply gets no DM rather than a second failure to explain.
        member = guild.get_member(creator) if creator is not None else None
        if member is None and creator is not None:
            # The cache holds only people the bot has run into (the Members intent is
            # off), and "the person who queued it is told" has to be true rather than
            # likely — so ask Discord for that one id, the same way the composer does,
            # and with the same deadline: this runs on the delivery loop, where an
            # unanswered request would hold up every other due message.
            import asyncio

            try:
                member = await asyncio.wait_for(guild.fetch_member(creator), timeout=10.0)
            except Exception as e:
                print(
                    f"[SCHEDULE] Could not look up {creator} to tell them about "
                    f"#{msg_id}: {e}",
                    flush=True,
                )
                member = None
        if member is not None:
            try:
                await send_card(member, embed=embed, timeout=None)
                return "dm"
            except Exception as e:
                print(
                    f"[SCHEDULE] Could not DM the card for #{msg_id} to {creator}: {e}",
                    flush=True,
                )

        print(
            f"[SCHEDULE] {scheduled.describe(msg)}: {headline.lower()} — {_clamp(reason, 200)} "
            f"(no usable log channel, and nobody to tell directly).",
            flush=True,
        )
        return ""
    except Exception as e:
        print(f"[SCHEDULE] Could not report message #{msg_id}: {e}", flush=True)
        return ""


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
    # A restart that interrupted a send is the one outcome where the message may
    # have landed anyway, so it is told as itself rather than as a plain failure.
    for interrupted in scheduled.sweep_stale(now):
        await _alert_schedule_outcome(
            interrupted["guild_id"],
            interrupted["id"],
            "interrupted",
            interrupted.get("last_error") or "",
        )
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
            # Being too late to post is not a delivery failure, but it is still a
            # message the server was promised and did not get.
            await _alert_schedule_outcome(msg["guild_id"], msg["id"], "missed", reason)
            print(f"[SCHEDULE] {scheduled.describe(msg)}: {reason}", flush=True)
            done.append({"id": msg["id"], "status": "missed", "error": reason, "results": []})
            continue
        try:
            outcome = await _deliver_scheduled(msg, now)
        except Exception as e:
            # ``_deliver_scheduled`` is written not to raise, but a message that
            # somehow does must not take the rest of the queue down with it — and
            # leaving it claimed would hide a real failure behind a stale sweep.
            # The row is only settled if the claim is still open, so a message that
            # was already recorded (and had its repeat advanced) is not settled twice.
            raised = scheduled.get(msg["guild_id"], msg["id"]) or {}
            if raised.get("status") == "sending":
                # Conditional for the same reason the restart sweep's is: the read
                # above and this write are two steps, and the row can be settled in
                # between by another process. Only the caller whose write reports an
                # affected row owns the failure — and therefore posts the card.
                if scheduled.mark_failed(
                    msg["id"], now, f"Delivery failed: {e}", only_if_status="sending"
                ):
                    await _alert_schedule_outcome(
                        msg["guild_id"],
                        msg["id"],
                        _alert_tone(msg, scheduled.get(msg["guild_id"], msg["id"]) or raised),
                        f"Delivery failed: {e}",
                    )
            print(f"[SCHEDULE] {scheduled.describe(msg)}: delivery raised: {e}", flush=True)
            done.append({"id": msg["id"], "status": "failed", "error": str(e), "results": []})
            continue
        fresh = scheduled.get(msg["guild_id"], msg["id"]) or {}
        if outcome["sent"]:
            status = "sent"
            if outcome["failed"]:
                # Most of the announcement went out and one channel refused it:
                # the outcome nobody notices on their own.
                await _alert_schedule_outcome(
                    msg["guild_id"], msg["id"], "partial", outcome["error"]
                )
        elif outcome.get("skipped"):
            # Nothing to report is not news: a quiet week is the generator saying
            # so, and a card of zeros is what it exists to avoid posting.
            status = "skipped"
        else:
            status = "failed"
            await _alert_schedule_outcome(
                msg["guild_id"], msg["id"], _alert_tone(msg, fresh), outcome["error"]
            )
        done.append({
            "id": msg["id"],
            "status": status,
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


#: How often to ask the public site whether it is serving the repository. Six
#: hours is chosen against the failure, not against the cost: the check is nine
#: requests and the exposure it watches for is measured in days if it is
#: measured at all, so anything faster buys nothing and anything slower delays
#: the one message that matters. One re-check is done shortly after every boot,
#: because a boot is a deploy and a deploy is when the output directory could
#: have changed.
SITE_GUARD_INTERVAL_SECONDS = int(os.environ.get("SITE_GUARD_INTERVAL_SECONDS") or 6 * 3600)
SITE_GUARD_FIRST_CHECK_SECONDS = int(os.environ.get("SITE_GUARD_FIRST_CHECK_SECONDS") or 90)


async def _run_site_guard() -> None:
    """One pass: probe, remember the answer, and say something if it warrants it."""
    import asyncio

    verdict = await asyncio.to_thread(site_guard.check)
    site_guard.record(verdict)
    print(f"[SITE-GUARD] {site_guard.describe(verdict)}", flush=True)
    message = site_guard.note(verdict)
    if message and BOT_INSTANCE is not None:
        from utils import operator_log

        await operator_log.site_guard_alert(BOT_INSTANCE, verdict)


async def site_guard_loop():
    """Ask the public site, on a timer, whether it is publishing this checkout.

    It lives here rather than in CI on purpose. CI is where the *build* is
    verified, and CI is also the thing that can stop: while the Pages project was
    serving this repository, the `Site` and `Tests` workflows had been failing
    for an hour and a half because the account's Actions jobs would not start at
    all (`The job was not started because recent account payments have failed`),
    so nothing in them ran. A guard against a publishing mistake that shares a
    failure mode with publishing is not a guard.

    This process is always up and is already in the operator's server, so the
    alarm reaches a person without a new service or a new credential. Every
    branch is best-effort: a check that raises is reported and retried, because
    the alternative is a background loop that dies quietly and a workspace that
    reports itself as guarded forever.
    """
    import asyncio

    print(
        f"[SITE-GUARD] Watching {site_guard.site_url()} — first check in "
        f"{SITE_GUARD_FIRST_CHECK_SECONDS}s, then every {SITE_GUARD_INTERVAL_SECONDS}s.",
        flush=True,
    )
    await asyncio.sleep(max(1, SITE_GUARD_FIRST_CHECK_SECONDS))
    while True:
        try:
            await _run_site_guard()
            await asyncio.sleep(max(60, SITE_GUARD_INTERVAL_SECONDS))
        except Exception as e:
            print(f"[SITE-GUARD] Loop error (will retry): {e}", flush=True)
            await asyncio.sleep(60)


def start_site_guard(bot):
    """Start the check on the bot's own event loop."""
    if bot is None or getattr(bot, "loop", None) is None:
        print("[SITE-GUARD] No event loop — the site will not be watched.", flush=True)
        return None
    return bot.loop.create_task(site_guard_loop())


#: How often to re-read UptimeRobot. Five minutes is chosen against the source,
#: not against the page: the free API allows ten calls a minute and the numbers
#: it returns are computed on UptimeRobot's side over a day or a month, so a
#: faster poll would spend somebody's rate limit to learn the same thing. The
#: first pass is early because a boot is a deploy, and "did the deploy stay up?"
#: is the question this panel answers.
UPTIME_INTERVAL_SECONDS = int(os.environ.get("UPTIME_INTERVAL_SECONDS") or 300)
UPTIME_FIRST_CHECK_SECONDS = int(os.environ.get("UPTIME_FIRST_CHECK_SECONDS") or 60)


async def _run_uptime_check() -> None:
    """One refresh, one line to the host log, and a card only when it *changes*.

    The transition, not the state, is what is worth a message: a monitor that is
    down and was down is a standing condition, and repeating it every five
    minutes would bury the one card that mattered.
    """
    import asyncio

    current = await asyncio.to_thread(uptime_status.check)
    previous = uptime_status.record(current)
    print(f"[UPTIME] {current['status']}: {current['detail']}", flush=True)
    if previous is None or previous == current["status"]:
        return
    message = uptime_status.headline(current)
    if message and BOT_INSTANCE is not None:
        from utils import operator_log

        await operator_log.uptime_alert(BOT_INSTANCE, current, message)


async def uptime_status_loop():
    """Read UptimeRobot on a timer, so no page ever waits on it.

    Every branch is best-effort for the same reason the site check is: a
    background loop that dies quietly leaves a panel showing a number from an
    hour ago with nothing anywhere saying so.
    """
    import asyncio

    if not uptime_status.configured():
        print(
            "[UPTIME] Not connected — set UPTIMEROBOT_API_KEY to show monitor "
            "history on the dashboard and in /health.",
            flush=True,
        )
    await asyncio.sleep(max(1, UPTIME_FIRST_CHECK_SECONDS))
    while True:
        try:
            await _run_uptime_check()
            await asyncio.sleep(max(60, UPTIME_INTERVAL_SECONDS))
        except Exception as e:
            print(f"[UPTIME] Loop error (will retry): {e}", flush=True)
            await asyncio.sleep(60)


def start_uptime_status(bot):
    """Start the refresh on the bot's own event loop."""
    if bot is None or getattr(bot, "loop", None) is None:
        print("[UPTIME] No event loop — monitors will not be read.", flush=True)
        return None
    return bot.loop.create_task(uptime_status_loop())


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
