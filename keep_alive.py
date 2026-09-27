"""
Real-Time Web Admin Dashboard & Keep-Alive Server for the DaCat Discord Bot.
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
import asyncio
import functools
import json
import math
import os
import re
import sqlite3
import sys
import time
import secrets
import urllib.parse
from collections import deque
from threading import Lock, Thread
import socket
import datetime
import html as _html
import ipaddress
from flask import (
    Flask, render_template, request, redirect, send_from_directory, session, jsonify,
    url_for, Response, abort,
)
from utils import (
    ai_guard, alert_webhook, brand, command_publish, command_scope, company, compression,
    config_audit, consent, db, digest, discord_auth, features, figures, guild_directory,
    # `health` is the name this module's own `/health` route already owns, so the
    # checklist is aliased the way `levels` is: one name, one meaning, and an
    # import that shadows a route is a page that raises on every render.
    health as health_checks, hub, identity, levels as levels_module, media_health, meowfee,
    moderation_actions, modlog, modules, pause, permissions, plans, principal, qna,
    ratelimit, scheduled, server_control, sessions, site_guard, support,
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

    def isatty(self):
        return getattr(self.target, "isatty", lambda: False)()

    def fileno(self):
        if hasattr(self.target, "fileno"):
            return self.target.fileno()
        raise OSError("fileno not supported")

    def __getattr__(self, name):
        return getattr(self.target, name)

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

sys.stdout = LogTee(sys.stdout)
sys.stderr = LogTee(sys.stderr)

app = Flask(__name__, template_folder="templates")

# Egress is what this host is billed for, and the pages and JSON this process
# writes are the repetitive text that gzips five to eight times over. Registered
# here, before every other ``after_request`` hook in this module, because Flask
# runs them in reverse order — so this one sees the *final* headers, including
# the ``Vary`` the security pass sets for the two market endpoints.
compression.register(app)


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


#: The properties of a response that are about the *transfer* rather than the
#: message, and that the forwarder below must therefore not copy: the length is
#: recomputed from the body it actually sends, and the type has to be declared on
#: the response object rather than appended as a second header.
_TRANSFER_HEADERS = frozenset({"content-length", "content-type"})


def forwarded_response(status: int, headers, body: bytes) -> dict:
    """The arguments for the response that carries what Flask answered.

    The Space does not serve this app directly: ``app.py`` launches Gradio and
    mounts the Flask app on it as a catch-all, so every page a browser asks for
    is re-sent by that mount. This function is the first half of what the mount
    sends; :func:`carried_headers` is the second.

    It exists because the mount used to build its own response from the body and
    then copy Flask's headers **minus ``content-type``** — the exclusion looked
    like de-duplication, since the new response object sets its own type, but it
    set *none*: a starlette ``Response`` with no ``media_type`` has no
    ``Content-Type`` header at all, and the header was being filtered out before
    anything could put it back. A browser that receives HTML with no type sniffs
    it and renders the **source**, so the console, the dashboard and both sign-in
    pages were plain text in Chrome while the same bodies rendered correctly when
    Flask was asked directly — which is exactly what made this invisible from the
    server side: nothing failed, the bytes were right, and only the interpretation
    of them was missing.

    So the type travels as ``media_type``, whole, with the charset Flask chose
    (starlette appends one of its own only when the media type carries none, so
    ``text/html; charset=utf-8`` stays one header and not two).
    """
    content_type = None
    for key, value in headers or ():
        if key.lower() == "content-type":
            content_type = value
    kwargs: dict = {"content": body, "status_code": status}
    if content_type:
        kwargs["media_type"] = content_type
    return kwargs


def carried_headers(headers) -> list[tuple[str, str]]:
    """The headers of Flask's answer that the response must be *appended* with.

    The type is not among them — it travels as ``media_type``, starlette's own
    way of declaring one, which keeps the charset Flask chose and puts it back
    when the header is serialised. The length is not among them either: the mount
    knows the body it is actually sending, and Flask's length is the length *it*
    sent.

    Everything else is, in Flask's order and **as pairs rather than a mapping**,
    because ``Set-Cookie`` may legitimately appear more than once and a starlette
    ``Response`` only takes a mapping for its ``headers`` argument — the second
    cookie would be dropped before anyone saw the response. So the mount appends
    these to the response it built, where a repeat is a separate entry and two
    cookies stay two cookies.
    """
    return [(key, value) for key, value in headers or () if key.lower() not in _TRANSFER_HEADERS]


# Four environments render these templates in this repository — this app, the
# `scripts/preview_dashboard.py` harness, and two test helpers that build a bare
# Jinja environment to render a page and read it — and Jinja 3 gives each of them
# its own globals dict, so there is no single place to put this. Each registers
# the same function, and a missing registration fails loudly the moment anything
# renders that page rather than quietly shipping an unversioned URL.
# The brand is a *global* rather than a context key for the reason above, one
# step further: the frame's `<meta name="theme-color">` and the hub's own two
# headings both need it, they arrive from two different context builders, and a
# name that is missing from one of them renders an empty string — which is how
# this project once shipped eleven pages whose wordmark was a lone ".". A global
# cannot be forgotten by a route that was written after it.
app.jinja_env.globals["brand"] = brand
app.jinja_env.globals["asset_version"] = asset_version
_env_secret = config_audit.get("FLASK_SECRET_KEY")
if _env_secret:
    app.secret_key = _env_secret
else:
    app.secret_key = secrets.token_hex(32)
    print(
        "[DASHBOARD] FLASK_SECRET_KEY is not set. Using a random key for this process "
        "(sessions reset on restart). Set FLASK_SECRET_KEY in the environment.",
        flush=True,
    )

_behind_https_host = bool(
    config_audit.is_set("RENDER") or config_audit.is_set("RAILWAY_ENVIRONMENT")
)
# Only trust proxy headers when we are actually behind a known reverse proxy.
# Otherwise a client could spoof X-Forwarded-For and bypass login rate limiting.
_trust_proxy_headers = _behind_https_host or config_audit.flag("TRUST_PROXY_HEADERS")
# A self-hosted reverse proxy cannot be fingerprinted the way Render can, and the
# server binds 0.0.0.0 — so without this switch a deployment behind its own TLS
# terminator issued a session cookie with no Secure flag, and a plaintext request
# to port 8080 would carry it. DASHBOARD_HTTPS=1 says "this host is HTTPS only".
_https_only_override = config_audit.flag("DASHBOARD_HTTPS")
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
ADMIN_PASSKEY = config_audit.get("ADMIN_PASSKEY")
if not ADMIN_PASSKEY:
    print(
        "[DASHBOARD] ADMIN_PASSKEY is not set — passkey login and the passkey API "
        "endpoints are disabled. Admins can still sign in with the one-time link "
        "from the ⚡ Open Dashboard button on /help. Set ADMIN_PASSKEY to enable "
        "passkey login.",
        flush=True,
    )
DEFAULT_OWNER_IDS = {1526964236708745318, 1348622207999279146, 1549177590328205393}
OWNER_USER_ID = int(config_audit.get("OWNER_USER_ID", "0") or "0")


def _is_owner_user(user_id: int | None) -> bool:
    if not user_id:
        return False
    if OWNER_USER_ID and user_id == OWNER_USER_ID:
        return True
    return user_id in DEFAULT_OWNER_IDS


#: Whether a Discord sign-in matching owner accounts may open the console by
#: itself. When 1, the Owner Discord Sign In button directly opens the console.
OWNER_DISCORD_ELEVATION = config_audit.flag("OWNER_DISCORD_ELEVATION")

#: The header the owner site's Cloudflare Worker stamps with the visitor's own
#: address, and the header carrying the shared secret that proves the stamp came
#: from that worker rather than from the visitor. Both are needed: see
#: ``_client_ip`` for what a header alone would mean.
CLIENT_IP_HEADER = "X-Meow-Client-IP"
PROXY_SECRET_HEADER = "X-Meow-Proxy"
OWNER_PROXY_SECRET = config_audit.get("OWNER_PROXY_SECRET")


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
        # Recorded here rather than inside the limiter, because ``remaining`` is a
        # question a caller may ask without acting on — the refusal happens at
        # the call site, so this is where the window learns one occurred.
        _PASSKEY.note_refused()
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
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5

# The API's passkey is the same credential the login form protects, so it gets the
# same treatment: without a window, /api/set-module was an unlimited oracle for a
# long-lived secret that is typed once into a host's dashboard and never rotated.
PASSKEY_WINDOW_SECONDS = 15 * 60
PASSKEY_MAX_ATTEMPTS = 10

#: The three doors, and their one implementation (see ``utils/ratelimit.py``).
#: Named rather than anonymous because the console's Access page reports each
#: window separately: "too many failed attempts" is about the passkey, and the
#: sign-in form's window is a different question with the same shape.
_SIGN_IN = ratelimit.RateLimiter("sign-in", LOGIN_WINDOW_SECONDS, LOGIN_MAX_ATTEMPTS)
_PASSKEY = ratelimit.RateLimiter("passkey", PASSKEY_WINDOW_SECONDS, PASSKEY_MAX_ATTEMPTS)
#: The buckets themselves, under the names the suite has always driven them by —
#: ``keep_alive._passkey_attempts.clear()`` in a test's ``setUp`` is how every
#: passkey test isolates itself, and the limiter's map *is* this object.
_login_attempts = _SIGN_IN.buckets
_passkey_attempts = _PASSKEY.buckets

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


def _proxy_signed() -> bool:
    """Whether this request proves it came through our own worker.

    A hop header is a claim about a chain the caller may not be part of, and the
    backend is directly reachable — measured on the live deployment: a bare
    `curl -H 'X-Forwarded-Host: evil.example'` to the Space's own address was
    answered with an OAuth redirect_uri pointing at `evil.example`, because
    "behind a proxy" had been read as "the header can be believed". It cannot: the
    Cloudflare edge is not the only door, and the address in that header is what
    a sign-in comes back to.

    So a forwarded value is believed only when the request carries *our worker's*
    stamp, which is an HMAC-free shared secret compared in constant time — the
    one thing a caller cannot write. Two conditions, both required: the
    deployment says it is proxied (``_trust_proxy_headers``) and the secret
    matches.
    """
    if not (_trust_proxy_headers and OWNER_PROXY_SECRET):
        return False
    try:
        from flask import has_request_context  # noqa: PLC0415 — one reader, no cycle

        if not has_request_context():
            return False
        offered = request.headers.get(PROXY_SECRET_HEADER, "")
        return bool(offered) and secrets.compare_digest(offered, OWNER_PROXY_SECRET)
    except Exception:  # noqa: BLE001 — a request context that cannot be read proves nothing
        return False


def _forwarded_host() -> str:
    """The address this request arrived on, when our own worker says so.

    Empty when nothing does — a direct connection to this process, a local run, an
    offline build, or a forged header — and that is deliberately **not** answered
    with a guess. The fallbacks in :func:`dashboard_base_url` answer a different
    question ("which address should a link name"), and a request that arrived on
    no address in particular must not be dressed as the member dashboard's: the
    three published addresses draw three different headers, so a guess here puts
    the wrong one on the page.
    """
    if not _proxy_signed():
        return ""
    try:
        from flask import has_request_context  # noqa: PLC0415 — as above

        if has_request_context():
            return (request.headers.get("X-Forwarded-Host") or "").strip()
    except Exception:
        pass
    return ""


def _forwarded_proto() -> str:
    """The scheme that address was reached over, `https` unless it says otherwise.

    Signed like the host, and for the same reason: the scheme decides whether the
    session cookie is marked Secure and whether HSTS is sent, and a caller who
    can set it can talk a deployment out of both.
    """
    if not _proxy_signed():
        return "https"
    try:
        from flask import has_request_context  # noqa: PLC0415 — as above

        if has_request_context():
            return (request.headers.get("X-Forwarded-Proto") or "").strip() or "https"
    except Exception:
        pass
    return "https"


def _on_member_host() -> bool:
    """Whether this request arrived on the member dashboard's own address.

    The one question the header of a served page turns on (see :func:`_frame_nav`),
    asked of the address rather than of the page: `dacatdashboard.pages.dev`
    answers `/dashboard` and `/servers` and answers `/systems`, `/start` and
    `/radio` with a 301 to the public site, so the pages that host serves are
    right to offer the first pair and wrong to offer the second — and the *same*
    templates are served on the public address, where the second pair is exactly
    right.
    """
    host = _forwarded_host().rstrip("/").lower()
    return bool(host) and host == urllib.parse.urlsplit(hub.dashboard_url()).netloc.lower()


def _on_owner_host() -> bool:
    """Whether this request arrived on the operator console's own address.

    The same question as :func:`_on_member_host`, asked of the *third* address,
    and asked for the same reason. The console's host serves `/agree` — the
    clickwrap every sign-in is sent through before it gets anywhere, the
    operator's passkey sign-in included — and `agree.html` extends the shared
    frame, so on that host it was wearing the public site's marketing nav: Home,
    Systems, Radio and an "Add to Discord" button, every one of them a path that
    host answers with a 301 to the public site. The console's two own pages
    override their nav by hand (`templates/_owner_nav.html`); `/agree` is the one
    page of that host with no page of its own to override it. Read from
    `utils.hub.console_url` — the reader the console's build and its outward
    links use — so a deployment that moves its console moves this answer too;
    `hub.live_url` is the *machine's* address, which is one hop behind this one
    and never what a browser asked for.
    """
    host = _forwarded_host().rstrip("/").lower()
    return bool(host) and host == urllib.parse.urlsplit(hub.console_url()).netloc.lower()


def _console_frame_nav() -> dict:
    """What a page of the *console* host needs to draw that host's own header.

    A dict rather than a list, and the shape is the difference that matters: the
    member host's header is a list of destinations (`MEMBER_FRAME_NAV`), while
    the console's is a macro that also names the two addresses it links out to
    (`_owner_nav.html`). Empty everywhere but the console host, so every other
    reader of the same template — the member app, the public site, an offline
    build, a local run — keeps the frame it already had, which is the property
    `_frame_nav` was built for and this one now shares.
    """
    if not _on_owner_host():
        return {}
    return {
        "console_frame": True,
        "site_url": hub.marketing_url(),
        "dashboard_url": hub.dashboard_url(),
    }


def dashboard_base_url() -> str:
    """Where *this* process is reachable, for the one-time sign-in link.

    The token lives in this process's memory, so a link that names a different
    host is dead on arrival — and that is exactly how this project is run: the
    bot is deployed on Render *and* run from the development Mac, where
    ``DASHBOARD_BASE_URL`` is unset and every link pointed at the Render URL,
    whose process had never heard of the token. "That link has expired" was the
    symptom, and nothing had expired.
    """
    explicit = config_audit.get("DASHBOARD_BASE_URL").rstrip("/")
    if explicit:
        return explicit
    forwarded_host = _forwarded_host()
    if forwarded_host:
        return f"{_forwarded_proto()}://{forwarded_host}".rstrip("/")
    # Render sets this to the service's public URL automatically.
    hosted = config_audit.get("RENDER_EXTERNAL_URL").rstrip("/")
    if hosted:
        return hosted
    if config_audit.is_set("RENDER"):
        return "https://meow-discord-bot.onrender.com"
    if config_audit.is_set("SPACE_ID") or config_audit.is_set("RAILWAY_ENVIRONMENT"):
        # The dashboard has an address of its own now, and this is the host the
        # member-facing sign-in happens on — so it is also the redirect Discord
        # is told about. A deployment that has moved sets `DASHBOARD_BASE_URL`,
        # which is read above; this is the fallback for the machine itself.
        return "https://dacatdashboard.pages.dev"
    # Anywhere else the dashboard is a local one, and the admin is on the same
    # machine as the process that minted the token.
    return f"http://localhost:{config_audit.get('PORT', '8080')}"


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
    """The sign-in page, with every method's availability read from the environment.

    The frame — the name, the navigation, the footer, the invite — comes from
    :func:`_site_context` rather than being spelled out here, which is what this
    page used to do: it listed the sign-in page's own fields and six of the
    frame's, so its footer says *"A Discord bot with a dashboard for every
    server that has it"* where every other page states the real counts, and its
    navigation could never show a Pricing link on a deployment that sells. Two
    answers to one question is the failure the shared context exists to end.
    """
    body = render_template(
        "login.html",
        **_site_context(),
        # Which address this door is on decides its header — the member host
        # offers the app's own pages, every other address the site's.
        **_frame_nav(),
        current="",
        error=error,
        csrf_token=get_csrf_token(),
        # The operator's passkey is the **console** host's door (`/owner/login`,
        # since `1a56bf8` took it off this page), and this is the answer
        # `login.html` asks for rather than a flag that was left behind: the
        # route still accepts a passkey — the endpoint is the same one — but this
        # page is the member door, and a second entrance on the address members
        # use is the defect the console's own prefix list records.
        passkey_enabled=False,
        # `oauth_enabled` is the shared context's — it is the same call
        # (`discord_auth.enabled()`), and passing it twice is a `TypeError`
        # rather than a silent override, which is how this was found.
        oauth_callback=oauth_redirect_uri(),
        oauth_hint=_oauth_setup_hint(),
    )
    return (body, status) if status else body


def _owner_signin_notice() -> str:
    """Why somebody already signed in is looking at the operator's sign-in page.

    The Discord route answers "who is this" and not "is this the operator", so
    the two must be told apart on the page itself: a form with no explanation is
    indistinguishable from a console that refused them.
    """
    if not is_authenticated() or _principal().is_operator:
        return ""
    if session.get("owner_candidate"):
        return (
            "That is the owner's Discord account, and the console still wants "
            "the passkey. Enter it to unlock the fleet."
        )
    return (
        "You are signed in, but this console belongs to the operator. The "
        "passkey is the only way in."
    )


def _owner_login_page(error=None, status: int | None = None):
    """The operator sign-in page for the /owner console."""
    body = render_template(
        "owner_login.html",
        **_site_context(),
        current="",
        error=error,
        csrf_token=get_csrf_token(),
        notice=_owner_signin_notice(),
        passkey_enabled=bool(ADMIN_PASSKEY),
        discord_is_enough=OWNER_DISCORD_ELEVATION,
        # Where the public site is, for the one link on this page that leaves the
        # operator's surface. It used to be "/", which on the owner host is the
        # console — so "Return to Public Site" returned the operator to the page
        # they were already on. The address comes from the same reader the hub
        # uses, so a deployment that moves its site moves this link with it.
        site_url=hub.marketing_url(),
        # …and where the *member* dashboard is, for the page's other outward link.
        # That one used to be `/login`, which this host does not serve: the worker
        # answers it with a 301 to the public site, so "sign in to a server's own
        # dashboard" handed the visitor to a third host through two redirects.
        # Same reader as the link in the console's own nav (`_owner_page` below),
        # so the two cannot name different addresses for one surface.
        dashboard_url=hub.dashboard_url(),
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


def _begin_session() -> None:
    """Start a fresh session: the one this browser was holding is *retired*.

    Every door clears the session before it signs somebody in, and does so on
    purpose — a sign-in that keeps the previous session id is a session-fixation
    bug. But ``session.clear()`` only *forgets* the id. The ledger row stays, so a
    dead browser keeps a live-looking row in the console's list, and the same
    person signing in again appears twice. Closing the row first makes the list
    describe browsers rather than sign-ins.
    """
    sessions.revoke(session.get(sessions.COOKIE_KEY))
    session.clear()


def _record_session(*, label: str = "") -> None:
    """Put this sign-in in the ledger, with the address and browser it came from.

    Called at every sign-in, after the session's scope has been decided: the row
    has to describe the session that actually exists, or the console's list is a
    description of something else. A failure to write is not a failure to sign
    in — ``utils/sessions`` answers "" and the session simply cannot be revoked,
    which the Access page says out loud.

    **The sid is kept only while it is still believed.** Two sign-ins in one
    session — a signed-in person entering the passkey — keep the row they started
    with, so the list does not grow a second entry for the same browser. But a sid
    the ledger has refused (revoked from the Access page, expired, or minted by a
    database that a restore has since replaced) must be *replaced* here rather
    than kept: the console answers such a session with "your sign-in is no longer
    valid, reload to sign in again", and the sign-in that follows is the reload.
    Keeping the dead sid made that advice impossible to follow — the person signed
    in successfully and stayed refused, with a cookie purge as the only way out.
    """
    sid = session.get(sessions.COOKIE_KEY)
    if sid and sessions.live(sid):
        return
    sid = sessions.start(
        session.get("auth_scope") or "",
        guild_id=session.get("guild_id"),
        user_id=session.get("user_id"),
        label=label or str((session.get("discord_user") or {}).get("name") or ""),
        ip=_client_ip(),
        agent=request.headers.get("User-Agent", ""),
    )
    if sid:
        session[sessions.COOKIE_KEY] = sid
    else:
        # A host that cannot write the ledger has no row to point at, and a sid
        # left in the cookie would be one nothing knows — which is refused. An
        # empty session is adopted at first use instead, which is the same ring
        # of fallbacks ``is_authenticated`` describes.
        session.pop(sessions.COOKIE_KEY, None)


def is_authenticated():
    """Whether this request carries a sign-in that is still believed.

    Two checks rather than one, and the second is the new one: Flask's cookie
    says *this server signed this*, and the ledger says *and it has not been
    revoked*. A session with no sid — one opened before the ledger existed, or
    one whose row could not be written — is adopted here on its first request so
    that it, too, becomes something the operator can end.
    """
    if session.get("authenticated") is not True:
        return False
    sid = session.get(sessions.COOKIE_KEY)
    if not sid:
        _record_session(label="adopted at first use")
        return True
    return sessions.live(sid)


def _principal() -> principal.Principal:
    """This request's session, as the policy object.

    Spelled as a function rather than a cached module global because Flask's
    session is per-request and this process serves them concurrently. The rules
    themselves live in ``utils/principal.py``, in one table, which is the point:
    eleven comparisons against three string literals had eleven homes for the
    same policy.
    """
    return principal.from_session(session)


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


def _normalise_ip(value: str | None) -> str | None:
    """An address as text, or ``None`` if that is not what this is.

    The value becomes a bucket key, so anything that is not an address is
    refused rather than trusted: a header that carried a list, a path or a
    megabyte of padding would otherwise be a way to choose a bucket.
    """
    text = (value or "").strip()
    if not text or len(text) > 64:
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def _client_ip() -> str:
    """Return the client IP used for rate limiting.

    Two sources, in the order they can be believed:

    1. The **worker's signed stamp**, honoured only when it arrives with
       ``OWNER_PROXY_SECRET`` (:func:`_proxy_signed`). That host is a proxy of a
       proxy, so a forwarded chain ends at *Cloudflare* rather than at the
       visitor: every person on the owner domain shared one bucket, which is a
       five-request lockout anybody could trigger, and it is why the worker
       names the visitor instead of leaving us to infer one.
    2. The **peer** address, which is the only one a caller cannot write.

    ``X-Forwarded-For`` is deliberately gone. It read as evidence while the
    backend was only ever reached through the edge; the deployment is reachable
    directly too, where the header is whatever the caller typed.
    """
    if _proxy_signed():
        stamped = _normalise_ip(request.headers.get(CLIENT_IP_HEADER))
        if stamped:
            return stamped
    # The peer address, and **nothing else**. ``X-Forwarded-For`` used to be the
    # fallback for a deployment with the trust flag on, and that fallback was the
    # hole: the backend answers its own address directly, so a caller who never
    # went through the worker could pick the hop the rate limiter counted — five
    # wrong passkeys per invented address is an unlimited number of guesses. A
    # chain a caller can write is not evidence, so it is not read at all. A
    # proxy that wants its visitors counted per visitor sends the signed stamp
    # (see ``scripts/build_owner.py``), not a header.
    return (request.remote_addr or "unknown")[:64]


def _login_allowed(ip: str) -> bool:
    """Whether this address may try the passkey form again yet.

    A question, not a verdict: the sign-in route records only the *failures*, so
    a correct passkey leaves no trace in the window (it clears whatever is
    there, which is why somebody who mistypes once and then gets it right is not
    one attempt closer to a lockout).
    """
    return _SIGN_IN.remaining(ip) > 0


def _record_login_failure(ip: str):
    _SIGN_IN.record(ip)


def _clear_login_failures(ip: str):
    _SIGN_IN.clear(ip)


def _passkey_attempts_left() -> int:
    """How many passkey guesses this client has left in the current window."""
    return _PASSKEY.remaining(_client_ip())


def _record_passkey_failure() -> None:
    _PASSKEY.record(_client_ip())


def _clear_passkey_failures() -> None:
    _PASSKEY.clear(_client_ip())


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
    who = _principal()
    if who.is_administered_session:
        # Only the *ids* live in the session. The session is a signed cookie, and
        # a name per server would push a person who administers a few dozen of
        # them past the 4 KB a browser will store — a session that vanishes on
        # sign-out with no error anywhere, which is the worst kind of bug to
        # debug. Names come from the bot's live cache instead, so a renamed
        # server shows its new name and one the bot has left disappears by
        # itself.
        return [known[gid] for gid in who.administered if gid in known]
    if who.is_scoped_link:
        return [known[who.guild_id]] if who.guild_id in known else []
    if who.is_operator:
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
    who = _principal()
    if not (who.is_administered_session or who.is_scoped_link):
        # The operator's scope sees every server the bot is in, by design.
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
    who = _principal()

    if who.is_scoped_link:
        gid = who.guild_id
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

    if who.is_administered_session:
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
        stored = who.guild_id
        if stored is not None:
            wanted_candidates.append(stored)
        for gid in wanted_candidates:
            if gid in allowed:
                return gid
        # Someone who manages none of them is not an error: they are signed in,
        # and the picker's job is to tell them what to do about it.
        return min(allowed) if allowed else None

    if not who.is_operator:
        return None

    session_guild = who.guild_id
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


def _form_payload(form) -> dict:
    """A plain HTML form read as the JSON payload the endpoints already take.

    Multi-valued fields stay lists (the channels, the tags) and single ones become
    strings, which is the shape ``request.get_json()`` would have given — so an
    endpoint that reads ``data.get("content")`` does not care which door the
    request came through.
    """
    data = {}
    for key in form.keys():
        values = form.getlist(key)
        data[key] = values if len(values) > 1 else values[0]
    return data


def _json_or_form() -> dict:
    """This request's payload, from JSON or from a plain form.

    The dashboard's own forms are the whole reason: an HTML form posts
    ``application/x-www-form-urlencoded``, these endpoints read JSON, and the
    alternative — a second implementation of every rule behind a form-shaped door
    — is the defect this file keeps paying for. One adapter, at the read.
    """
    data = request.get_json(silent=True)
    if data:
        return data
    if request.form:
        return _form_payload(request.form)
    return {}


def _safe_dash_return(value) -> str:
    """The page a form asked to be sent back to, or ``/dashboard``.

    A relative path **inside this app**, and nothing else: this value rides in a
    hidden field, which makes it a request rather than a fact, and an open
    redirect built out of a form field is how a page ends up sending somebody to
    a host it does not own.

    Two prefixes are allowed rather than one because both surfaces now write the
    same way — the operator console's switches and the dashboard's are forms that
    post to the same endpoints, and a console form that could only name a
    ``/dashboard`` return would land the operator on somebody else's server page
    after every flip. Neither prefix can leave the host: both are literal starts
    of a path, ``//`` is rejected before either is considered (it is a protocol-
    relative URL, which ``startswith("/owner")`` would otherwise accept as
    ``//evil.example``), and a backslash is rejected because a browser treats it
    as a slash.
    """
    text = (value or "").strip()
    if text.startswith("//") or "\\" in text:
        return "/dashboard"
    if text.startswith("/owner") or text.startswith("/dashboard"):
        return text
    return "/dashboard"


def _form_door(view):
    """Let a dashboard form post to a JSON endpoint and land back on a page.

    These endpoints answer JSON because that is the right contract for the console
    and for scripts — and the wrong thing to show a person who pressed a button in
    a browser, which is a tab full of ``{"success": false, …}``. Rather than a
    second implementation behind a form-shaped door, the form posts to the **same
    endpoint** and this turns the same answer into a redirect carrying its
    sentence: the server's own refusal, or — when the endpoint reports what
    actually happened in ``said`` — that sentence, falling back to the wording
    the page supplied in ``dash_notice``.

    ``said`` outranks the form's ``dash_notice`` deliberately: the form can only
    guess at the outcome before posting ("Answered — it is on its way as a DM"),
    while ``said`` is written after the fact, so a blank reply box or a DM Discord
    refused would otherwise be reported to the operator as a delivery that
    happened. The form's wording remains the floor for endpoints that answer
    with no sentence of their own — the switches, which say which system moved
    on the page that moved it.

    **And it outranks the generic line on a refusal too**, because two of the
    endpoints behind this door report the refusal under ``said`` and never use
    the word ``error`` at all: a member action answers with
    ``moderation_actions.Outcome.to_dict()`` (``detail``, ``said``, no ``error``)
    and a whole-server action with ``server_control.Report.to_dict()``, which is
    the same shape. Reading only ``error`` handed the page *"That did not go
    through."* every time the bot explained itself — *"I could not read your own
    member in that server"*, *"You cannot moderate me"*, *"**Bob** has a role at
    or above yours"* — all of it composed, all of it dropped one line before the
    person who pressed the button. A refusal with something to say says it.

    A form post is recognised by ``dash_return`` rather than by ``request.form``
    alone, because ``/api/identity`` is multipart for the console's app as well and
    a JSON caller sends neither.
    """

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        response = view(*args, **kwargs)
        target = request.form.get("dash_return") if request.form else None
        if not target:
            return response
        status = 200
        if isinstance(response, tuple):
            response, status = response[0], response[1]
        try:
            payload = json.loads(response.get_data(as_text=True))
        except Exception:  # noqa: BLE001 — an answer that is not JSON has no sentence
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        error = str(payload.get("error") or "")
        # Whichever page asked for the redirect is the one that reads its own
        # keys: the console pops ``owner_notice``/``owner_error`` and the
        # dashboard pops ``dash_notice``/``dash_error``, so writing only the
        # dashboard's pair would send an operator back to a console page that
        # had been handed a sentence it never reads.
        to_console = _safe_dash_return(target).startswith("/owner")
        notice_key = "owner_notice" if to_console else "dash_notice"
        error_key = "owner_error" if to_console else "dash_error"
        if status >= 400 or error:
            session[error_key] = (
                error or str(payload.get("said") or "") or "That did not go through."
            )
        else:
            session[notice_key] = (
                payload.get("said") or request.form.get("dash_notice") or "Saved."
            )
        return redirect(_safe_dash_return(target))

    return wrapped


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


#: Paths that are readable without any kind of sign-in, and are therefore
#: readable without agreeing to anything. The policies come first because they
#: have to: an acceptance wall in front of the Terms is worse than no Terms, and
#: a crawler cannot assent to a page it is refused. ``/agree`` itself is here for
#: the obvious reason, and the two directive files and the traps are here because
#: refusing them would mean refusing the agents that are being told what the
#: rules are.
#:
#: A path that is on neither list is **gated**: the invariant test walks Flask's
#: own route table and fails on a new route that was not classified, so adding a
#: page means choosing which side of the clickwrap it is on rather than getting
#: whichever answer the code happened to default to.
#: `True` for the one address that is a whole page with nothing under it. It is
#: separate from the prefix list because a prefix list containing ``"/"`` makes
#: **every** path public — the classification test below would pass while
#: classifying nothing, and a new private route would silently inherit public
#: headers. That is the shape of a guard that reads as a rule and is not one.
PUBLIC_EXACT = ("/",)

PUBLIC_PREFIXES = (
    "/systems",
    "/start",
    "/pricing",
    "/privacy",
    "/terms",
    "/ai",
    "/ai.txt",
    "/robots.txt",
    "/.well-known/",
    "/support",
    "/donate",
    "/market",
    "/health",
    "/hub",
    # A feature's own page, in the public nav beside the other product pages.
    # Found by fixing the check that exists to catch exactly this: `/music` was on
    # *neither* list, and the list it was missing from made it a signed-in surface
    # — the agreement stood in front of the marketing page for a music feature,
    # and it was served `noindex`, which is the wrong answer twice over. The
    # rename kept the old address here as well as on the route, because an old
    # link must reach the redirect rather than the agreement.
    "/radio",
    "/music",
    "/login",
    "/logout",
    "/agree",
    "/static/",
    "/api/image",
    "/api/figures",
    "/api/uptime",
    "/api/status",
    "/api/market/",
    "/api/_index",
    "/_corpus",
)

#: The signed-in surfaces the agreement stands in front of. Written out rather
#: than inferred from "is this route authenticated", because the question the
#: clickwrap answers is about *this* machinery: it exists so that nobody reaches
#: a composer that posts as the bot, or an economy adjustment, without having
#: been shown the terms those actions are taken under.
GATED_PREFIXES = (
    "/dashboard",
    "/servers",
    "/owner",
    "/api/config",
    "/api/live-logs",
    "/api/identity",
    "/api/toggle",
    "/api/set-module",
    "/api/voice-debug",
    "/api/mentionables",
    "/api/send-message",
    "/api/scheduled",
    "/api/schedule-message",
    "/api/console/",
    # Paths that were gated by *default* rather than by declaration, which is
    # the safe direction and still the wrong one: the split decides whether the
    # agreement stands in front of a path and whether it is served `noindex`, and
    # a list nobody wrote a name into cannot be read as a decision. All of these
    # are the dashboard's own (`_require_auth_api` is what actually stops a
    # stranger), so declaring them changes no behaviour — it makes the sentence
    # true rather than accidental.
    "/api/command-scope",
    "/api/channels",
    # …the leveling rates, which is the same case arriving by the other door: the
    # write is the dashboard's own form, and the gate it already had is the same
    # one the switches answer with.
    "/api/level-settings",
    "/api/server-settings",
    # A system's own settings (thresholds, filters, ceilings) and the reaction
    # roles a server manages from its Server page: both are the same authority
    # and the same agreement as the switches they sit beside.
    "/api/module-settings",
    "/api/reaction-roles",
    # The support bridge: a message posted from here speaks as the bot in a
    # channel, which is the same authority the composer has and belongs behind
    # the same agreement.
    "/api/support",
    # The members and server panels: the same authority as the toggles (a
    # session's principal, re-checked per request), and they belong behind the
    # agreement for the same reason — these are actions *as* the bot against
    # people, and the terms are what somebody agreed to before taking them.
    "/api/member-action",
    "/api/member-warnings",
    "/api/server-action",
    "/api/console/server-action",
)


def _agreement_stands_here(path: str) -> bool:
    """Whether the clickwrap is asked of this path.

    Fail-closed by construction: a path that neither list names is gated. That is
    the safe direction for a rule whose whole purpose is that a condition of use
    was shown before the page behind it worked.
    """
    if path in PUBLIC_EXACT:
        return False
    if any(path == gate or path.startswith(gate) for gate in GATED_PREFIXES):
        return True
    return not any(path == public or path.startswith(public) for public in PUBLIC_PREFIXES)


def _note_guard(kind: str, *, agent: str, path: str, label: str = "") -> None:
    """Write the event down, and tell the operator when it is worth telling.

    The alert goes out on a thread rather than in the request: a crawler waiting
    for a Discord card to be composed would be a crawl that costs this deployment
    the latency of its own notification. Striking a trap and *then* being told
    about it late is the correct order; being told about it quickly and having
    the request held open is not.
    """
    event = ai_guard.note(
        kind,
        agent=agent,
        path=path,
        ip=_client_ip() or "",
        country=request.headers.get("CF-IPCountry", ""),
    )
    print(
        f"[GUARD] {kind}: {event['agent'] or 'no user-agent'} at {event['path']} "
        f"(x{event['hits']})",
        flush=True,
    )
    if not event["alert"]:
        return
    # Two destinations on purpose, because they fail differently. The card is for
    # the operator who is *watching*; the webhook is out of band and silent, and it
    # is the one that still arrives when the gateway is down — which is exactly the
    # situation a flood creates. Neither can raise, and both are sent from here so
    # the decision about what is worth reporting stays in one place.
    try:
        alert_webhook.guard_event(kind, event, label=label)
    except Exception as exc:  # noqa: BLE001 — an alert may never fail a request
        print(f"[GUARD] could not hand the event to the alert webhook: {exc}", flush=True)
    Thread(target=_alert_guard, args=(event, label), daemon=True).start()


def _alert_guard(event: dict, label: str = "") -> None:
    """Put the event in front of the operator, and never raise doing it."""
    from utils import operator_log  # noqa: PLC0415 — discord is not needed to serve a page

    try:
        _run_on_bot_loop(operator_log.ai_guard_alert(BOT_INSTANCE, event, label=label), timeout=20)
    except Exception as exc:
        print(f"[GUARD] could not report the event: {exc}", flush=True)


@app.before_request
def _refuse_ai_crawlers():
    """The two active halves of the anti-AI posture, before anything renders.

    Order is deliberate: the trap is checked first, so a declared crawler that
    wanders into one is recorded as what it is — a positive identification —
    rather than folded into the refusal counter. Then the token table, which is
    the cheap answer: a named AI crawler gets 403 and a paragraph naming the two
    documents it is choosing to ignore, on every path except the directives
    themselves. Nobody else is touched, which is the property that keeps this
    from becoming the thing that broke the site.
    """
    agent = request.headers.get("User-Agent", "")
    path = request.path

    label = ai_guard.trap(path)
    if label is not None:
        _note_guard("honeytoken", agent=agent, path=path, label=label)
        return Response(ai_guard.decoy(path), mimetype="application/json")

    token = ai_guard.crawler(agent)
    if token and ai_guard.refuses(path):
        _note_guard("crawler", agent=token, path=path)
        body = ai_guard.refusal_body()
        return Response(body, status=403, mimetype="text/plain; charset=utf-8")
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


#: The two pages this site answers with when there is nothing to answer with.
#:
#: Written as data rather than as two templates because they are one shape with
#: two sentences, and because the status code is the only part a crawler or a
#: monitor reads. Both are rendered by :func:`_error_page` in the site's own
#: shell, and the static build gets the same document by asking this process for
#: a path nothing serves — the rule this project already follows for every other
#: page: what a visitor reads on the CDN and what this process answers are one
#: template with one context.
#:
#: Before this, both surfaces failed and neither failure was this site's. The
#: process answered with Flask's own bare default — no navigation, no
#: stylesheet, no way back — and the published `404.html` was hand-written HTML
#: naming seven classes the current sheet does not define (`page-404`,
#: `brand-logo`, `hero-cta`, `btn-primary`, …) beside `static/logo.svg`, which the
#: redesign deleted. A mistyped address on the CDN therefore got a
#: broken-picture glyph, a heading in the browser's own font, and no way to
#: anywhere. A visitor who typed one character wrong met the least finished page
#: on the site.
ERROR_PAGES = {
    404: {
        "eyebrow": "Not found",
        "headline": "There is nothing at that address",
        "explanation": (
            "No page of this deployment answers it. A mistyped address is the "
            "usual reason, and nothing here is broken — the ways back are below."
        ),
    },
    500: {
        "eyebrow": "This side",
        "headline": "That did not work, and it is not you",
        "explanation": (
            "The bot could not answer that request. It has been written down where "
            "the operator reads them, so it is a known fault rather than a mystery. "
            "The pages that do work are below."
        ),
    },
}


def _error_page(status: int, *, explanation: str = ""):
    """One error page, in the site's own shell, for both surfaces.

    The ways onward are built from the deployment rather than written into the
    template, so an error page cannot offer a door this deployment does not
    have: the sign-in link is whatever :func:`_site_context` calls it, and
    Pricing appears only where anything can actually be bought.
    """
    page = ERROR_PAGES.get(status) or ERROR_PAGES[500]
    context = _site_context()
    ways = [
        {
            "label": "The front page",
            "href": "/",
            "note": "What this bot is, what is in it, and what it is doing right now.",
            "meta": "/",
        },
        {
            "label": f"Every system ({context['system_count']})",
            "href": "/systems",
            "note": "Each system, and exactly what switching one off does.",
            "meta": "/systems",
        },
        {
            "label": "Getting started",
            "href": "/start",
            "note": "Three steps, and the two invites side by side.",
            "meta": "/start",
        },
    ]
    if context.get("selling"):
        ways.append(
            {
                "label": "Pricing",
                "href": "/pricing",
                "note": "What a licence covers, and what it costs.",
                "meta": "/pricing",
            }
        )
    ways.append(
        {
            "label": context["enter_label"],
            "href": context["enter_url"],
            "note": "The dashboard, for a server that already has the bot.",
            "meta": "needs Discord",
        }
    )
    return (
        render_template(
            "error.html",
            **context,
            current="",
            code=status,
            ways=ways,
            eyebrow=page["eyebrow"],
            headline=page["headline"],
            explanation=explanation or page["explanation"],
        ),
        status,
    )


@app.errorhandler(404)
def _no_such_page(_error):
    """A mistyped address is the most common error page a visitor ever sees."""
    return _error_page(404)


@app.errorhandler(500)
def _broken_page(_error):
    """An unhandled exception, told to a visitor without a traceback.

    A public address raising a `TemplateNotFound` used to answer this process's
    own HTML shell — *"The server encountered an internal error"* — which tells a
    visitor nothing and looks like a host that has fallen over. The fault is on
    this side, and now the page says only that.
    """
    return _error_page(500)


@app.after_request
def set_security_headers(response):
    # The anti-AI reservation, on every response this process writes. It travels
    # in a header rather than only in `robots.txt` because a header reaches what a
    # file cannot: a JSON answer, an image, a PDF, a 403 — and because the
    # collectors that ignore the file are the ones whose log somebody may later
    # have to read. `private` follows the clickwrap's own split, so the pages
    # behind a sign-in are additionally `noindex`: they were never public, and a
    # collector's defence that it read only what was public is not one to leave
    # open on the pages that need it least.
    for name, value in ai_guard.headers(
        private=_agreement_stands_here(request.path)
    ).items():
        response.headers[name] = value
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
        # Whether the pictures and sounds this bot sends still exist. A fifth
        # separate claim, and the one with the least visible failure of all: a
        # dead link is not an error here, it is an empty box in somebody's chat
        # that Discord draws without complaint — which is how every roleplay GIF
        # in this tree answered 404 for who knows how long.
        #
        # Counts and no names, like the three above it: this endpoint is
        # unauthenticated, and the card that names the dead links goes to the
        # operator's own channel.
        "media": media_health.health_json(),
        # How many automated readers were refused for being what they declared
        # themselves to be, and how many fetched a page no human can reach. Two
        # numbers and a date, no agent, no path, no address — the ledger names
        # those, and it is read by the operator rather than by a stranger with a
        # monitor. A non-zero `traps` here is not a statistic; it is the thing
        # the operator is meant to act on.
        "anti_ai": ai_guard.totals(),
        # How many sign-ins have agreed to which revision of the policies in the
        # last 30 days. The revision is the content digest of the documents, so
        # this also answers "is the live text the one people agreed to" — the
        # question a clickwrap exists to make answerable.
        "consent": consent.totals(),
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
        if not _principal().is_operator:
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
        if not _principal().is_operator:
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
            # ``_player`` only exists on a player subclass of VoiceClient, and
            # this bot's radio does not wrap the client at all. Reading it
            # directly raised AttributeError on a plain client, so go through
            # getattr — this endpoint must report on any voice client without
            # assuming one.
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
    # The same probe the boot line and /radio doctor read, so this endpoint can
    # never report a different host than they do.
    try:
        from utils import voice_caps  # noqa: PLC0415

        caps = voice_caps.probe(refresh=True).to_dict()
    except Exception as exc:
        caps = {"playable": None, "why": f"probe failed ({type(exc).__name__})"}
    # The one reading a capability probe cannot give: whether this host can
    # actually open a station. It is asked here rather than read from the weekly
    # pass's last verdict because this endpoint's whole job is to answer for the
    # host *now* — and it reports the kind of failure, not just the fact of one.
    try:
        from utils import radio_stream  # noqa: PLC0415

        stream = radio_stream.self_check().to_dict()
    except Exception as exc:  # noqa: BLE001 — a diagnostic that 500s is not a diagnostic
        stream = {"healthy": None, "reason": f"the check could not run ({type(exc).__name__})"}

    return jsonify({
        "bot_ready": started,
        "gateway": _gateway_label(started, connected),
        "caps": caps,
        "stream": stream,
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
    """What the bot calls itself: the brand, and never the running application.

    This used to prefer the *live* Discord application's name, on the theory that
    renaming the application in the developer portal was what moved every page at
    once. The theory is wrong for this deployment, and the way it fails is the
    worst kind: the application behind this product is **verified**, and Discord
    refuses a rename from the API with ``APPLICATION_VERIFIED_SUPPORT_REQUIRED``,
    so the field the old code read still held the name this product retired —
    which is why ``/login``, ``/dashboard``, ``/market`` and ``/owner`` all
    announced *Meow* while every wordmark on those same pages said something
    else. A name with two authors is the failure `tests/test_brand.py` already
    writes a law about, so the name has one author, and it is the brand module:
    the value here is the same before a cold start, after a rename request, and
    on a process whose gateway has not connected yet.
    """
    return brand.NAME


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
        # come from the registry, in the units every other surface prints —
        # ``entry_count``, what a visitor can type, because these figures land on
        # pages whose prose says "110 commands" right beside them and a tile
        # answering "95" to the same word is the two-numbers failure again. The
        # tree is not read for them either way: the count someone is shown should
        # not depend on whether a gateway happened to be awake.
        "servers": None,
        "members": None,
        "systems": features.system_count(),
        "commands": features.entry_count(),
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


def _static_site() -> bool:
    """Is this the published copy rather than a running process?

    The status line below is a claim about *this process*. A static host serves
    files, so there is no process, nothing will ever connect, and "a sleeping
    free-tier host takes a moment to wake" becomes a promise that never resolves
    — a visitor reads a permanently half-started bot. Set only by
    ``scripts/build_site.py``; the bot never sets it.
    """
    return config_audit.flag("MEOW_STATIC_SITE")


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


def _needs_figures(*groups: list[dict]) -> bool:
    """Whether the page was drawn *without* a reading.

    True when anything on it is still the dash :data:`NOT_READ` stands for, which
    is what a build of the published site looks like: a file, no process, no
    gateway to count, so *servers* and *members reached* have no value to print.
    The host answers the same question `False` for, because it renders the
    numbers itself — and that is the whole point of the check: a page asks the
    machine only for the readings it does not already have, rather than asking
    again for an answer that is on it.
    """
    return any(tile["waiting"] for group in groups for tile in group)


#: A figure that was never read. An em dash, not a zero: the modules above go to
#: some trouble to report "I do not know" separately from "none", and a card that
#: printed `0 servers` for a sleeping host would undo that in the last step.
NOT_READ = "—"


def _tile(key: str, label: str, value: str) -> dict:
    """One figure card, and whether it is waiting on a reading.

    ``waiting`` is written where the value was computed rather than worked out
    again in a template, because it is the difference between a cell the
    published copy has to ask for and one it already has. A card that carried
    the hook with a number in it would invite a reader to overwrite a count the
    host had already made, which is the second author one value must not have.
    """
    return {"key": key, "label": label, "value": value, "waiting": value == NOT_READ}


def _control_tiles(live: dict) -> list[dict]:
    """The four figures the Control panel card leads with.

    These replaced `_home_stats()[:3]`, which was the same idea with the wrong
    three: *systems* and *commands* are true of every copy of this bot ever
    started, so a visitor learned nothing about whether anybody uses it. Servers
    and the people in them are the numbers that answer that, and they are the
    ones a cold host can only say "not yet" about.

    The front door's panel draws these same four, which is why the labels are
    the registry's words rather than the hero's copy: two pages naming one
    figure differently is how a product comes to look like two.
    """
    text = live.get("text") or {}
    return [
        _tile(key, label, text.get(key) or NOT_READ)
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
        _tile(key, label, window.get(key) or NOT_READ)
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
    live = config_audit.get("MEOW_LIVE_APP_URL").rstrip("/")
    proxied = config_audit.flag("CLOUDFLARE_PROXY")
    if _static_site() and live and not proxied:
        return f"{live}/api/figures"
    return "/api/figures"


#: What the **member dashboard's** own address offers in its header.
#:
#: The destinations of a header are the destinations of the *address* it is drawn
#: on, and this is the pair `dacatdashboard.pages.dev` answers: the overview and
#: the picker that says which server a session is about. Its frame used to offer
#: the public site's four — Home, Systems, Get started, Radio — of which **three**
#: that host answers with a 301 to `dacat.pages.dev` (measured on the live host:
#: `/systems`, `/start`, `/radio` and `/pricing` all 301, while `/dashboard` and
#: `/servers` 302 to the door because the reader is not signed in yet). So
#: "Systems" in the header of the app sent an administrator to the marketing
#: page *about* systems instead of the switches one rail-click away, and
#: "Get started" offered onboarding to somebody already inside. The console's own
#: header was given this treatment first (`templates/_owner_nav.html`); this is
#: the same rule for the third address.
#:
#: `tests/test_owner_site.py` checks every href here against the prefix list that
#: host's worker is built from, so an item that address does not answer fails the
#: suite rather than the reader.
MEMBER_FRAME_NAV = (
    ("dashboard", "Dashboard", "/dashboard"),
    ("servers", "Which server", "/servers"),
)


def _frame_nav() -> dict:
    """The header destinations the address answering this request offers.

    Empty everywhere but the member host, and empty is the point rather than a
    gap: it leaves each frame's own list — the public site's — in place for the
    marketing pages, for the frames as the offline site build renders them, and
    for a local run. The member app's four pages merge this into their render, so
    one template is right on the public address (where it wears the site's
    destinations, proxied and working) and on the dashboard's own (where it wears
    the app's), which is what a second copy of the list in a template could not
    be.
    """
    if not _on_member_host():
        return {}
    return {"nav_items": MEMBER_FRAME_NAV}


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
        # The fallback is the brand's own initial rather than a literal: this read
        # ``or "M"``, which is the initial of a name the product no longer uses,
        # so a page drawn before the gateway connected could letter its tile with
        # a retired brand.
        "bot_initial": (name[:1] or brand.INITIAL).upper(),
        # The footer's copyright, from the module that already owns the name and
        # names the same holder as the licence at the root of this tree.
        "copyright_holder": brand.COPYRIGHT_HOLDER,
        "copyright_year": brand.COPYRIGHT_YEAR,
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
        # Two honest numbers about commands, in one place: ``entry_count`` is what
        # a person can type (``/market buy`` is its own row) and ``command_count``
        # is what Discord's picker holds (``/market`` is one). A page prints one
        # and labels it; printing the picker's figure over a list of entries is
        # how the front door came to advertise 95 commands above a hundred and ten
        # of them, which reads as a page arguing with itself.
        "entry_count": features.entry_count(),
        "command_count": features.command_count(),
        "features": features.dashboard_modules(),
        # The difference between the two counts above, by name. A page that prints
        # a total and then a shorter list has to say what the difference is; the
        # alternative is a reader counting cards and finding one missing.
        "always_on": features.always_on_names(),
        # ...and the other half of the same sentence: the systems that arrive
        # *off*. The front door's own fifteen-row band used to carry this in a
        # drawer, which is why a page that only described `/radio` as switchable
        # left a visitor to find out by being refused.
        "ships_off": features.ships_off(),
        **_invite_context(),
    }


#: What each dashboard page is *for*, in the landing page's own words.
#:
#: Keyed by the page id in :data:`DASHBOARD_PAGES` rather than written out as a
#: list of its own, and that is the whole point: the tour on the front door walks
#: the dashboard's real pages, so a page added to the dashboard cannot go
#: unddescribed here and a page removed cannot linger. A page this mapping has no
#: entry for still appears, with a sentence built from its label — the failure
#: mode of forgetting one is "it is still there", never "it is missing".
LANDING_TOUR = {
    "overview": {
        "icon": "◎",
        "note": "This server, at a glance",
        "blurb": "What this server is doing right now, at a glance.",
        "points": (
            "Who is in voice, and who is sitting alone",
            "Which systems are on, and which are off",
            "The gateway's latency",
        ),
    },
    "systems": {
        "icon": "▦",
        "note": "One switch per system",
        "blurb": "One switch per system, per server.",
        "points": (
            "Every system, with its switch",
            "The commands a switch turns off",
            "A republish button for Discord's picker",
        ),
    },
    "members": {
        "icon": "☰",
        "note": "The roster and its moderation",
        "blurb": "The roster, and the moderation that goes with it.",
        "points": (
            "Search members, and read their warnings",
            "Warn, time out, kick or ban — or undo it",
            "A record of who asked for what",
        ),
    },
    "server": {
        "icon": "⛨",
        "note": "Lockdown, lift and slowmode",
        "blurb": "The whole server at once: lockdown, lift, slowmode.",
        "points": (
            "See the plan before it runs",
            "The permission the bot is missing, said up front",
            "What was locked, and when it was lifted",
        ),
    },
    "economy": {
        "icon": "▤",
        "note": "Wallets, banks and circulation",
        "blurb": "What this server's coins are doing.",
        "points": (
            "Wallets, banks and what is in circulation",
            "The largest holdings, and their share",
            "Read-only: nothing here moves a balance",
        ),
    },
    "leveling": {
        "icon": "⤴",
        "note": "XP, rank cards, level roles",
        "blurb": "Who is earning, and how far they have climbed.",
        "points": (
            "Members earning, against everyone seen",
            "The highest level, and the average",
            "How far the leader is into their next level",
        ),
    },
    "composer": {
        "icon": "✎",
        "note": "Write a message as the bot",
        "blurb": "Write a message as the bot, and see it first.",
        "points": (
            "A preview of how it will arrive",
            "The character and channel limits",
            "A channel chosen per server",
        ),
    },
    "schedule": {
        "icon": "◷",
        "note": "Queued messages and repeats",
        "blurb": "Queue a message, once or on a repeat.",
        "points": (
            "One-off, or every day, week or month",
            "Messages the bot can write itself",
            "Cancel or send one by hand",
        ),
    },
    "identity": {
        "icon": "☺",
        "note": "The bot's name and picture",
        "blurb": "The bot's name and picture in this server.",
        "points": (
            "A per-server nickname and bio",
            "An avatar and banner",
            "What Discord shows you, read back",
        ),
    },
    "commands": {
        "icon": "⌘",
        "note": "Which commands this server keeps",
        "blurb": "Your switches, against what Discord actually holds.",
        "points": (
            "Every command the bot declares",
            "Every command Discord's picker has",
            "What is missing, by name",
        ),
    },
    "status": {
        "icon": "◈",
        "note": "Latency, uptime and errors",
        "blurb": "This deployment's own health, on the page.",
        "points": (
            "Uptime, and the last site check",
            "A configuration audit that names what is missing",
            "The gateway's own badge",
        ),
    },
}

#: The questions the front door answers, in the order it answers them.
#:
#: Authored here rather than in the template because three of the six are claims
#: about *the deployment* — the retention window, whether Discord sign-in is on,
#: and what the market is priced from — and a template that hard-coded them would
#: keep saying them after the deployment changed.
LANDING_QUESTIONS = (
    (
        "Does the bot need administrator permission?",
        # The answer has to agree with the step two bands up, because a visitor
        # reads both before deciding: the invite the page offers really does ask
        # for Administrator (`discord_auth.INVITE_PERMISSIONS`), and the smaller
        # grant is the second link beside it. The answer here used to say the
        # invite asks "for what the systems you keep need", which is true of the
        # minimal link and false of the one the button uses — two permissions
        # claimed for one URL, on one page, in the same screen.
        "Not to work. The invite asks for Administrator — one bit, and the reason no "
        "feature ever answers “Missing Permissions” — with a smaller grant beside "
        "it if you would rather hand over less.",
    ),
    (
        "What happens when I switch a system off?",
        "Its commands leave that server's picker. Nothing is deleted, and switching "
        "it back on puts them back.",
    ),
    (
        "Where is my data, and for how long?",
        "In this deployment's own database, for the number of days printed in the "
        "footer.",
    ),
    (
        "Do I have to make an account?",
        "No. Discord sign-in, and it shows only the servers you already administer.",
    ),
    (
        "Is the market real money?",
        "No. Real prices, the bot's own coins — nothing on it costs money.",
    ),
    (
        "What if the host is asleep?",
        "These pages come from a host that never sleeps. The live figures wake the "
        "bot when a page asks, and say so while they do.",
    ),
)


def _landing_systems() -> list[dict]:
    """The systems the front door's preview panel names, from the registry itself.

    All of them, not the switchable subset: the page's closing sentence counts
    both, and a list that quietly dropped the always-on ones would be the same
    defect the figures band was fixed for — two numbers about one word,
    disagreeing. The panel draws the first four and links to `/systems` for the
    rest, which is where the whole list lives.

    Every row carries the registry's own **default state**, because the grid
    drawn from these rows is the dashboard's grid as a server that has never
    been configured sees it. Left out, the front door assumed "on" for every
    system — which was true until four of them started shipping off, and was
    then a page showing Market and Starboard lit while `/systems` beside it said
    they arrive switched off. One author for the default: `default_enabled`.
    """
    return [
        {
            "id": feature.id,
            "name": feature.name,
            "icon": feature.icon,
            "category": feature.category,
            "desc": feature.description,
            "commands": [str(command) for command in feature.commands],
            "toggleable": bool(feature.toggleable),
            "enabled": features.default_enabled(feature.id),
            "command_count": len(feature.commands),
        }
        for feature in features.FEATURES
    ]


def _command_groups() -> list[dict]:
    """Every system with its full command list, as one collapsed group each.

    The reference page's accordions, built from :func:`features.feature_entries`
    — the one reader of what a system contains, so this page cannot disagree with
    the figure printed above it. A group carries its ``feature.id`` because the
    anchor is an address: ``/systems#cmd-economy`` opens that group, and anyone
    who has one of those links keeps a working one.
    """
    groups: list[dict] = []
    for feature in features.FEATURES:
        rows = []
        for name, note in features.feature_entries(feature):
            rows.append({
                "name": name,
                "search": " ".join(
                    part for part in (name, note, feature.name, feature.category, feature.id)
                    if part
                ).lower(),
            })
        groups.append({
            "id": feature.id,
            "icon": feature.icon,
            "name": feature.name,
            # The two facts the page's *other* systems list used to carry, on the
            # row that now carries the name too. They are read from the registry
            # rather than written into the template for the reason every other
            # reader here exists — a category retyped in a template is a second
            # copy of the registry, and the copy is the one that goes stale — and
            # they are here rather than in a list of their own because the two
            # lists were the same fifteen rows, in the same order, twice.
            "category": feature.category,
            "toggleable": bool(feature.toggleable),
            "rows": rows,
        })
    return groups


def _landing_tour() -> list[dict]:
    """The dashboard's own pages, described for the front door's tour."""
    rows = []
    for page_id, label, path, _anchor in DASHBOARD_PAGES:
        entry = LANDING_TOUR.get(page_id) or {}
        rows.append({
            "id": page_id,
            "label": label,
            "path": path,
            "icon": entry.get("icon") or "▸",
            # Two lengths of one description on purpose: ``blurb`` is the sentence
            # the one card reads, ``note`` is what the scan line beside it prints.
            # A four-word note is not a shorter sentence, it is a different job —
            # and the fallback is a sentence so that a page nobody wrote a note for
            # fails the law that reads this rather than printing three words that
            # happen to look like a note.
            "note": entry.get("note") or f"The {label.lower()} page of the dashboard.",
            "blurb": entry.get("blurb") or f"The {label.lower()} page of the dashboard.",
            "points": list(entry.get("points") or ()),
        })
    return rows


#: The refusal a member gets when a system is off — **the bot's own sentence**.
#:
#: ``bot.global_module_check`` composes it on the interaction path, and it cannot
#: be imported here: importing ``bot`` starts a gateway. So the words are written
#: once in this file and `tests/test_meoww_site.py` compares them against
#: `bot.py`'s own source. A front door that paraphrases the bot is a front door that
#: lies about what a member will see, which is the one thing the demo band must
#: not do.
SIM_OFF_SENTENCE = "⚠️ The {name} system is currently turned off on this server."
SIM_OFF_HINT = "An administrator can re-enable it instantly in the Web Dashboard."


def _sim_payload() -> dict:
    """What the front door's command demo needs, from the bot's own tables.

    Two facts, and each already has exactly one author — which is the whole reason
    this is built here rather than written into the template:

      * **Which commands exist.** ``permissions.COMMAND_ACCESS`` is the audience
        table whose coverage of the real command tree is enforced elsewhere; its
        roots are this product's command list, not a second copy of it.
      * **Which system owns one.** ``features.command_gates()`` is the map
        ``bot.global_module_check`` itself reads on the interaction path, so the
        demo's answer to "is this on" is the answer the bot would give.

    Their union is checked as it is built: a real command that is in neither table
    would be called "not a command" by the demo, and the demo's only claim is that
    it recognises the real ones. A registry edit that lands in that gap is a
    build-time failure here rather than a quiet lie on the front page.

    The system *names* come from the same registry the switches beside it are
    drawn from, so a refusal names the system a server's own dashboard would name.
    """
    names = {feature.id: feature.name for feature in features.FEATURES}
    gated = {root: list(ids) for root, ids in features.command_gates().items()}
    always = sorted(features.ALWAYS_ALLOWED_COMMANDS)
    known = {str(name).split(" ")[0] for name in permissions.COMMAND_ACCESS}
    missing = known - set(gated) - set(always)
    if missing:
        raise ValueError(
            "the command demo would refuse real commands: " + ", ".join(sorted(missing))
        )
    for ids in gated.values():
        for feature_id in ids:
            if feature_id not in names:
                raise ValueError(f"a gate names a system the registry does not have: {feature_id}")
    return {
        "names": names,
        "gates": gated,
        "always": always,
        "count": len(known),
        "off": SIM_OFF_SENTENCE,
        "hint": SIM_OFF_HINT,
    }


def _landing_context() -> dict:
    """Everything the rebuilt front door draws, read once.

    The page's whole claim is "this is what is in it, and here is what each piece
    does" — so every list on it comes from a registry rather than from prose: the
    systems the preview panel names from :mod:`utils.features`, the tour from
    :data:`DASHBOARD_PAGES`. That is what makes it impossible for the front door
    to advertise a system or a page that does not exist, which is the defect this
    project has shipped twice.

    What is *not* read here is as much of a decision: the whole systems list and
    the whole command list are one click away on `/systems`, which renders them
    from the same registry, so this function does not build a second copy for a
    band that would be a third of the page's height.
    """
    systems = _landing_systems()
    return {
        "systems": systems,
        "switchable_count": len(features.dashboard_modules()),
        # ``entry_count`` is deliberately not here: it is a figure about the
        # registry, so it lives in :func:`_site_context` with the other one, and
        # the front door spreads that context before this mapping. Spelling it
        # here as well was a `TypeError` on the next request — two authors for
        # one name — which is the failure this pair of functions exists to avoid.
        "tour": _landing_tour(),
        # The command demo's own tables: which commands exist and which system
        # owns each. See :func:`_sim_payload` — the switches it sits beside come
        # from ``systems`` above, and this adds no third list of systems.
        "sim": _sim_payload(),
        "faq": [{"question": question, "answer": answer} for question, answer in LANDING_QUESTIONS],
    }


def _home_page():
    """The front door: one deep page that answers its own questions in place.

    It used to be the shortest of three pages, because every question lived one
    click away — the systems list on `/systems`, the steps on `/start`. That is a
    front door that explains nothing to a visitor who will not click twice, so
    the answers moved here with the interactions that make them legible: walk the
    dashboard's pages, open a question. The two lists are the part that stayed
    where they belong: `/systems` carries every system and every command, and
    this page names four of them and links to it — a band of all fifteen under
    the hero was a third of the front door's height and a second place to keep a
    list, which is the copy that goes stale.

    The panel beside the hero draws the *same four cards* `/systems` opens with,
    from the same reader, rather than a second set of its own figures. What it
    used to draw was `_home_stats()`: the registry's two counts, which the hero's
    own meta line prints three inches away, and — on a host that had just woken —
    nothing else, under a line that read *Sample data, nothing here is connected*.
    Servers and members reached are the numbers a visitor cannot get anywhere else
    on this page, so they are the ones the panel leads with, dashed rather than
    zeroed until this deployment answers for itself.
    """
    # Whether this copy has a process behind it: `utils.figures.deployment`
    # refuses the tracker on a build machine, so the two cards it *can* fill in
    # (`servers`, `members reached`) are the two the panel has to ask for.
    live = _public_figures(read_tracker=not _static_site())
    hero_figures = _control_tiles(live)
    figures_url = _figures_url() if _needs_figures(hero_figures) else ""
    return render_template(
        "home.html",
        **_site_context(),
        **_landing_context(),
        # Which nav link is this page. The server knows, so the rail says so with
        # `aria-current` and no script can disagree with it.
        current="home",
        status=_home_status(),
        hero_figures=hero_figures,
        figures_url=figures_url,
        # The panel's closing line, and the two things it has to be true about:
        # which half of the panel is a drawing, and where the numbers came from.
        # It used to read *Sample data — nothing here is connected*, which was
        # honest about the first draft and became the one line a front door
        # cannot afford the moment the panel started reading the deployment.
        panel_note=(
            "The switches are a drawing; the counts are read from this deployment."
            if figures_url
            else "The switches are a drawing; the counts are this deployment's own."
        ),
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
    static_copy = config_audit.get("MEOW_STATIC_SITE") == "1"
    live_figures = _public_figures(read_tracker=not static_copy)
    periods = live_figures.get("periods") or []
    # The window the Activity card opens on. Seven days, because it is the one a
    # visitor can read as "lately" without being told how old the deployment is,
    # and because it is the window the weekly digest uses.
    opened = next((p for p in periods if p["id"] == "week"), periods[0] if periods else {})
    opened_id = opened.get("id") or "week"
    # The two cards' figures, built here rather than inside the call: whether the
    # page was drawn with them decides whether it asks the machine for them.
    control_tiles = _control_tiles(live_figures)
    activity_tiles = _activity_tiles(live_figures, opened_id)
    return render_template(
        "systems.html",
        **_site_context(),
        current="systems",
        # What the deployment is actually doing, as counts — the figures the two
        # preview cards draw. Read once here and again by `/api/figures` for the
        # published copy, from the same cached snapshot, so the page and the
        # endpoint cannot disagree about the same minute.
        control_tiles=control_tiles,
        activity_tiles=activity_tiles,
        activity_periods=[
            {"id": p["id"], "label": p["label"], "selected": p["id"] == opened_id}
            for p in periods
        ],
        # Which window the activity tiles are drawn from, so the reader that
        # fills them in can name the same one: `activity:<period>:<key>`.
        activity_period_id=opened_id,
        command_groups=_command_groups(),
        # Empty unless this copy was drawn without a reading — the published site
        # is a file with no process behind it, and its two dashes were permanent
        # until this: `/api/figures` answers the same numbers through the Pages
        # proxy, and the page now asks for the ones it is missing.
        figures_url=_figures_url() if _needs_figures(control_tiles, activity_tiles) else "",
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


# How many stations the page opens with. A table is not a wall: every row is in
# the document, two dozen of them are on screen, and the search and the two
# filters reach all of them. The number is here rather than in the template
# because the tests hold the page and the sheet to it.
STATION_PREVIEW = 24


def _station_rows(stations: list) -> list[dict]:
    """The catalogue as rows the page can filter, one per station.

    Read out of :mod:`utils.radio_catalogue` rather than written down, for the
    reason every list on this site is read from its registry: a page that names
    a station the bot would refuse to play is the failure those readers exist to
    prevent. Each row carries its own haystack — name, description, genre,
    country, language and the ad-free tag, lowercased — so the page's search has
    one author and the template has none, which is the shape
    :func:`_command_groups` already uses for the command reference.
    """
    rows: list[dict] = []
    for station in stations:
        ad_free = "ad-free" in (getattr(station, "tags", ()) or ())
        rows.append({
            "name": station.name,
            "icon": station.icon,
            "desc": station.desc,
            "genre": station.genre,
            "country": station.country,
            "language": station.language,
            "ad_free": ad_free,
            "search": " ".join(
                part for part in (
                    station.name,
                    station.genre,
                    station.country,
                    station.language,
                    *(getattr(station, "tags", ()) or ()),
                ) if part
            ).lower(),
        })
    return rows


def _radio_page():
    """Radio, on the site: what it does, and what it refuses to pretend.

    The command list is read out of ``utils/features`` rather than written here,
    for the reason every other page reads the registry — a page that lists a
    command the bot does not have is the failure this project keeps meeting. The
    table comes from :mod:`utils.radio_catalogue` for the same reason, and it is
    read rather than written so the page cannot advertise a station the bot would
    refuse to play.

    The page's last section is the one that makes it worth publishing: a stream
    can still go quiet, and what the bot does about it — ask again, then say so —
    is the part a visitor cannot discover until it happens to them.
    """
    # The registry holds **one** feature for radio and the question box — the
    # legacy module — so the radio commands are the `/radio` half of it. This
    # looked the id `"radio"` up, which no feature has answered to since those two
    # became one flag: `features.get` answers ``None`` rather than raising, the
    # template reads ``radio_commands`` as empty, and the page's own section —
    # headed *One group, three faces* — shipped with **no commands in it at all**,
    # on the page a visitor reads to decide whether to ask for the system.
    feature = features.get("legacy")
    entries = features.feature_entries(feature) if feature else ()
    # Just the radio three: the switch also carries `/qna`, and this page is the
    # radio's. The name is what a person types, so the note after the em dash is
    # the template's own sentence rather than a second copy of the registry's.
    radio_commands = [
        name for name, _note in entries if name.lstrip("/").split()[0] == "radio"
    ]
    stations: list = []
    index = None
    try:
        from utils import radio_catalogue  # noqa: PLC0415

        stations = list(radio_catalogue.CATALOGUE)
        index = radio_catalogue.index()
    except Exception as exc:
        print(f"[SITE] could not read the stations for /radio: {exc}", flush=True)

    rows = _station_rows(stations)
    # The counts and the two filter menus come from the catalogue's own index,
    # so the page cannot offer a language or a country its picker does not
    # count. A table that could not be read at all falls back to the rows, which
    # keeps the page's own arithmetic true rather than printing zeroes.
    station_languages = (
        [name for name, _ in index.languages] if index
        else sorted({row["language"] for row in rows if row["language"]})
    )
    station_countries = (
        list(index.countries) if index
        else sorted({row["country"] for row in rows if row["country"]})
    )

    return render_template(
        "radio.html",
        **_site_context(),
        current="radio",
        meta_description=(
            f"Radio in {_bot_display_name()}: 24/7 stations in every language and "
            "country the table could verify, one command for all of it, and an "
            "honest word about what can go wrong."
        ),
        radio_commands=radio_commands,
        stations=stations,
        station_rows=rows,
        station_preview=STATION_PREVIEW,
        station_languages=station_languages,
        station_countries=station_countries,
        index=index,
    )


@app.route("/radio")
def radio_page():
    return _radio_page()


@app.route("/music")
def music_page():
    """The address this page used to have, kept alive.

    A redirect rather than a second route because a URL somebody bookmarked is
    not a mistake to punish: the page moved, and 301 is the sentence for that.
    """
    return redirect(url_for("radio_page"), code=301)


@app.route("/")
def home():
    user_agent = request.headers.get("User-Agent", "").lower()
    if "uptimerobot" in user_agent or "curl" in user_agent:
        # Uptime monitors are pointed at this process and parse this exact line;
        # the landing page above is for people, and a monitor must not be made to
        # fetch a whole document to learn one word.
        return f"{brand.NAME} is alive and operational!"
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
    # The third document, and the only one that is *not* an agreement: it says
    # what this deployment does about automated collection, which is a statement
    # about the operator rather than a condition on the visitor. It is on the same
    # page template, from the same file, for the reason the other two are — one
    # policy, readable in a browser and quotable in a complaint.
    "ai": {
        "filename": "AI_POLICY.md",
        "eyebrow": "AI & crawling",
        "command": "/ai",
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
        # A missing document is this deployment's fault rather than the
        # reader's, and it is the reader who is looking at it — so it is the
        # error page's own sentence on the error page's own shell, instead of a
        # bare `<h1>` fragment in the browser's default font.
        return _error_page(
            404,
            explanation=(
                f"{_html.escape(document['filename'])} is missing from this "
                "deploy, so this page has nothing to show. In Discord the "
                f"{document['command']} command reads the same file and says "
                "the same thing."
            ),
        )
    # The frame comes from `_site_context()` — including the count the footer
    # states and the flag that decides whether Pricing exists — and only the
    # document itself is this page's own business.
    return render_template(
        "legal.html",
        **_site_context(),
        current=slug,
        body=webdocs.render(text),
        title=webdocs.title_from(text, document["filename"]),
        eyebrow=document["eyebrow"],
        command=document["command"],
        effective=_effective_date(text),
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


@app.route("/ai")
def policy_ai():
    return _policy_page("ai")


# ---------------------------------------------------------------------------
# What automated readers are told
# ---------------------------------------------------------------------------
# One source, four surfaces. ``utils.ai_guard`` builds the file and the app
# serves it; each static build writes the *same* function's output, so the
# Render host, the marketing site, the hub and the owner console cannot end up
# with four different answers to one question. Before this, two of those hosts
# published `User-agent: * / Allow: /` and nothing else — which is the state
# every deployment drifts into when each one writes its own file by hand.
#
# The trap routes below are real routes on purpose. Flask resolves the URL
# *before* the ``before_request`` hooks run, so a declared trap with no route is
# a 404 and this hook is never reached: the paths would have been listed, in the
# directives and in the footer of every page, and served nothing. They exist so
# that the hook and the route agree, whichever one answers.
@app.route("/robots.txt")
def robots_file():
    """This host's directives. No sitemap: this process publishes none."""
    return Response(ai_guard.robots_txt(), mimetype="text/plain; charset=utf-8")


@app.route("/ai.txt")
@app.route("/.well-known/ai.txt")
def ai_file():
    """The training prohibition, in the form a crawler can read."""
    return Response(ai_guard.ai_txt(), mimetype="text/plain; charset=utf-8")


def _trap_view():
    """A declared trap, answered as the decoy it is.

    Reached only when the guard hook did not already answer — which is the
    point: two mechanisms, one of which is the request path doing the work. The
    hook records and identifies; this is the floor under it.
    """
    return Response(ai_guard.decoy(request.path), mimetype="application/json")


for _trap_index, (_trap_path, _trap_label) in enumerate(ai_guard.HONEYTOKENS):
    app.add_url_rule(_trap_path, endpoint=f"trap_{_trap_index}", view_func=_trap_view)


@app.route("/pricing")
def pricing_page():
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
    #
    # The frame is `_site_context()` and only the prices are this page's. It
    # used to be spelled out here — six frame names, hand-copied — and the one
    # it left out was `command_count`, so the page read *"15 systems and
    # commands"* and the footer lost its number too. A price list is the wrong
    # place to discover that the frame has two authors.
    checkout_pro = plans.checkout_url(plans.PRO)
    checkout_enterprise = plans.checkout_url(plans.ENTERPRISE)
    return render_template(
        "pricing.html",
        **_site_context(),
        current="pricing",
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
    static_copy = config_audit.get("MEOW_STATIC_SITE") == "1"
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
        # The footer's line, from the same module the site's footer reads — the
        # hub is a fifth build of one product and its copyright is not its own.
        "copyright_holder": brand.COPYRIGHT_HOLDER,
        "copyright_year": brand.COPYRIGHT_YEAR,
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
            _begin_session()
            session["authenticated"] = True
            session.permanent = True
            session["auth_scope"] = (
                principal.SCOPE_GUILD if meta.get("guild_id") else principal.SCOPE_GLOBAL
            )
            if meta.get("guild_id"):
                session["guild_id"] = meta["guild_id"]
            if meta.get("user_id"):
                session["user_id"] = meta["user_id"]
            _record_session(label="one-time link")
            get_csrf_token()
            return redirect(_door_target())
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
            _SIGN_IN.note_refused()
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
            _begin_session()
            session["authenticated"] = True
            session.permanent = True
            session["auth_scope"] = principal.SCOPE_GLOBAL
            _record_session(label="passkey")
            get_csrf_token()
            return redirect(_door_target())

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
        # The ledger row is closed before the cookie is dropped. A sign-out that
        # only forgot the token would leave a revocable row with no browser
        # beside it, which is a thing a session list should not have to explain.
        sessions.revoke(session.get(sessions.COOKIE_KEY))
        session.clear()
        return redirect("/login")
    return redirect("/dashboard" if is_authenticated() else "/login")


# ---------------------------------------------------------------------------
# Agreeing to the terms, on purpose
# -------------------------------------------------------------------
#: Where each document can be read before deciding. The key is the file, so a
#: document added to ``consent.DOCUMENTS`` without a page fails loudly in the
#: test that walks this mapping rather than rendering a link to nowhere.
AGREE_HREFS = {
    "TERMS.md": "/terms",
    "PRIVACY.md": "/privacy",
    "AI_POLICY.md": "/ai",
}


def _agree_notice(*, declined: bool, written: bool, error: str | None) -> tuple[str, str]:
    """The sentence the page opens with, and whether it is bad news.

    Three states, and the wording of each is the point. **Declined** is not an
    error and is never dressed as one: somebody who said no is told exactly what
    that means and how to change their mind, in the same size of type as the
    sentence that would have greeted an acceptance. **Unwritten** says that the
    agreement happened and the *record* of it did not. A failure to write is the
    one case where a page could quietly lie, so it does not: the person agreed,
    and the page says which half of that is in doubt.
    """
    if error:
        return error, True
    if declined:
        return (
            "You are signed out, and nothing about your servers was changed. "
            "The pages you were using need an agreement, so they will ask again "
            "next time you sign in — and you can read the three documents below "
            "without signing in at all."
        ), False
    if not written:
        return (
            "Agreed, and then reloaded — but the record of it could not be "
            "written on this host, so the ledger is missing an entry. Your "
            "access is unaffected; the operator has this on the console's "
            "Access page."
        ), True
    return "", False


def _door_target(default: str = "/dashboard") -> str:
    """Where a sign-in goes next: where it was headed, or the agreement first.

    **This is the clickwrap, and it is deliberately at the door rather than on
    every request.** A signed-in session that has not agreed is sent to
    ``/agree`` *by the route that signed it in* — the passkey form, the one-time
    link, Discord's callback, the owner console's form — so the terms are shown
    at the moment access is granted, which is the moment a reasonable person
    expects them and the moment a collector cannot describe as something it
    missed. A gate on every request instead would have been a second, weaker
    thing: it asks the same question of a session that already answered it, and
    a background fetch that receives a redirect learns nothing from it.

    What it does not do is ask a *visitor*: the public pages, the policies, the
    status endpoint and the directives are all reachable signed out, because a
    policy behind an acceptance wall is worse than no policy at all.
    """
    if not consent.needs(session):
        return default
    target = _safe_next(default) or "/dashboard"
    return f"/agree?next={urllib.parse.quote(target)}"


def _agree_page(
    *, next_url: str, error: str | None = None, declined: bool = False, written: bool = True, status: int | None = None
):
    """The clickwrap page: three documents, one decision, two equal buttons."""
    notice, bad = _agree_notice(declined=declined, written=written, error=error)
    body = render_template(
        "agree.html",
        **_site_context(),
        # The clickwrap is reached from both member addresses and from the
        # console's own host; each draws its own header (see `_frame_nav` and
        # `_console_frame_nav`). `_frame_nav` answers the member app's list and
        # nothing on the console's host, which is why the second reader exists:
        # without it this page wore the public site's marketing nav on the
        # console, the one host where every one of those links is a 301.
        **_frame_nav(),
        **_console_frame_nav(),
        current="",
        csrf_token=get_csrf_token(),
        documents=[
            {
                "title": title,
                "href": AGREE_HREFS[filename],
                "label": consent.document_label(filename),
            }
            for filename, title in consent.DOCUMENTS
        ],
        revision=consent.version(),
        ledger=consent.writable(),
        next_url=next_url,
        notice=notice,
        notice_bad=bad,
        declined=declined,
    )
    return (body, status) if status else body


@app.route("/agree", methods=["GET", "POST"])
def agree():
    """Ask for an agreement, and record the answer.

    Sign-in-wrap rather than browsewrap: the terms are shown at the door and an
    explicit act is what opens it. Three rules, and they are the reason this is a
    route rather than a checkbox on the sign-in form:

    * **The two answers are the same size.** Same class, same width, same
      type — one button says what agreeing to each document means, the other
      says what declining means. Neither is pre-selected, nothing is hidden, and
      there is no countdown, no nagging and no guilt: an interface that makes
      "no" hard produces a consent that is not consent.
    * **Declining is a real door.** It signs the session out, says so plainly,
      and leaves the documents readable without it. Nothing is retained from a
      refusal except the fact that the person is signed out.
    * **An old agreement is not a standing agreement.** ``consent.needs`` compares
      the *content digest* of the documents, so the sentence somebody accepted in
      June does not cover a clause added in September. That is what makes this a
      clickwrap rather than a formality.
    """
    if not is_authenticated():
        return redirect("/login")
    live = consent.version()
    next_url = _safe_next(request.args.get("next") or request.form.get("next")) or "/dashboard"
    if not consent.needs(session):
        return redirect(next_url)

    if request.method == "POST":
        if not validate_csrf():
            return _agree_page(
                next_url=next_url,
                error="That form had expired. Nothing was recorded — try again.",
                status=400,
            )
        decision = (request.form.get("decision") or "").strip()
        if decision == "accept":
            written = consent.accept(
                scope=session.get("auth_scope") or "",
                guild_id=session.get("guild_id"),
                user_id=session.get("user_id"),
                label=str((session.get("discord_user") or {}).get("name") or ""),
                ip=_client_ip() or "",
                agent=request.headers.get("User-Agent", ""),
            )
            # Marked either way. The person agreed; a ledger this host could not
            # write is a fact about the host, and refusing them entry over it
            # would punish the wrong party for it.
            if live:
                session[consent.SESSION_KEY] = live
            if not written:
                return _agree_page(next_url=next_url, written=False)
            return redirect(next_url)
        if decision == "decline":
            sessions.revoke(session.get(sessions.COOKIE_KEY))
            session.clear()
            return _agree_page(next_url=next_url, declined=True)
        return _agree_page(
            next_url=next_url,
            error="Neither answer arrived with that form — nothing was recorded. Try again.",
            status=400,
        )

    return _agree_page(next_url=next_url)


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
    _begin_session()
    # Ids only — see ``_visible_guilds`` for why the names are not kept here.
    session["authenticated"] = True
    session.permanent = True
    user_id_int = _int_or_none(user.get("id"))
    session["user_id"] = user_id_int
    is_owner_user = _is_owner_user(user_id_int)
    # ``owner_candidate`` is a claim about *who this is*, and it buys the copy on
    # the sign-in page — never the console. Scope is granted by the passkey and
    # by nothing else, unless a deployment has deliberately switched that off.
    session["owner_candidate"] = is_owner_user
    granted = (
        principal.SCOPE_GLOBAL
        if (is_owner_user and OWNER_DISCORD_ELEVATION)
        else principal.SCOPE_USER
    )
    session["auth_scope"] = granted
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
    _record_session(label=session["discord_user"]["name"])
    get_csrf_token()
    print(
        f"[DASHBOARD] {session['discord_user']['name']} signed in — {len(servers)} "
        "server(s) they administer that this bot is in.",
        flush=True,
    )
    if granted == principal.SCOPE_GLOBAL:
        # The member dashboard, and **not** the console. Discord returns the
        # browser to the host that asked — ``dashboard_base_url()`` follows the
        # request's own ``X-Forwarded-Host`` — so this callback only ever arrives
        # on a member-facing host, and none of those serves ``/owner``:
        # ``dacatdashboard.pages.dev/owner`` is a 301 to the public site, which
        # is another 301 to the console's host, where the session cookie minted
        # here does not exist, because a cookie is host-scoped. The operator
        # signed in and arrived at a passkey form, signed out — one host further
        # from the dashboard they clicked. The console is a host of its own with
        # its own door (`/owner/login`, the passkey), and a `next` still wins, so
        # that host can ask for `/owner` by name on the day it offers a Discord
        # sign-in: see `DASHBOARD_PREFIXES` in `scripts/build_owner.py` for the
        # other half of this rule.
        return redirect(_door_target(next_path or "/dashboard"))
    if len(servers) == 1:
        session["guild_id"] = int(servers[0]["id"])
    if len(servers) == 1 or next_path:
        return redirect(_door_target(next_path or "/dashboard"))
    return redirect(_door_target("/servers"))


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
    operator = _principal().is_operator
    servers = [
        {**entry, "icon": _guild_icon(entry["id"])} for entry in _visible_guilds()
    ]
    if operator:
        # The operator's reach is every server the bot is in, so the list says
        # what each of them is currently running. Choosing which server to open
        # is otherwise a guess from a list of names.
        for entry in servers:
            entry["scope"] = _module_summary(entry["id"])
    body = render_template(
        "servers.html",
        # The invite in the frame is offered even with nothing to pick: a person
        # whose servers do not have the bot yet is exactly who needs this page.
        **_site_context(),
        **_frame_nav(),
        # `servers` is an id in the member host's header and in no other list,
        # so this marks the right item where the app's nav is drawn and nothing
        # where the public site's is.
        current="servers",
        servers=servers,
        error=error,
        operator=operator,
        user=session.get("discord_user") or {},
        current_guild_id=_int_or_none(session.get("guild_id")),
        csrf_token=get_csrf_token(),
    )
    return (body, status) if status else body


@app.route("/servers", methods=["GET", "POST"])
def servers():
    """Pick which server the dashboard is looking at."""
    if not is_authenticated():
        return redirect("/login")
    if _principal().is_scoped_link:
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


def market_context(instrument) -> dict:
    """Everything ``market.html`` needs, in one place.

    The route and ``tests/test_market_page.py`` both call this, so the test that
    renders the template in a bare Jinja environment cannot pass while the route
    hands over something different — which is the failure it exists to catch:
    a page that renders in the tooling and raises in production.
    """
    from utils import market as market_module

    return {
        "symbol": instrument.symbol,
        "name": instrument.name,
        "kind": instrument.kind,
        "periods": list(market_module.PERIODS.items()),
        "default_period": market_module.DEFAULT_PERIOD,
        "instrument_groups": _market_groups(market_module),
        "instrument_count": len(market_module.INSTRUMENTS),
        "treat_fee_label": market_module.treat_fee_label(),
        # The ticket's own limits, read from the bot rather than written into the
        # page: the fill window is the bot's (`MIN_TRADE_COINS`,
        # `MAX_TRADE_COINS`), and a page that offers an amount the bot refuses is
        # offering a transaction that cannot happen. The shortcut buttons are
        # built here for the same reason — the hand-written set ended in "1M",
        # which is four times the largest fill this market has ever allowed.
        "min_trade": market_module.MIN_TRADE_COINS,
        "max_trade": market_module.MAX_TRADE_COINS,
        "default_spend": min(max(10_000, market_module.MIN_TRADE_COINS), market_module.MAX_TRADE_COINS),
        "quick_sizes": _market_quick_sizes(
            market_module.MIN_TRADE_COINS, market_module.MAX_TRADE_COINS
        ),
        # The rail is rendered by the server, so the board is on the page before
        # any script runs; the things a template cannot know — which instrument
        # this is, which period it opened on, and what the bot will fill — ride
        # along here.
        "boot_json": _inline_json(
            {
                "symbol": instrument.symbol,
                "period": market_module.DEFAULT_PERIOD,
                "minTrade": market_module.MIN_TRADE_COINS,
                "maxTrade": market_module.MAX_TRADE_COINS,
            }
        ),
    }


def _market_quick_sizes(minimum: int, maximum: int) -> list[dict]:
    """The order ticket's shortcut amounts, inside the bot's own fill window.

    They are computed rather than written into the template because the two
    numbers have to agree and only one of them is a design decision: the window
    is the bot's, and a shortcut outside it puts a command on the clipboard that
    Discord refuses — which is exactly what "1M" did, against a
    ``MAX_TRADE_COINS`` of 250,000.
    """
    picks = [1_000, 10_000, 100_000, maximum]
    rows: list[dict] = []
    for value in picks:
        if not minimum <= value <= maximum or any(row["value"] == value for row in rows):
            continue
        label = f"{value // 1_000_000}M" if value >= 1_000_000 else f"{value // 1_000}k"
        rows.append({"value": value, "label": label})
    return rows


@app.route("/market/<symbol>")
def market_page(symbol: str):
    """The live, interactive chart for one instrument."""
    from utils import market as market_module

    raw = str(symbol or "")
    instrument = market_module.BY_SYMBOL.get(raw.strip().upper())
    if instrument is None:
        safe = _html.escape(raw, quote=True)
        # A shell, because a mistyped ticker is the likeliest way to arrive: the
        # bare `<h1>` this used to return was an unstyled white page with one
        # link and no way back to the board, the dashboard or the site — a dead
        # end reached by pressing one wrong key, next to a page of forty panels.
        return (
            "<!doctype html>\n<html lang=\"en\">\n<head>\n"
            "    <meta charset=\"utf-8\">\n"
            "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"    <title>{safe} is not on the market — {_html.escape(_bot_display_name())}</title>\n"
            "    <link rel=\"icon\" type=\"image/svg+xml\" "
            f"href=\"/static/favicon.svg?v={asset_version('favicon.svg')}\">\n"
            f"    <link rel=\"stylesheet\" href=\"/static/meow.css?v={asset_version('meow.css')}\">\n"
            "</head>\n<body class=\"has-aurora\">\n"
            "    <div class=\"aurora\" aria-hidden=\"true\"><span class=\"grid\"></span></div>\n"
            "    <main class=\"page\" style=\"max-width:640px;margin:12vh auto;\">\n"
            "        <span class=\"eyebrow\">The market</span>\n"
            f"        <h1 style=\"margin-top:0.5rem;\">{safe} is not on the market</h1>\n"
            "        <p class=\"muted\" style=\"margin-top:1rem;\">\n"
            "            The board carries every instrument this deployment trades, by ticker."
            "            The one you asked for is not one of them — the link may have been"
            "            typed from memory, or the symbol may have been renamed.\n"
            "        </p>\n"
            "        <p style=\"margin-top:1.5rem;display:flex;gap:0.75rem;flex-wrap:wrap;\">\n"
            "            <a class=\"btn btn-primary\" href=\"/market/BTC\">Open the board</a>\n"
            "            <a class=\"btn\" href=\"/\">What this bot is</a>\n"
            "            <a class=\"btn\" href=\"/dashboard\">The dashboard</a>\n"
            "        </p>\n"
            "    </main>\n</body>\n</html>\n",
            404,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    # The instrument comes from ``market_context`` and the *chrome* from the same
    # place every other page's does. This route passed only the first, so the
    # header's wordmark and its mark were filled from names nothing had sent:
    # the page drew an empty brand beside a live price, which is how it stayed
    # unnoticed — an unset name in a template is empty, not an error. The two
    # mappings share no key, so the merge cannot silently replace an instrument
    # with a site name.
    return render_template(
        "market.html", **{**_site_context(), **market_context(instrument)}
    )


def _market_groups(market_module) -> list[dict]:
    """The instrument catalog as the rail's sections.

    Grouped by kind, in catalog order, with the two known kinds labelled and any
    future kind appearing under its own name rather than being dropped into
    somebody else's section.
    """
    labels = {market_module.CRYPTO: "Crypto", market_module.STOCK: "Stocks & ETFs"}
    groups: list[dict] = []
    for instrument in market_module.INSTRUMENTS:
        if not groups or groups[-1]["kind"] != instrument.kind:
            groups.append(
                {
                    "kind": instrument.kind,
                    "label": labels.get(instrument.kind, instrument.kind.title()),
                    "instruments": [],
                }
            )
        groups[-1]["instruments"].append(instrument)
    return groups


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
    ("members", "Members", "/dashboard/members", "members"),
    ("server", "Server", "/dashboard/server", "server-control"),
    # The two pages that are about what a member *earns* here. They are pages
    # rather than cards because each of them is a whole job: the economy's
    # leaderboard is a reading nobody can take off a switch, and the level roles
    # are a promise somebody has to be able to check one row at a time.
    ("economy", "Economy", "/dashboard/economy", "economy"),
    ("leveling", "Levels", "/dashboard/leveling", "leveling"),
    ("composer", "Composer", "/dashboard/composer", "composer"),
    ("schedule", "Scheduled", "/dashboard/schedule", "schedule"),
    ("identity", "Identity", "/dashboard/identity", "identity"),
    ("commands", "Commands", "/dashboard/commands", "command-scope"),
    ("status", "Status", "/dashboard/status", "status"),
)

#: The dashboard rail's groups, as (label, page ids). The same law the operator's
#: rail is built to (see :data:`OWNER_GROUPS`), applied to the same problem: the
#: dashboard grew to nine pages, and a flat list of nine is where finding one stops
#: being instant. The grouping is declared here rather than written into the
#: template so the rail stays a rendering of the server's own page list — a page
#: added above that nobody placed in a group is drawn ungrouped, never missing.
#:
#: ``overview`` is deliberately absent for the reason it is absent from
#: :data:`OWNER_GROUPS`: it is the page you return to rather than a destination
#: beside the others, and a home link under a heading reads as though it belongs to
#: that heading. ``tests/test_design_laws.py`` holds the arithmetic — no group
#: longer than five, five groups at most, every other page placed in the order it
#: was declared.
DASHBOARD_GROUPS = (
    # What this server *is* — the systems it runs, the people in it, what they
    # earn here, and the controls over the whole of it. Five pages, which is the
    # width this law allows and the width it reads at: an administrator comes
    # here to see the systems, the roster, the server's own controls, and then
    # the two ledgers.
    ("This server", ("systems", "members", "server", "economy", "leveling")),
    ("Message", ("composer", "schedule")),
    ("Configure", ("identity", "commands", "status")),
)

#: ``/dashboard`` and its six siblings, keyed by path.
DASHBOARD_PAGE_BY_PATH = {path: page for page, _label, path, _anchor in DASHBOARD_PAGES}


@app.route("/dashboard")
@app.route("/dashboard/systems")
@app.route("/dashboard/members")
@app.route("/dashboard/server")
@app.route("/dashboard/economy")
@app.route("/dashboard/leveling")
@app.route("/dashboard/composer")
@app.route("/dashboard/schedule")
@app.route("/dashboard/identity")
@app.route("/dashboard/commands")
@app.route("/dashboard/status")
def dashboard():
    """One page of one server's dashboard — served, not forwarded.

    Every address in this group used to answer **301 to** ``/servers``: the
    template these pages render was swept into the archive while its routes, its
    readers and forty of its own tests still needed it, so per-feature management
    — the systems, the members, the two ledgers, the composer, the queue, the
    identity — was nowhere to be found. The pages are back, in this site's own
    language, drawn from the readers the API endpoints already answer with.

    A signed-out visitor meets the door first. The scope rules are
    :func:`_dashboard_scope`'s, unchanged: a one-time link may only ever reach the
    server it was minted for, and a lookup that fails is never written back into
    the session.
    """
    if not is_authenticated():
        return redirect("/login")

    page = DASHBOARD_PAGE_BY_PATH.get(request.path, "overview")
    return _dashboard_page(page, _dashboard_scope())


def _dashboard_scope():
    """Which server this request is about: the query's, else the session's.

    One function because two callers ask: the page render and (through the
    endpoint's own resolver) every form on it. The rule that matters is the
    ``else``: a failed lookup is **not** persisted, because writing the ``0``
    fallback into the session is what once demoted a server-scoped sign-in to an
    unscoped one, letting the second request reach any server the bot was in.
    """
    requested = request.args.get("guild_id")
    if requested is None:
        requested = request.form.get("guild_id")
    guild_id = _resolve_guild_id(requested)
    if guild_id:
        session["guild_id"] = guild_id
        return guild_id
    return 0


#: What each dashboard page says it is: ``(title, lede)``. Declared here rather
#: than written into the template so the rail, the heading and the skip link
#: cannot describe the same page three different ways — and so the copy sits
#: beside the page list it belongs to, where a page added to the table is a page
#: somebody has to write a sentence for.
DASHBOARD_PAGE_COPY = {
    "overview": (
        "This server",
        "What this server is running right now, who is in it, and what needs a look.",
    ),
    "systems": (
        "Systems",
        "Every system this bot can run here, with its own switch. Off means the "
        "commands are not offered in this server either.",
    ),
    "members": (
        "Members",
        "Who is here, and what may be done to whom — through the same guard the "
        "slash commands use.",
    ),
    "server": (
        "Server",
        "The whole-server controls: lock it down, lift that, and set slowmode.",
    ),
    "economy": (
        "Economy",
        "Where this server's coins are — the wallets, the bank, and the fee the bot "
        "charges. A reading, not a place to set a balance.",
    ),
    "leveling": (
        "Levels",
        "What being here earns, at the rates configured here, and which level "
        "grants what.",
    ),
    "composer": (
        "Composer",
        "Write a message and post it as the bot. It reaches the channels you pick "
        "and nobody else.",
    ),
    "schedule": (
        "Scheduled",
        "What this server has queued, and what it last did — the queue survives a "
        "restart, so the promise is not tied to this tab.",
    ),
    "identity": (
        "Identity",
        "The bot's name and picture **in this server only** — Discord keeps them on "
        "the bot's member here, so changing them changes nothing anywhere else.",
    ),
    "commands": (
        "Commands",
        "What Discord holds for this server, beside what its switches say it should "
        "— the two are different claims, and only one of them can be wrong.",
    ),
    "status": (
        "Status",
        "The deployment behind the bot: the gateway, the uptime watch, the site "
        "check and the configuration audit.",
    ),
}


def _dashboard_rail(nav) -> list[dict]:
    """The rail as groups, with the ungrouped bucket **first**.

    The same arithmetic the console's rail is built to: the home page is
    deliberately in no group — it is where you return to rather than a
    destination beside the others — so the ungrouped bucket is the first row, not
    an overflow. A page added to the table and not placed in a group lands there
    too, so forgetting to place one never makes it unreachable.
    """
    ungrouped = [item for item in nav if not item["group"]]
    rail = [{"label": "", "pages": ungrouped}] if ungrouped else []
    rail += [
        {"label": label, "pages": [item for item in nav if item["group"] == label]}
        for label, _ids in DASHBOARD_GROUPS
    ]
    return rail


def _stamp_rows(rows, field, target) -> None:
    """Add a human reading of an epoch column, in place.

    ``at`` and ``joined_at`` are seconds since the epoch: true, and unreadable on
    a page. The text is made here rather than in the template because four
    environments render these templates and only one of them has this app's
    filters (the same reason :func:`_inline_json` serialises its own payloads).
    """
    for row in rows or []:
        raw = row.get(field)
        try:
            row[target] = _utc_text(int(raw)) if raw else ""
        except (TypeError, ValueError):
            row[target] = ""


def _schedule_queue(guild_id, page) -> dict:
    """What this server has queued, for the one page that draws the queue.

    Read where it is drawn, for the reason the two ledgers are: a page about the
    identity form has no business reading a queue. Never raises — a queue nobody
    can read is a sentence on the page rather than a page nobody can open.
    """
    empty = {"read": False, "upcoming": [], "recent": [], "pending": 0,
             "max_pending": 0, "catchup_hours": 0}
    if not guild_id or page != "schedule":
        return empty
    try:
        data = scheduled.list_for_guild(guild_id)
        guild = _live_guild(guild_id)
        return {
            "read": True,
            # Names are resolved now rather than stored: a channel renamed since
            # it was queued is named as it is, and one that is gone says so.
            "upcoming": _stamped_schedule(guild, data["upcoming"]),
            "recent": _stamped_schedule(guild, data["recent"]),
            **_scheduled_counts(guild_id),
        }
    except Exception as exc:  # noqa: BLE001 — a reading that fails is not a page that fails
        print(f"[DASHBOARD] ⚠️ Could not read the queue: {exc}", flush=True)
        return empty


def _stamped_schedule(guild, messages) -> list[dict]:
    """The queue's rows, each with its delivery time as a person reads it."""
    rows = [_schedule_view(guild, message) for message in messages]
    _stamp_rows(rows, "run_at", "run_at_text")
    return rows


def _channel_choices(guild_id, page) -> list[dict]:
    """Every channel a message could land in, for the pages that send one.

    Discord's own model decides the list: a text channel and a *voice* channel both
    take a message (``discord.VoiceChannel`` is ``Messageable``), while forums,
    categories and stages are left out because they take a thread or nothing. The
    bot's own permissions decide ``can_send``, so a channel it cannot post to is
    offered as such rather than quietly accepted and refused a moment later.

    The leveling page is on this list because a level-up announcement is a
    message: the picker that decides where it lands is this one. Its own form
    filters the voice rooms out, and does it there rather than here because the
    two pages that *compose* a message can legitimately address one. The Server
    page is here for the moderator log, which is also a message — a case card —
    and it filters to the text rooms for the same reason the level-up picker
    does.
    """
    if not guild_id or page not in ("composer", "schedule", "leveling", "server"):
        return []
    guild = _live_guild(guild_id)
    if guild is None:
        return []
    me = getattr(guild, "me", None)
    rows = []
    try:
        for channel in [*guild.text_channels, *guild.voice_channels]:
            perms = channel.permissions_for(me) if me is not None else None
            if perms is not None and not perms.view_channel:
                continue
            rows.append({
                # Text, like every id that crosses to a browser: a snowflake is not
                # a JavaScript number (see ``_console_id``).
                "id": str(channel.id),
                "name": channel.name,
                "kind": "voice" if type(channel).__name__ == "VoiceChannel" else "text",
                "can_send": True if perms is None else bool(perms.send_messages),
            })
    except Exception as exc:  # noqa: BLE001 — a picker that cannot be read is empty, not fatal
        print(f"[DASHBOARD] ⚠️ Could not read the channel list: {exc}", flush=True)
        return []
    return rows


def _economy_display(reading):
    """The economy ledger as the page prints it: text, and no arithmetic in a template.

    Every figure comes from the table the bot writes — the page computes none of
    them — and the strings are made here because a template doing arithmetic on
    "no economy yet" divides by a circulation of zero.
    """
    if not reading:
        return None
    return {
        "wallet": f"{reading.get('wallet', 0):,}",
        "bank": f"{reading.get('bank', 0):,}",
        "circulation": f"{reading.get('circulation', 0):,}",
        "meow_bank": f"{reading.get('meow_bank', 0):,}",
        "accounts": reading.get("accounts", 0),
        "top": [
            {
                **holder,
                "net_text": f"{holder.get('net', 0):,}",
                "wallet_text": f"{holder.get('wallet', 0):,}",
                "bank_text": f"{holder.get('bank', 0):,}",
                "share_text": f"{holder.get('share', 0):.1f}%",
            }
            for holder in reading.get("top", [])
        ],
    }


def _fee_display(reading) -> dict:
    """The bank fee as a page states it: three labelled facts and one sentence.

    One author for the two surfaces that print it — the member dashboard's own
    economy page and the operator console's — and the reason is the sentence
    underneath. ``meowfee.describe()`` is written for **Discord**, where `**2%**`
    is bold; a page is not, and the marks arrive on it instead of the emphasis.
    That strip was missing on exactly one of the two readers once, and
    `/owner/economy` shipped a literal `**2%**` to the one person most likely to
    paste it somewhere. One function is what makes it a rule rather than
    something the next reader has to remember.

    ``stored`` is not decoration: it is whether the rate came from this server's
    own row or from the shipped default, which is the whole question an operator
    has when two servers charge differently.
    """
    found = reading or {}
    return {
        "label": str(found.get("label") or ""),
        "enabled": bool(found.get("enabled", True)),
        # A missing cap is not a fee that can be unlimited — it is a reading that
        # did not carry one, and the template prints it as a number.
        "cap": found.get("cap") or 0,
        "stored": bool(found.get("stored", False)),
        "describe": str(found.get("describe") or "").replace("**", ""),
    }


def _checked_text(seconds) -> str:
    """How long ago a reading was taken, in words a person would use.

    Coarse on purpose. A pass runs every few minutes, so a page that said
    ``183s`` would be reporting a clock rather than an age; the one thing a
    reader needs from this number is whether they are looking at something that
    just answered or something from an hour ago.
    """
    try:
        value = max(0, int(seconds))
    except (TypeError, ValueError):
        return ""
    if value < 90:
        return f"{value}s ago"
    if value < 5400:
        return f"{value // 60} min ago"
    if value < 172800:
        return f"{value // 3600}h ago"
    return f"{value // 86400}d ago"


def _site_note(reading) -> str:
    """The site check in a sentence, for a reading that carries counts and no words.

    ``site_guard.health_json`` returns words only while nothing has run in this
    process; once there is a verdict it returns the counts, and the sentence it
    *does* compose (``utils.site_guard.describe``) is written for a host log to be
    grepped in — it leads with ``clean —`` and names every path. A page says what
    the count means instead.
    """
    found = reading or {}
    state = str(found.get("status") or "")
    probes = found.get("probes")
    exposed = int(found.get("exposed") or 0)
    cached = int(found.get("cached") or 0)
    unanswered = int(found.get("unanswered") or 0)
    site = str(found.get("site") or "the site")
    if state == site_guard.Verdict.EXPOSED:
        return (
            f"{exposed} of the {probes} repository paths still answer with a page at "
            f"{site}. That is the whole of what this check looks for: a file the "
            "deployment was supposed to leave out being served anyway."
        )
    if state == site_guard.Verdict.CACHED:
        return (
            f"{cached} of the {probes} repository paths are being answered from a cache "
            f"at {site} although the site itself refuses them. A redeploy does not clear "
            "that and a reload does not either — it takes a purge, which is a "
            "Cloudflare-side action on a host this project does not own."
        )
    if state == site_guard.Verdict.FALLBACK:
        return (
            f"{site} answers a page for a path that does not exist, so a page there says "
            "nothing about what was asked for and this reading cannot tell."
        )
    if state == site_guard.Verdict.UNREACHABLE:
        return (
            f"{unanswered} of the {probes} probes went unanswered at {site}: the tree "
            "was neither confirmed public nor confirmed private."
        )
    if state == site_guard.Verdict.CLEAN:
        return (
            f"All {probes} of the repository paths answer 404 at {site} — the answer "
            "that means the deployment is not publishing them."
        )
    return ""


def _health_display(*, uptime=None, site=None, config=None) -> dict:
    """The deployment's three health readings as a page states them.

    One author for the operator console's Ops page, and the reason is what these
    cards used to be: a ``facts()`` dump, which walks a mapping's **field names**.
    So the operator read `status / at / stale / ratio 30d / age seconds / age text
    / detail` — their own reader's variable names, in monospace — on the one page
    whose entire job is to be read when something is wrong.

    The other half of the same defect is worse than ugly: ``facts()`` renders a
    mapping's *scalars* and skips a list, and ``monitors`` is a list — so the names
    and their 30-day ratios were missing while the panel still said *"2 monitors, 1
    up"*. A count with no name is a reading nobody can act on, which is why the rows
    travel through here untouched for the table that draws them.

    Nothing is invented: every figure and every sentence comes from the reading
    itself, and a reading that is absent comes back as a word rather than as a zero
    — ``unknown`` is a different claim from ``0 down``.
    """
    up = uptime or {}
    watching = [f"{int(up.get('total') or 0)} monitor(s)"]
    for key in ("up", "down", "paused", "waiting"):
        value = int(up.get(key) or 0)
        if value:
            watching.append(f"{value} {key}")
    ratio = up.get("ratio_30d")
    checked = site or {}
    unset = int((config or {}).get("unset") or 0)
    attention = int((config or {}).get("attention") or 0)
    return {
        "uptime": {
            "state": str(up.get("status") or "unknown"),
            "counts": " · ".join(watching),
            "checked": str(up.get("age_text") or "never"),
            # A kept reading is a different claim from a fresh one, and this is the
            # only line on the page that says which of the two is on screen.
            "held": (
                "Kept from the last pass that answered: every refresh since then "
                "failed to reach UptimeRobot, so these figures are the older ones."
                if up.get("stale")
                else ""
            ),
            "ratio": f"{ratio}%" if isinstance(ratio, (int, float)) else "",
            "page": str(up.get("page") or ""),
            # The rows themselves, for the table. Not summarised here: a monitor's
            # own name, state and ratio are what the table's columns are.
            "monitors": list(up.get("monitors") or []),
            "note": str(up.get("detail") or ""),
        },
        "site": {
            "state": str(checked.get("status") or "not run yet"),
            "checked": _checked_text(checked.get("checked_seconds_ago")),
            "probes": checked.get("probes"),
            "exposed": checked.get("exposed"),
            "cached": checked.get("cached"),
            "unanswered": checked.get("unanswered"),
            "site": str(checked.get("site") or ""),
            "note": str(checked.get("detail") or "") or _site_note(checked) or (
                "Not run yet in this process — the first pass runs shortly after boot, "
                "and this card fills itself in then."
            ),
        },
        "config": {
            "state": str((config or {}).get("status") or "unknown"),
            "unset": unset,
            "attention": attention,
            "note": (
                f"{attention} setting{'s' if attention != 1 else ''} this deployment "
                f"reads {'are' if attention != 1 else 'is'} not set, and every one of "
                f"them costs a feature — {unset} in all are unset, and the ones that "
                "matter are named below."
                if attention
                else (
                    "Every setting that costs a feature is set. "
                    f"{unset} settings are unset, and none of them is one."
                )
            ),
        },
    }


def _level_display(reading):
    """The level ledger as the page prints it, including the rows XP has moved.

    A stored level that disagrees with the XP is **repaired**: the reader derives
    ``level`` from the XP and keeps the stored column beside it, and a row where
    the two differ is one ``/setlevel`` or a restored backup left behind. The page
    prints the derived figure and says so, because the XP is what the rank card,
    the leaderboard and every payout are computed from.
    """
    if not reading:
        return None
    settings = reading.get("settings") or {}
    return {
        "text_xp": settings.get("text_xp", 0),
        "coin_band": f"{settings.get('text_min_coins', 0)}–{settings.get('text_max_coins', 0)}",
        # XP first, then coins: the order the bot pays them in, and the order the
        # page reads them in.
        "voice": f"{settings.get('vc_xp_per_min', 0)} XP · {settings.get('vc_coins_per_min', 0)} 🪙",
        "stored": bool(settings.get("stored")),
        "channel_id": settings.get("channel_id") or 0,
        "dm": bool(settings.get("dm_notifications")),
        # The same figures as raw numbers, for the form that changes them. The two
        # strings above are what the page *reads*; a control has to start at the
        # value, not at a sentence about it.
        "enabled": bool(settings.get("enabled", True)),
        "text_min_coins": int(settings.get("text_min_coins", 4)),
        "text_max_coins": int(settings.get("text_max_coins", 10)),
        "vc_coins_per_min": int(settings.get("vc_coins_per_min", 2)),
        "vc_xp_per_min": int(settings.get("vc_xp_per_min", 3)),
        "top": [
            {
                **earner,
                "level_text": f"Level {earner.get('level', 0)}",
                "repaired": earner.get("stored_level") != earner.get("level"),
                "progress_text": f"{earner.get('into', 0):,} / {earner.get('needed', 0):,} XP",
                "xp_text": f"{earner.get('xp', 0):,}",
            }
            for earner in reading.get("top", [])
        ],
    }


#: The setting the default role is stored under. Named here rather than read out
#: of ``cogs/server_control.py`` because that cog imports this module — a cycle,
#: and the reason the key that already lives in one place (`modlog.SETTING`) is
#: imported while this one is spelled once, beside the check that reads it.
DEFAULT_ROLE_SETTING = "default_role_id"


def _log_health(guild, guild_id) -> dict | None:
    """The moderator log as the checklist needs it: set, real, writable.

    ``None`` means the setting could not be read at all, which the card says out
    loud rather than counting as healthy — see ``utils/health``'s note on the two
    kinds of silence. A stored id pointing at a channel that is gone is the case
    this exists for: ``modlog.channel_for`` prints one line into a host log, and a
    server whose moderators see no record has no way to find that line.
    """
    try:
        raw = db.read_setting_sync(guild_id, modlog.SETTING)
    except Exception as exc:  # noqa: BLE001 — a reading, never a 500
        print(f"[HEALTH] ⚠️ could not read {modlog.SETTING} for {guild_id}: {exc}", flush=True)
        return None
    if raw is None or str(raw).strip() == "":
        return {"configured": False}
    try:
        channel_id = int(raw)
    except (TypeError, ValueError):
        # A value that is not an id at all (a hand-edited database, one old
        # ``/updatedb``): there is no channel to look up, and there never was.
        return {"configured": True, "exists": False, "channel": f"`{raw}`"}
    lookup = getattr(guild, "get_channel", None)
    if not callable(lookup):
        # A guild that cannot resolve a channel — the preview harness's
        # gateway-less server, and nothing else — cannot answer this reading, and
        # saying so is the whole point: `None` is "not read", never "healthy".
        return None
    channel = lookup(channel_id)
    if channel is None:
        return {"configured": True, "exists": False, "channel": f"`{channel_id}`"}
    facts = {
        "configured": True,
        "exists": True,
        # The channel's *name*, not Discord's ``<#id>`` mention: this is read on a
        # web page, and a page is not where Discord's own syntax should be printed
        # literally. (The old side of this was the same mistake the fee's marks
        # were; the id is on the line beside it in the fix if it is wanted.)
        "channel": f"#{getattr(channel, 'name', channel_id)}",
    }
    # One rule, and now one implementation: the picker on the Server page warns
    # about the same thing, through ``modlog.can_post``. Both permissions have to
    # have been *answered* before this says anything — ``getattr(..., False)``
    # would read a stand-in that never heard of ``embed_links`` as Discord saying
    # no, and the card would then report a permission the bot actually has, which
    # is a finding invented by a fixture.
    verdict = modlog.can_post(channel, getattr(guild, "me", None))
    if verdict is not None:
        facts["can_post"] = verdict
    return facts


def _log_picker(guild_id, choices=()) -> dict:
    """The moderator log as the Server page's *own* control needs it.

    Two readings and no verdict: what is stored, and what the stored channel is
    called. The verdict is the checklist's (``_log_health``), drawn on the
    overview — a second opinion here is how one page ends up saying the record is
    fine while the other says the channel it points at is gone.

    ``current`` and ``offered`` exist for the case the picker's list cannot
    cover. A stored channel Discord would not hand back — deleted, or an outage —
    is still a channel this server has set, and a ``<select>`` with no matching
    option draws an empty one: the page telling an administrator their log is
    *off* when the database says it is on is the exact class of lie this project
    spends the rest of its time removing. ``offered`` is answered here rather than
    by the template comparing two lists, so the option that carries the stored id
    is drawn exactly when it is the only one that could.
    """
    try:
        raw = db.read_setting_sync(guild_id, modlog.SETTING)
    except Exception as exc:  # noqa: BLE001 — a reading, never a 500
        print(f"[DASHBOARD] ⚠️ could not read {modlog.SETTING} for {guild_id}: {exc}", flush=True)
        return {"read": False, "channel_id": "", "current": "", "offered": True}
    channel_id = modlog._as_channel_id(raw)
    if channel_id is None:
        return {"read": True, "channel_id": "", "current": "", "offered": True}
    guild = _live_guild(guild_id)
    lookup = getattr(guild, "get_channel", None)
    channel = lookup(channel_id) if callable(lookup) else None
    name = getattr(channel, "name", None)
    return {
        "read": True,
        # Text, like every id that crosses to a browser: a snowflake is not a
        # JavaScript number, and the option it is compared against is a string.
        "channel_id": str(channel_id),
        "current": f"#{name}" if channel is not None else f"#{channel_id}",
        "offered": any(str(row.get("id")) == str(channel_id) for row in choices),
    }


#: Where a mapped reaction-role message lives, for the link the page draws.
REACTION_ROLE_LINK = "https://discord.com/channels"

#: The three snowflakes a pasted message link carries, in order.
_REACTION_ROLE_LINK_RE = re.compile(r"channels/(\d+)/(\d+)/(\d+)")


def _reaction_roles_reading(guild_id, roles=()) -> dict:
    """Every mapping this server has, with the role each emoji hands out.

    A row whose role was deleted since is still listed and said to be gone: a
    mapping nothing will ever grant is exactly what an administrator needs to
    see, and hiding it removes the only place it still exists. Reads are sync
    because this is a page, and a failed read is *said* rather than drawn as an
    empty list — the two look identical in a list and mean opposite things.
    """
    try:
        rows = db.read_reaction_roles_sync(guild_id)
    except Exception as exc:  # noqa: BLE001 — a reading, never a 500
        print(f"[DASHBOARD] ⚠️ could not read reaction roles for {guild_id}: {exc}", flush=True)
        return {"read": False, "rows": []}
    by_id = {row["id"]: row for row in roles}
    listed = []
    for row in rows:
        role = by_id.get(str(row["role_id"]))
        listed.append({
            "message_id": str(row["message_id"]),
            "channel_id": str(row["channel_id"]),
            "emoji": row["emoji"],
            "role_id": str(row["role_id"]),
            "role_name": role["name"] if role else "",
            # Only claim "gone" when Discord actually handed back a list; a
            # failed read is not evidence that a role was deleted.
            "role_gone": bool(roles) and role is None,
            "above": bool(role and role["above"]),
            "link": (
                f"{REACTION_ROLE_LINK}/{int(guild_id)}/{int(row['channel_id'])}/{int(row['message_id'])}"
                if row["channel_id"] else ""
            ),
        })
    return {"read": True, "rows": listed}


def _role_choices(guild_id) -> list:
    """Every role this server has that could be handed to a new member.

    A reading from Discord, with the same honesty rule the channel list follows:
    an empty list means "could not read, or there are none" and the page says
    which. ``above`` marks a role Discord would refuse to assign — the bot's own
    role sits at or below it — which is the one thing about a default role that
    silently does nothing when it is wrong.
    """
    guild = _live_guild(guild_id)
    if guild is None:
        return []
    me = getattr(guild, "me", None)
    top = getattr(getattr(me, "top_role", None), "position", None)
    rows = []
    for role in getattr(guild, "roles", None) or []:
        # ``Role.is_default`` is a *method*: reading the attribute as a flag is
        # truthy for every role, which silently dropped the whole list.
        is_default = getattr(role, "is_default", None)
        if callable(is_default) and is_default():
            continue
        position = getattr(role, "position", 0) or 0
        rows.append({
            # Text, like every id that crosses to a browser: a snowflake is not
            # a JavaScript number, and the option it is compared against is a
            # string (see ``_console_id``).
            "id": str(getattr(role, "id", "")),
            "name": str(getattr(role, "name", "")),
            "position": int(position),
            "above": bool(top is not None and int(top) <= int(position)),
        })
    rows.sort(key=lambda row: (-row["position"], row["name"].lower()))
    return rows


def _default_role_reading(guild_id, choices=()) -> dict:
    """The default role as its own control needs it: stored, and whether it lands.

    The same split the log picker uses: this reports *what is set* and the
    checklist on the overview reports whether it works. A stored role that is not
    in the list Discord just handed back is still shown — a picker that fell back
    to its first option would draw a server that assigns a role as one that
    assigns none.
    """
    try:
        raw = db.read_setting_sync(guild_id, DEFAULT_ROLE_SETTING)
    except Exception as exc:  # noqa: BLE001 — a reading, never a 500
        print(
            f"[DASHBOARD] ⚠️ could not read {DEFAULT_ROLE_SETTING} for {guild_id}: {exc}",
            flush=True,
        )
        return {"read": False, "role_id": "", "current": "", "above": False, "offered": True}
    if raw is None or str(raw).strip() == "":
        return {"read": True, "role_id": "", "current": "", "above": False, "offered": True}
    role_id = _int_or_none(raw)
    if role_id is None:
        return {
            "read": True, "role_id": "", "current": f"{raw}",
            "above": False, "offered": False,
        }
    match = next((row for row in choices if row["id"] == str(role_id)), None)
    return {
        "read": True,
        "role_id": str(role_id),
        "current": f"@{match['name']}" if match else f"role {role_id}",
        "above": bool(match and match["above"]),
        "offered": match is not None,
    }


def _default_role_health(guild, guild_id) -> dict | None:
    """The default role as the checklist needs it: set, real, and assignable.

    The hierarchy half is the defect this is really about: Discord refuses a role
    change for a role at or above the bot's highest one, and the cog's own reply
    is the one it gives when it *believes* it worked — so a server whose default
    role sits too high has a welcome nobody receives and no error anywhere.
    ``above`` is left unset when the positions could not be read, because a
    hierarchy claim made from a missing reading is a claim this module cannot
    make.
    """
    try:
        raw = db.read_setting_sync(guild_id, DEFAULT_ROLE_SETTING)
    except Exception as exc:  # noqa: BLE001 — a reading, never a 500
        print(
            f"[HEALTH] ⚠️ could not read {DEFAULT_ROLE_SETTING} for {guild_id}: {exc}",
            flush=True,
        )
        return None
    if raw is None or str(raw).strip() == "":
        return {"configured": False}
    lookup = getattr(guild, "get_role", None)
    if not callable(lookup):
        return None  # a stand-in, or a guild that cannot resolve roles
    try:
        role_id = int(raw)
    except (TypeError, ValueError):
        return {"configured": True, "found": False, "name": str(raw)}
    role = lookup(role_id)
    if role is None:
        return {"configured": True, "found": False, "name": f"`{role_id}`"}
    facts = {"configured": True, "found": True, "name": getattr(role, "name", "a role")}
    me = getattr(guild, "me", None)
    top = getattr(me, "top_role", None) if me is not None else None
    position = getattr(role, "position", None)
    if top is not None and position is not None:
        facts["above"] = int(getattr(top, "position", -1)) > int(position)
    return facts


def _server_health(guild_id, states, publish=None) -> dict:
    """What this server's dashboard should be complaining about, and what it read.

    The checks are ``utils/health``'s — pure functions over plain facts — and this
    is only the reading half: Discord's own answer for the bot's role, the two
    settings that point at a channel and a role, and the publish reading the page
    has already taken. It cost one settings read per check and nothing else.

    The two keys that matter are ``checked`` and ``unread``. A check nobody could
    perform and a check that came back healthy both produce no findings, and a
    card that cannot tell those apart prints "nothing needs a look" over a server
    it never looked at — which is this project's oldest defect, wearing a green
    dot.
    """
    facts = health_checks.presets()
    facts["systems"] = dict(states or {})
    facts["names"] = {module["id"]: module["name"] for module in AVAILABLE_MODULES}
    guild = BOT_INSTANCE.get_guild(guild_id) if (BOT_INSTANCE and guild_id) else None
    if guild is None:
        return {
            "read": False,
            "why": (
                "The bot is not connected to this server right now, so there is nothing "
                "to check its role, its channels or its commands against."
                if guild_id else
                "No server is in hand, so there is nothing to check."
            ),
            "findings": [],
            "checked": [],
            "unread": [],
            "counts": {"broken": 0, "warning": 0},
        }

    me = getattr(guild, "me", None)
    permissions = getattr(me, "guild_permissions", None) if me is not None else None
    if permissions is not None:
        facts["permissions"] = {
            name: bool(getattr(permissions, name, False))
            for name in health_checks.PERMISSION_NAMES
        }
    facts["log"] = _log_health(guild, guild_id)
    facts["default_role"] = _default_role_health(guild, guild_id)
    if publish is not None:
        facts["publish"] = {
            "matches": publish.get("matches"),
            "text": publish.get("text", ""),
        }

    rows = health_checks.findings(facts)
    return {
        "read": True,
        "why": "",
        "findings": [
            {
                "key": row.key,
                "severity": row.severity,
                "title": row.title,
                # The findings are written with the marks a *card* reads (bold
                # names, and an id in backticks), because the same rows are what
                # a slash command prints; this render is a page, and a page is
                # not where `**X**` should be read literally. Stripped here
                # rather than in the template, which is the same rule the bank's
                # own fee follows.
                "detail": _plain_marks(row.detail),
                "fix": _plain_marks(row.fix),
            }
            for row in rows
        ],
        "checked": health_checks.read(facts),
        "unread": health_checks.unread(facts),
        "counts": health_checks.summary(rows),
    }


def _plain_marks(text: str) -> str:
    """Discord's own marks, as a web page renders them: not at all.

    ``**bold**`` and a backtick-fenced id are how a *card* is written, and every
    finding is composed for a card because the same text is what ``/modlog`` and
    the console print. A page that showed them literally is a page sending an
    administrator to fix "``log_channel_id``", which is what the first render of
    this card did — and a remedy with two backticks around it reads as a typo
    rather than as an instruction.
    """
    return (text or "").replace("**", "").replace("`", "")


def _dashboard_page(page: str, guild_id):
    """One page of one server's dashboard, as the template draws it.

    **Every page computes every rule**: the scope rules, the two readings of
    Discord's command list, the identity state and the composer's rulebook are
    computed whichever page is open, because a page that computed only what it
    draws would be eleven chances to disagree with the others about the same
    server. The two exceptions are documented where they are made — a member list
    is a Discord reading rather than a rule (:func:`_management_context`) and a
    ledger is a database reading (:func:`_progression_context`), so both are read
    only where they are drawn.
    """
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

        # The operator's own word about this system, when they have one. The
        # state above already *is* their word — ``get_states`` applies it last —
        # so what this adds is the reason: a switch that has been taken away from
        # the person looking at it has to say who took it and mean it, or the
        # only visible difference is that pressing it does nothing.
        override = modules.override_for(m_id)
        modules_data.append({
            "id": m_id,
            "name": m["name"],
            "icon": m["icon"],
            "category": m["category"],
            "desc": m["desc"],
            "commands": m["commands"],
            # A copy per render. The reading below writes each setting's value in
            # force into its spec, and ``AVAILABLE_MODULES`` is module state that
            # outlives one request — mutating it would hand the next render (and
            # the test-suite) a stale value as if it were the registry's own.
            "settings": [dict(spec) for spec in m.get("settings", ())],
            "enabled": is_enabled,
            "locked": override is not None,
            "state": ("Locked on" if override else "Locked off") if override is not None else "",
            "blocked": (
                f"The operator has switched {m['name']} "
                f"{'on' if override else 'off'} for every server, so this server "
                "cannot change it. It is on the console's Systems page."
            ) if override is not None else ""
        })

    # The settings each system declares, with this server's values in force —
    # the reading half of every control the Systems page draws under its switch.
    # Read through the registry so the number the page prints and the rule the
    # bot applies are the same number, and read on every page for the same
    # reason every other reading is: the overview's list links to these cards.
    for module in modules_data:
        values = features.read_setting_values(
            module["id"], lambda key: db.read_setting_sync(guild_id, key)
        )
        for spec in module["settings"]:
            spec["value"] = values.get(spec["id"])

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
    # it. Server-rendered, and now the only renderer there is: the page's refresh
    # script is gone with the rest of the dashboard's JavaScript, because the
    # host's CSP has no `unsafe-inline` and a page whose readings need a script
    # is a page with no readings on the day the script fails.
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

    # What this server is quietly not getting, from the same readers everything
    # else on the page comes from — the switch states above, the publish reading
    # just taken, and two settings. Computed on every page for the reason the rest
    # of this function's readings are, and drawn on the front page, which is where
    # somebody looks when a feature they switched on is not doing anything.
    server_health = _server_health(guild_id, states, publish_data)

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

    # Every page of one server, with this session's server carried across: a rail
    # that dropped `guild_id` would move an administrator from one page of their
    # server to another server's page.
    guild_query = f"?guild_id={guild_id}" if guild_id else ""
    # The two pages that are about *people and permissions* rather than about
    # configuration. They are read only where they are drawn, and that is the one
    # exception to "every page computes everything": a member list is a reading
    # from Discord rather than a rule about the server, and asking Discord for a
    # roster while somebody is looking at the composer would be a page nobody can
    # open. Nothing here is a *rule* — the guards the actions use are computed at
    # the moment of the action, in the endpoint.
    management = _management_context(guild_id, page)
    progression = _progression_context(guild_id, page)
    # Read once and handed to two readers: the pickers on the pages that send a
    # message, and the log picker's own answer to "is the stored channel in this
    # list" — which the template cannot answer for itself without drawing the
    # stored channel twice.
    channel_choices = _channel_choices(guild_id, page)
    title, lede = DASHBOARD_PAGE_COPY.get(page, DASHBOARD_PAGE_COPY["overview"])
    dashboard_nav = _page_nav(
        DASHBOARD_PAGES, page, guild_query, groups=DASHBOARD_GROUPS
    )
    # An epoch is true and unreadable, and four environments render this template
    # while only one has this app's filters — so the reading of a timestamp is made
    # here, once, for the two tables that show one.
    _stamp_rows(management["recent"], "at", "when_text")
    if management["roster"]:
        _stamp_rows(management["roster"]["members"], "joined_at", "joined_text")
    page_path = next(
        path for candidate, _label, path, _anchor in DASHBOARD_PAGES if candidate == page
    )
    economy_reading = progression["economy"] or {}
    fee = economy_reading.get("fee") or {}
    # Read once and handed to two readers — the picker and the reading above it —
    # so the stored id is offered or explained against the same list.
    role_choices = _role_choices(guild_id)
    return render_template(
        "dashboard.html",
        # The frame every served page wears, merged first so this page's own
        # names win below. This render was the one group that never merged it,
        # and an unset name in a template is empty rather than an error — so all
        # eleven pages had been served with a header reading "." where the
        # wordmark goes, an empty footer title, no "15 systems and 110 commands"
        # line, no Support-the-bot link, and the *"nothing is kept longer than it
        # is needed"* fallback while every other page on the site said 365 days.
        # Nothing failed and every markup test still passed, because the markup
        # was always right: the render call was what was short. `_invite_context`
        # is merged over it because this page's invite link carries the server in
        # hand, which the site-wide one does not.
        **{**_site_context(), **_invite_context(guild_id=guild_id)},
        **_frame_nav(),
        # The frame's public list already carries a `dashboard` item, and so does
        # the member host's, so this marks the same destination on either.
        current="dashboard",
        dash_page=page,
        # The address a form on this page comes back to, server and all.
        page_url=f"{page_path}{guild_query}",
        # The fee, from the same switch the bank reads — through the one function
        # that states it for a page, which both this page and the console's
        # economy page call (see ``_fee_display``).
        economy_fee=_fee_display(fee),
        page_title=title,
        page_lede=lede,
        # The same list twice: as groups for the rail, and flat for the skip link
        # and the page's own script. One reading, two renderings.
        rail=_dashboard_rail(dashboard_nav),
        # The sentence one write left for the next render — popped rather than
        # read, so a reload cannot re-announce an action that already happened.
        # Marks stripped because the sentence was *written* for a card — the
        # endpoints answer both doors with one string, and several of them say
        # `**3** channel(s)` or wrap an id in backticks. One strip at the pop
        # covers every endpoint's wording, rather than each writer having to
        # remember which door it is talking to.
        dash_notice=_plain_marks(session.pop("dash_notice", "")),
        dash_error=_plain_marks(session.pop("dash_error", "")),
        # Read on the page that draws them, like the two ledgers.
        schedule_queue=_schedule_queue(guild_id, page),
        channel_choices=channel_choices,
        # The moderator log's own row, for the one page that offers to set it. On
        # every page for the reason ``server_health`` is: the checklist that
        # names this setting is drawn on the overview, and a page whose *advice*
        # is drawn separately from the control it points at is how the advice and
        # the surface came apart in the first place.
        log_picker=_log_picker(guild_id, channel_choices),
        # The other setting the overview's checklist points at. Both are read on
        # every page for the same reason ``log_picker`` is: the findings live on
        # the overview, and advice drawn apart from its control is how the two
        # came apart in the first place. Roles come from the live guild object
        # rather than an API call, so this costs nothing while somebody waits.
        roles=role_choices,
        default_role=_default_role_reading(guild_id, role_choices),
        # The reaction-role mappings, on the same page for the same reason: they
        # are rows this server owns, and the page that names the roles is the
        # page that can offer one.
        reaction_roles=_reaction_roles_reading(guild_id, role_choices),
        economy_display=_economy_display(progression["economy"]),
        level_display=_level_display(progression["levels"]),
        # What a member action may be asked for, from the one list the endpoint
        # validates against, so the page cannot offer what would be refused.
        member_actions=PANEL_MEMBER_ACTIONS,
        server_actions=PANEL_SERVER_ACTIONS,
        # The two write doors a form posts to, named once here rather than typed
        # into eleven forms in the template.
        toggle_url="/api/toggle",
        toggle_all_url="/api/toggle-all",
        management=management,
        economy_view=progression["economy"],
        level_view=progression["levels"],
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
        dashboard_pages=dashboard_nav,
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
        server_health=server_health,
        # The support bridge, and only for the operator: the conversation is
        # between the operator and the bot's own support channel, so a server's
        # administrator sees neither half of it. `None` rather than an empty card
        # is what the template's own `{% if support %}` reads as "not for you" —
        # a card drawn for everybody would be a page about a room most of its
        # readers cannot write in, and the panel's write is operator-only anyway.
        support=(
            _support_card(guild=_live_guild(guild_id))
            if _principal().is_operator
            else None
        ),
        active_count=active_count,
        total_count=len(AVAILABLE_MODULES),
        csrf_token=get_csrf_token(),
        # Only the servers this session may act on, so the switcher cannot even
        # offer one it would then refuse.
        guilds=_visible_guilds(),
        can_switch_guild=_principal().may_switch_guild,
        signed_in_as=session.get("discord_user") or {},
        pick_server_url=("/servers" if _principal().may_switch_guild else ""),
        # The fleet console is the operator's alone, so the link to it is only
        # offered where it would actually open. A server's administrator seeing
        # a button that bounces them back to the picker is worse than no button.
        fleet_url=("/owner" if _principal().is_operator else ""),
        # `selling`, the invite links and the retention sentence are no longer
        # listed here: they are `_site_context()`'s, merged above, and one owner
        # for them is the reason that function exists.
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
        # What has been sent out of band, and whether anything can be. Read from
        # process state, so this panel costs no request on a page somebody is
        # waiting for — and it is deliberately the *same* ledger the sender writes,
        # so what an operator reads here is what actually went to the channel.
        alert_state=alert_webhook.state(),
        # The one thing on the Overview that is about the *server* rather than
        # about the deployment, and the only thing an administrator opening this
        # page actually wants first: who is in a room, who is in one alone, and
        # what the player is doing. Reads the gateway; never raises.
        overview_live=_overview_live(guild_id),
    )


# ---------------------------------------------------------------------------
# Members and the server itself
# ---------------------------------------------------------------------------
# Two pages and two endpoints, and all four exist because a web session has no
# way to do this in Discord: an administrator who is not at their desktop, or who
# would rather not do a bulk permission change from a phone keyboard, had exactly
# one option — leave the incident for later.
#
# Three rules shape the whole surface:
#
# * **The authority is the session's, re-checked per request.** ``_principal``
#   decides who may act on a server (the same rule the toggles and the identity
#   panel already ask), and the *person's own* Discord permissions decide what
#   may be done to whom — which is why the actions run through
#   ``utils.moderation_actions``: the guard that refuses moderating an
#   administrator, the owner, or somebody with an equal role is the same one
#   ``/kick`` has always used.
# * **Every action is one line in ``web_actions``.** Discord's audit log credits
#   the *bot*, because the bot is what performed the action, so this is the only
#   place the person who asked is written down.
# * **A refusal is an answer.** Permission problems, a member who left between the
#   page loading and the click, and a server where the bot is not present are all
#   answered with a sentence naming what to do rather than a 500.


def _live_guild(guild_id):
    """The live guild object for this id, or ``None``. Never raises."""
    if BOT_INSTANCE is None or not guild_id:
        return None
    try:
        return BOT_INSTANCE.get_guild(int(guild_id))
    except (TypeError, ValueError):
        return None


async def _actor_member(guild, user_id):
    """The signed-in person's own member in that server.

    Cache first, one fetch after: the Server Members intent fills the cache on a
    live gateway, and a *cold* cache (a restart, or the intent missing) would
    otherwise refuse every action with "I could not read your own member" — a
    sentence that is true and useless.
    """
    if guild is None or not user_id:
        return None
    member = guild.get_member(int(user_id))
    if member is not None:
        return member
    fetch = getattr(guild, "fetch_member", None)
    if not callable(fetch):
        return None
    try:
        return await fetch(int(user_id))
    except Exception:  # noqa: BLE001 — not a member, or Discord refused to say
        return None


def _may_act_here(guild_id) -> bool:
    """Whether this session may act on this server at all.

    One question with one home (``utils.principal``), asked again on every
    request: a Discord sign-in's reach is recomputed from the bot's live guilds
    rather than trusted from the cookie, so a server the person was demoted from
    stops being reachable immediately.
    """
    return _principal().may_act_in(guild_id, administered=[row["id"] for row in _visible_guilds()])


def _loop_reading(coro, default):
    """``(payload, read)`` for one reading that needs the bot's loop.

    The bridge is ``_run_on_bot_loop``, the one this file already had (on the
    bot's own loop when there is one, and ``asyncio.run`` for the stand-in
    harness that has none) — a second bridge would be a second set of rules about
    when Discord may be touched.

    A reading that fails returns the caller's default and ``False``, which is how
    a page can say *not read* rather than drawing a confident empty list: "I could
    not ask" and "there is nobody" are different answers, and this project keeps
    them apart everywhere else too.
    """
    try:
        payload = _run_on_bot_loop(coro, timeout=20.0)
    except Exception as exc:  # noqa: BLE001 — a reading that fails is not a page that fails
        print(f"[PANEL] reading failed: {exc}", flush=True)
        return default, False
    if isinstance(payload, dict) and payload.get("success") is False:
        return default, False
    return payload, True


def _bot_abilities(guild) -> list[dict] | None:
    """What this deployment's own permissions let it do here — a reading, not a guess.

    Every control on the two management pages depends on a permission the bot may
    not hold: it was invited without *Manage Channels*, or its role sits below the
    one it is trying to change. The failure that produces is the worst kind — a
    button that reports success and changes nothing — and the reason is on
    ``guild.me``, which costs no request to read. ``None`` when there is no member
    object to read, because "I could not check" and "you cannot do this" are
    different sentences and only one of them is actionable.
    """
    perms = getattr(getattr(guild, "me", None), "guild_permissions", None)
    if perms is None:
        return None
    rows = (
        ("Kick members", "kick_members", "the kick button, on the Members page"),
        ("Ban members", "ban_members", "ban, and undoing one"),
        ("Moderate members", "moderate_members", "timeouts and warnings"),
        ("Manage channels", "manage_channels", "lockdown, lifting it, and slowmode"),
        ("Manage messages", "manage_messages", "clearing up after one"),
    )
    return [
        {"label": label, "ok": bool(getattr(perms, attr, False)), "why": why}
        for label, attr, why in rows
    ]


def _progression_context(guild_id, page) -> dict:
    """What this server's members hold and have earned, for the two ledgers.

    Read where it is drawn, for the reason :func:`_management_context` gives: a
    page about the composer has no business reading a leaderboard, and these are
    two more tables nobody asked for unless the page is open.

    Both readings are **reads**, and the pages say so. The balances and the XP
    are written by ``cogs/economy.py`` and ``cogs/leveling.py``, which are also
    the only things that decide what a message is worth; a dashboard that wrote
    its own numbers here would be a second source of truth for the same wallet.
    The one exception is the module switch, which is the same endpoint the
    Systems page uses — so "manage this feature" on these pages is the switch
    that already exists rather than a second one with its own rules.
    """
    context = {"economy": None, "levels": None}
    if not guild_id or page not in ("economy", "leveling"):
        return context
    # Names, where Discord's cache can answer without asking. A leaderboard of
    # ids is a leaderboard nobody reads, and the roster cache the Members page
    # already draws from holds exactly this mapping. A cold cache is not an
    # error: the row falls back to the id, which is still true.
    names: dict[int, str] = {}
    guild = _live_guild(guild_id)
    if guild is not None:
        cached = moderation_actions.roster_cached(guild)
        if cached:
            names = {
                int(member["id"]): member.get("display_name") or member.get("name") or ""
                for member in cached.get("members", [])
            }

    try:
        if page == "economy":
            reading = db.read_guild_economy_sync(guild_id, limit=12)
            # What the server's own fee switch is set to, from the same key the
            # bot reads — so the page cannot describe a fee that is off here, and
            # the bank figure below has a stated origin rather than appearing as
            # money from nowhere.
            stored = db.read_setting_sync(guild_id, meowfee.SETTING_KEY, None)
            reading["fee"] = {
                "enabled": meowfee.parse_enabled(stored) is not False,
                "label": meowfee.fee_label(),
                # `meowfee.describe()` is written for *Discord*, where `**2%**`
                # is bold. This console prints the sentence as text, so the
                # asterisks arrive on the page instead of the emphasis — the
                # same stripping the dashboard's own fee row already does, and
                # it was missing on exactly one of the two readers, which is how
                # `/owner/economy` came to show a literal `**2%**`.
                "describe": str(meowfee.describe()).replace("**", ""),
                "cap": meowfee.MAX_FEE,
                "stored": stored is not None,
            }
            for holder in reading["top"]:
                holder["name"] = names.get(holder["user_id"], "")
                # What share of everything this server holds is this member's.
                # Computed here rather than in the template because it is a
                # division by a total that can be zero, and a template doing
                # arithmetic on "no economy yet" divides by it.
                total = reading["circulation"] or 0
                holder["share"] = (holder["net"] / total * 100) if total else 0.0
            context["economy"] = reading
        else:
            reading = db.read_guild_levels_sync(guild_id, limit=12)
            for earner in reading["top"]:
                earner["name"] = names.get(earner["user_id"], "")
                # How far into the current level this member is, as the pair the
                # rank card draws: XP earned inside the band, and the band's own
                # width. From the same function the card uses, so the bar here
                # and the bar in Discord cannot disagree.
                _level, into, needed = levels_module.calculate_level_from_xp(earner["xp"])
                earner["into"] = into
                earner["needed"] = needed
                earner["progress"] = (into / needed * 100) if needed else 0.0
            # The role each level grants, named where the guild can be read.
            role_names = {}
            if guild is not None:
                role_names = {
                    int(role.id): role.name for role in getattr(guild, "roles", []) or []
                }
            reading["roles"] = [
                {**row, "name": role_names.get(row["role_id"], "")}
                for row in reading["roles"]
            ]
            context["levels"] = reading
    except Exception as e:
        # A page that cannot read its own table says so rather than drawing an
        # empty leaderboard, which would read as "nobody here has earned
        # anything" — the one reading that is worse than an error.
        print(f"[DASHBOARD] ⚠️ Could not read the {page} tables: {e}", flush=True)
    return context


def _management_context(guild_id, page) -> dict:
    """The roster and the server's own controls, for the two pages that draw them.

    Empty everywhere else, deliberately: this is the one part of this page that
    costs Discord a request, and a page about the composer has no business asking
    for a member list.

    **Synchronous on purpose**, because it is called while a page is being
    rendered: the roster comes out of the gateway's own cache (no await), and the
    three readings that genuinely need Discord's loop — the ban list, the
    lockdown ledger, the audit trail — go through :func:`_loop_reading` and say so
    when there is no loop to use.
    """
    context = {
        "page": page if page in ("members", "server") else "",
        "read": False,
        "why": "",
        "roster": None,
        "bans": [],
        "control": None,
        "query": "",
        "recent": [],
        # What this deployment may actually do here, so a control that cannot work
        # says so *before* it is pressed rather than after. Filled for both pages,
        # because a kick needs a permission exactly as a lockdown does.
        "abilities": None,
    }
    if context["page"] == "":
        return context
    guild = _live_guild(guild_id)
    if guild is None or not _may_act_here(guild_id):
        context["why"] = (
            "I am not in that server right now, so there is nothing to read — a "
            "restart or a removal makes this list empty rather than wrong."
            if guild is None
            else "This sign-in does not reach that server."
        )
        return context
    context["read"] = True
    context["abilities"] = _bot_abilities(guild)
    try:
        if context["page"] == "members":
            context["query"] = (request.args.get("q") or "").strip()
            cached = moderation_actions.roster_cached(guild, context["query"])
            if cached is not None:
                context["roster"] = cached
            else:
                # An empty cache is not an empty server: ask Discord once, and on
                # a host with no loop say that instead.
                roster, read = _loop_reading(
                    moderation_actions.roster(guild, context["query"]),
                    {"members": [], "matched": 0, "shown": 0, "cache": 0, "queried": False, "complete": False},
                )
                context["roster"] = roster
                if not read:
                    context["why"] = (
                        "Discord's member cache is empty and the bot is not "
                        "connected, so I could not ask — the **Server Members** "
                        "intent is what fills this list."
                    )
                    context["read"] = False
                    return context
            bans, _read = _loop_reading(moderation_actions.banned(guild, limit=20), [])
            context["bans"] = bans if isinstance(bans, list) else []
        else:
            control, read = _loop_reading(server_control.state(guild), None)
            if not read:
                # The plan is pure and needs nothing but the guild object, so even
                # without the ledger the page can say what a lockdown *would* do.
                work = server_control.plan(guild)
                control = {
                    "locked": 0,
                    "channels": [],
                    "counts": work.counts,
                    "plan": work.sentence(),
                    "ledger": False,
                }
            # The plan is composed for a Discord card (``server_control``'s own
            # sentence, counts in bold) and this page prints it as prose — the
            # same strip the checklist's rows get, applied at the one door both
            # branches above come through.
            if isinstance(control, dict) and control.get("plan"):
                control["plan"] = _plain_marks(control["plan"])
            context["control"] = control
        recent, _read = _loop_reading(db.recent_web_actions(guild_id=guild_id, limit=12), [])
        context["recent"] = recent if isinstance(recent, list) else []
    except Exception as exc:  # noqa: BLE001 — a reading that breaks must not break the page
        context["read"] = False
        context["why"] = f"I could not read that just now ({type(exc).__name__})."
        print(f"[PANEL] management context failed: {exc}", flush=True)
    return context


#: The member actions the panel may ask for, and the Discord permission each one
#: needs the *bot* to hold. Named here so a refusal can say which permission is
#: missing rather than "that failed", and so the page cannot offer an action the
#: endpoint would refuse.
PANEL_MEMBER_ACTIONS = ("warn", "timeout", "timeout_remove", "kick", "ban", "unban", "clear_warnings")
#: The whole-server actions the panel may ask for.
PANEL_SERVER_ACTIONS = ("lockdown", "unlock", "slowmode")
#: Everything ``/api/server-action`` accepts — which is *not* the same claim as
#: what a page offers. ``resync`` is asked for by the **Commands** page's own
#: button, a form that names its action in a hidden field rather than through
#: the dropdown `PANEL_SERVER_ACTIONS` draws, and it lives here as well as there
#: so the endpoint's list and the page's request are the same list. They were
#: not: the button shipped while the action was absent from the only thing the
#: endpoint compared against, so the single control the page offers for a stale
#: command picker answered *Unknown action* to every press. `leave` and `block`
#: are deliberately **not** here — they are the console's, because a server's
#: own administrator must not be able to make the bot abandon the server.
SERVER_ACTIONS = PANEL_SERVER_ACTIONS + ("resync",)


@app.route("/api/member-action", methods=["POST"])
@_form_door
def api_member_action():
    """Do one thing to one member, from the website.

    Same authority as every other dashboard write (the session's principal, the
    CSRF token, the server it names), plus the member guard the slash commands
    use — and one row in ``web_actions`` either way, because a panel action has
    no Discord audit entry naming the person who asked for it.
    """
    denied = _require_auth_api()
    if denied:
        return denied

    data = _json_or_form()
    payload, status = _member_action_result(data)
    return jsonify(payload), status


def _member_action_result(data):
    """One member action, from either door, as ``(payload, status)``.

    The JSON route a script calls and the dashboard's own member rows come
    through here together, so the guard that refuses moderating an administrator
    (the same one ``/kick`` has always used), the ``web_actions`` row naming the
    person who asked, and the sentence the page shows are one implementation
    rather than two that drift.
    """
    guild_id = _resolve_guild_id(data.get("guild_id"))
    if guild_id is None or not _may_act_here(guild_id):
        return {"success": False, "error": "That server is not reachable from this sign-in."}, 403

    action = str(data.get("action") or "")
    if action not in PANEL_MEMBER_ACTIONS:
        return {"success": False, "error": "Unknown action"}, 400

    guild = _live_guild(guild_id)
    if guild is None:
        return {
            "success": False,
            "error": "I am not connected to that server right now, so nothing was changed.",
        }, 409

    principal = _principal()

    async def work():
        actor_member = await _actor_member(guild, principal.user_id)
        target = await _target_member(guild, data.get("user_id"), action)
        if target is None:
            return {
                "success": False,
                "error": "That member is not in this server any more — reload the page and try again.",
            }, 404
        outcome = await moderation_actions.apply(
            moderation_actions.Request(
                guild=guild,
                action=action,
                target=target,
                actor=moderation_actions.Actor(
                    id=int(principal.user_id or 0),
                    name=principal.name or "the dashboard",
                    member=actor_member,
                    source="dashboard",
                ),
                reason=str(data.get("reason") or "No reason provided"),
                minutes=_int_or_none(data.get("minutes")) or 0,
                delete_days=_int_or_none(data.get("delete_days")) or 0,
            )
        )
        await db.record_web_action(
            scope=principal.scope,
            actor_id=principal.user_id,
            actor_name=principal.name,
            guild_id=guild_id,
            action=action,
            target=outcome.target_name or str(outcome.target_id),
            ok=outcome.ok,
            detail=outcome.detail,
            at=time.time(),
        )
        payload = outcome.to_dict()
        payload["said"] = (
            f"{outcome.verb} — case #{outcome.case_id}."
            if outcome.ok and outcome.case_id
            else (f"{outcome.verb} done." if outcome.ok else outcome.detail)
        )
        return payload, (200 if outcome.ok else 409)

    return _on_bot_loop(work())


async def _target_member(guild, raw_id, action):
    """The member an action is about: a member for most, a user for an unban."""
    user_id = _int_or_none(raw_id)
    if not user_id or user_id <= 0:
        return None
    member = guild.get_member(user_id)
    if member is not None:
        return member
    if action == "unban":
        fetch_user = getattr(guild, "fetch_user", None)
        if callable(fetch_user):
            try:
                return await fetch_user(user_id)
            except Exception:  # noqa: BLE001 — a user Discord will not name
                return None
        return None
    fetch = getattr(guild, "fetch_member", None)
    if callable(fetch):
        try:
            return await fetch(user_id)
        except Exception:  # noqa: BLE001 — left the server between page and click
            return None
    return None


def _on_bot_loop(coro, timeout: float = 20.0):
    """Run one coroutine on the bot's event loop and get ``(payload, status)`` back.

    The dashboard is Flask on the *main* thread and the gateway lives on the
    bot's loop; a Discord call made anywhere else either races that loop or
    raises. This is the one place that bridges them, and it is why every write
    from this file becomes a small function scheduled onto that loop rather than
    a second event loop of its own.

    Returns a payload and a status rather than a response, so each route decides
    what to do with a success (the console's leave, for instance, writes its
    audit row afterwards).
    """
    import concurrent.futures

    loop = getattr(BOT_INSTANCE, "loop", None) if BOT_INSTANCE is not None else None
    if loop is None or not loop.is_running():
        # The coroutine nobody is going to run is closed here rather than left
        # to the garbage collector: an un-awaited one is a RuntimeWarning per
        # request, and for a call that had already opened something the close is
        # what lets it go. Found by the reaction-role tests, whose stand-in bot
        # has no loop — the branch every "the bot is restarting" request takes.
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return {
            "success": False,
            "error": "The bot is not connected right now, so nothing was changed. Try again in a moment.",
        }, 409
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        result = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        return {
            "success": False,
            "error": "Discord did not answer in time — check that server for what actually happened.",
        }, 504
    except Exception as exc:  # noqa: BLE001 — a refusal, never a 500
        print(f"[PANEL] action failed: {exc}", flush=True)
        return {"success": False, "error": f"That did not go through ({type(exc).__name__})."}, 409
    # A coroutine that answers ``(payload, status)`` is a route's; one that answers
    # a plain dict or list is a *reading* (the roster, the ban list, the audit
    # trail) and gets 200. Both shapes exist because the readings are also called
    # from a page render, where there is no status to give.
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
        return result
    return result, 200


@app.route("/api/member-warnings")
def api_member_warnings():
    """One member's warnings, for the panel's own list.

    A read, so it is a GET rather than another action: the page asks this when
    somebody selects a member, and a *read* that went through the write endpoint
    would put a line in the audit trail saying somebody did something.
    """
    denied = _require_auth_api()
    if denied:
        return denied

    guild_id = _resolve_guild_id(request.args.get("guild_id"))
    if guild_id is None or not _may_act_here(guild_id):
        return jsonify({"success": False, "error": "That server is not reachable from this sign-in."}), 403
    user_id = _int_or_none(request.args.get("user_id"))
    if not user_id:
        return jsonify({"success": False, "error": "No member given."}), 400
    rows, _status = _on_bot_loop(_read_warnings(guild_id, user_id))
    return jsonify({"success": True, "warnings": rows.get("warnings", [])})


async def _read_warnings(guild_id, user_id):
    """The warning rows, read on the bot's loop like every other database read here."""
    return {"warnings": await db.get_warnings(guild_id, user_id)}


@app.route("/api/server-action", methods=["POST"])
@_form_door
def api_server_action():
    """Lock the server down, lift it, or set slowmode — as one recorded action."""
    denied = _require_auth_api()
    if denied:
        return denied

    data = _json_or_form()
    payload, status = _server_action_result(data)
    return jsonify(payload), status


def _server_action_result(data):
    """One whole-server action, from either door, as ``(payload, status)``.

    Shared with the dashboard's own Server page for the reason
    :func:`_member_action_result` is: a lockdown asked for from a form and one
    asked for from a script must be the same lockdown, recorded the same way.
    """
    guild_id = _resolve_guild_id(data.get("guild_id"))
    if guild_id is None or not _may_act_here(guild_id):
        return {"success": False, "error": "That server is not reachable from this sign-in."}, 403

    action = str(data.get("action") or "")
    if action not in SERVER_ACTIONS:
        return {"success": False, "error": "Unknown action"}, 400
    guild = _live_guild(guild_id)
    if guild is None:
        return {
            "success": False,
            "error": "I am not connected to that server right now, so nothing was changed.",
        }, 409

    if action == "resync":
        # The one fix for the one failure this page can see and nothing else can:
        # a command list Discord's own client cached and will not reread. It goes
        # through the helper the switches already use, so a republish asked for
        # from this page and one asked for by flipping a switch are one request —
        # and it is **not** a moderation act, so it writes no ``web_actions`` row.
        if not _resync_commands(guild_id):
            return {
                "success": False,
                "error": "This deployment cannot republish this server's command list right now.",
            }, 409
        return {
            "success": True,
            "said": "Asked Discord to republish this server's command list — its own client caches the picker, so give it a minute.",
        }, 200

    principal = _principal()
    seconds = _int_or_none(data.get("seconds")) or 0

    async def work():
        actor = moderation_actions.Actor(
            id=int(principal.user_id or 0),
            name=principal.name or "the dashboard",
            member=await _actor_member(guild, principal.user_id),
            source="dashboard",
        )
        if action == "lockdown":
            report = await server_control.lockdown(guild, actor)
        elif action == "unlock":
            report = await server_control.unlock(guild, actor)
        else:
            report = await server_control.set_slowmode(guild, seconds, actor)
        await db.record_web_action(
            scope=principal.scope,
            actor_id=principal.user_id,
            actor_name=principal.name,
            guild_id=guild_id,
            action=action,
            target="" if action != "slowmode" else f"{seconds}s",
            ok=report.ok,
            detail=report.sentence() if not report.ok else report.detail,
            at=time.time(),
        )
        payload = report.to_dict()
        return payload, (200 if report.ok else 409)

    return _on_bot_loop(work())


@app.route("/api/console/server-action", methods=["POST"])
@_form_door
def api_console_server_action():
    """The operator's own controls over a server as a whole.

    Three of them, and each is a thing only the operator can do: **leave** (the
    bot removes itself from a server — Discord has no other way for an
    application to do that), **block** (leave *and* refuse to come back, because
    a blocklist a re-invite can walk around is a note to self rather than a
    rule), and **resync** (republish that server's command list, which is the
    fix for the one failure an operator can see from here and nowhere else).
    """
    denied = _require_operator_api()
    if denied:
        return denied

    data = _json_or_form()
    guild_id = _int_or_none(data.get("guild_id"))
    action = str(data.get("action") or "")
    if not guild_id:
        return jsonify({"success": False, "error": "No server given."}), 400
    if action not in ("leave", "block", "unblock", "resync"):
        return jsonify({"success": False, "error": "Unknown action"}), 400

    actor = (session.get("discord_user") or {}).get("username") or "the operator"

    if action == "resync":
        # Answered **here**, not on the bot's loop, because this one is already
        # written for the Flask thread: ``_resync_commands`` is what the
        # dashboard's own switch calls, and ``bot.resync_commands``
        # (``bot.py``'s ``resync_guild_commands``) queues the Discord writes on
        # the loop itself and returns whether it managed to. This endpoint used
        # to look the hook up and ``await`` it instead — and a plain function is
        # not awaitable, so the operator's Republish button answered *"Discord
        # refused the republish (TypeError)"* on every press, naming Discord for
        # a mistake of ours. It also refuses for a server the bot is not in,
        # which is why the hook's own answer is what is reported.
        if not _resync_commands(guild_id):
            return jsonify({
                "success": False,
                "error": "This deployment cannot republish that server's command list right now.",
            }), 409
        return jsonify({
            "success": True,
            "said": "Asked Discord to republish that server's command list — its own client caches the picker, so give it a minute.",
        }), 200

    async def work():
        guild = _live_guild(guild_id)
        if action == "unblock":
            removed = await db.unblock_guild(guild_id)
            return {
                "success": True,
                "said": (
                    "That server is welcome again — it was on the list and no longer is."
                    if removed
                    else "That server was not on the list, so nothing changed."
                ),
            }, 200
        if guild is None:
            return {
                "success": False,
                "error": "I am not in that server, so there is nothing to leave or republish.",
            }, 409
        reason = f"{actor}: left from the console" if action == "leave" else f"{actor}: blocked from the console"
        if action == "block":
            await db.block_guild(guild_id, str(data.get("reason") or ""), actor, time.time())
        try:
            await guild.leave()
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "error": f"Discord refused that ({type(exc).__name__}) — the bot is still there.",
            }, 409
        said = (
            "Left that server. It is on the blocklist, so an invite will be refused too."
            if action == "block"
            else "Left that server. An invite can add me again."
        )
        # The audit row is written on the same loop as the action, so a leave that
        # succeeded cannot end up with no record of who asked for it.
        await db.record_web_action(
            scope="operator",
            actor_id=0,
            actor_name=actor,
            guild_id=guild_id,
            action=action,
            target=str(guild_id),
            ok=True,
            detail=said,
            at=time.time(),
        )
        return {"success": True, "said": said, "reason": reason}, 200

    body, status = _on_bot_loop(work())
    return jsonify(body), status


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


def _console_id(value) -> str:
    """A Discord id as **text**, because a JavaScript number cannot hold one.

    A snowflake is 18-19 digits and a double is exact to about 16, so an id that
    crosses to the app as a JSON *number* can come back as a different server —
    and it did, on the live console. ``String(Number("1542278896148422676"))``
    is ``"1542278896148422700"``: that is what the address bar showed, why
    ``/owner/servers/…42700`` answered **"No such server"** for a server it had
    just listed on the page behind it, and why the systems matrix — the one
    screen drawn as a switch per server — looked its own string keys up with a
    rounded number and painted **"?" in every cell**. Both symptoms are one
    defect, and the fix is to stop arithmetic from touching an identifier: ids
    cross this boundary as text, and everything that reads one parses it back
    with ``int()``.

    The *server-side* rows keep their integers, deliberately: ``modules.CACHE``
    and every SQL statement are keyed by the same ``int`` the gateway hands out,
    so a string there would be a second cache entry for one server.
    """
    return str(int(value))


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


def _page_nav(pages, current: str, query: str = "", groups=()) -> list[dict]:
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

    ``groups`` is optional and, when given, names the heading each page belongs
    under (:data:`OWNER_GROUPS`). A page it does not mention keeps an empty
    group, which the rail draws under no heading at all — so the failure mode of
    forgetting to place a new page is "it is still there", never "it is gone".
    """
    placed = {
        page_id: label for label, page_ids in groups for page_id in page_ids
    }
    return [
        {
            "id": spec_id,
            "label": label,
            "href": path,
            "anchor": anchor,
            "url": f"{path}{query}",
            "current": spec_id == current,
            "group": placed.get(spec_id, ""),
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
    ("live", "Live", "/owner/live", "live"),
    ("servers", "Servers", "/owner/servers", "servers"),
    ("systems", "Systems", "/owner/systems", "systems"),
    ("commands", "Commands", "/owner/commands", "commands"),
    ("economy", "Economy", "/owner/economy", "economy"),
    ("ops", "Ops", "/owner/ops", "ops"),
    ("questions", "Questions", "/owner/questions", "questions"),
    # The support bridge, on its own page because a conversation is not a card:
    # the transcript is the thing an operator came for, and a room's worth of
    # messages in the middle of the questions inbox would be the page's longest
    # section with the least to do with its heading.
    ("support", "Support", "/owner/support", "support"),
    ("access", "Access", "/owner/access", "access"),
    ("changes", "Changes", "/owner/changes", "changes"),
    # The tier, last, because it is the last place an operator should be and the
    # four actions it holds are the only ones in this console that are not about
    # one server. It is a *page* rather than a modal hung off the fleet list so
    # that its address can be bookmarked and, more usefully, so that the actions
    # have somewhere to live that is not one hurried click away from Oversight.
    ("god", "God level", "/owner/god", "god"),
)

#: The rail's groups, as (label, page ids) — **the console has nine pages and a
#: flat list of nine is where finding one stops being instant** (Hick's law: the
#: time to choose grows with the number of choices). Grouping is the remedy the
#: law itself prescribes, and it is applied here rather than in the React app so
#: that the rail is still drawn from the server's own page list: a page added
#: above that nobody placed in a group shows up ungrouped rather than vanishing
#: from the navigation.
#:
#: ``tests/test_design_laws.py`` holds the arithmetic: no group longer than five,
#: no page missing from the groups, and nothing named twice.
OWNER_GROUPS = (
    ("Fleet", ("live", "servers", "systems")),
    ("Work", ("commands", "economy", "ops", "questions", "support")),
    ("Owner", ("access", "changes", "god")),
)

#: Each page's own heading and its one-sentence answer to *why open this*.
#:
#: Declared beside the page table and the groups for the same reason they are
#: here: the rail, the routes and the words have to agree. A page added above with
#: no copy draws an empty heading, which is the failure mode to prefer over a
#: missing page — but the point of writing them down together is that neither
#: happens silently, and `tests/test_owner_console.py` walks the table.
OWNER_PAGE_COPY = {
    "overview": (
        "The whole deployment",
        "What is running, what is answering, and what is asking for attention — read from the "
        "same functions the bot's own panels read, not from a screen built to look like them.",
    ),
    "live": (
        "Live",
        "The reading as it stands right now: the gateway, the bot's own heartbeat, and the "
        "voice rooms one server is actually sitting in.",
    ),
    "servers": (
        "Servers",
        "Every server the bot is in, most populated first, with the switches of whichever one "
        "is in hand.",
    ),
    "systems": (
        "Systems",
        "Every system in the registry, and how many of the servers have it switched on.",
    ),
    "commands": (
        "Commands",
        "What this deployment publishes, and what Discord came back with the last time it was "
        "told.",
    ),
    "economy": (
        "Economy",
        "One server's two ledgers — what its members hold and what the bank charges — read from "
        "the readers its own dashboard draws.",
    ),
    "ops": (
        "Ops",
        "The host: what the monitor saw, what the site guard made of this process, and what the "
        "configuration is missing.",
    ),
    "questions": (
        "Questions",
        "What strangers asked the bot in their own servers, and whether each one has an answer.",
    ),
    "support": (
        "Support",
        "The bot's own support channel, held from here: write a message and it is posted "
        "into the room as the bot with your name under it, and the replies people write "
        "there come back to this page.",
    ),
    "access": (
        "Access",
        "Which sessions exist, where each came from, and whether anybody is being refused right "
        "now.",
    ),
    "changes": (
        "Changes",
        "The changelog this deployment ships, newest first.",
    ),
    "god": (
        "God level",
        "The four actions that are not about one server. They sit behind a second door and a "
        "clock, and each of them is written to the audit trail with the reason it was asked for.",
    ),
}

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

@app.route("/owner/login", methods=["GET", "POST"])
def owner_login():
    """Operator sign-in page, dedicated to the /owner console."""
    if request.method == "POST":
        if not validate_csrf():
            return _owner_login_page(
                "Your session expired. Please refresh and try again.", 403
            )

        ip = _client_ip()
        if not _login_allowed(ip):
            _SIGN_IN.note_refused()
            return _owner_login_page(
                "Too many failed attempts. Wait a few minutes and try again.", 429
            )

        if not ADMIN_PASSKEY:
            _record_login_failure(ip)
            return _owner_login_page("Admin passkey is not configured in this deployment.", 401)

        entered_passkey = request.form.get("passkey") or ""
        if secrets.compare_digest(entered_passkey, ADMIN_PASSKEY):
            _clear_login_failures(ip)
            _begin_session()
            session["authenticated"] = True
            session.permanent = True
            session["auth_scope"] = principal.SCOPE_GLOBAL
            _record_session(label="passkey")
            get_csrf_token()
            return redirect(_door_target("/owner"))

        _record_login_failure(ip)
        return _owner_login_page("Invalid admin passkey. Please try again.", 401)

    if is_authenticated() and _principal().is_operator:
        return redirect("/owner")
    return _owner_login_page()


def _owner_systems_matrix(fleet: list[dict]) -> tuple[list[dict], int]:
    """Every system in the registry, how many servers have it on — and each one's cell.

    One read per server rather than one per server per system: ``get_states``
    caches per guild, and eleven calls per server would be eleven chances to read
    a cache that moved underneath the page.

    ``read`` is reported per row rather than swallowed, for the reason this file
    keeps repeating: a system shown as *off everywhere* because the states could
    not be read would send the operator to fix something that is running.

    **Every row carries a cell per server** (``cells``), because the count alone
    is a number nobody can act on: *"on in 3 of 5"* tells an operator that two
    servers have a system off and gives them nowhere to switch it on. Each cell
    carries the server's id **as text** — a snowflake in a hidden field that
    crosses as a number comes back rounded, which is how a switch aimed at one
    server writes to another (``_console_id`` documents that defect from the live
    console) — plus whether that server's states could be read at all, so a cell
    whose state is unknown draws *unknown* rather than a confident off.
    """
    states_by_server: dict[int, dict | None] = {}
    for row in fleet:
        try:
            states_by_server[row["id"]] = modules.get_states(row["id"])
        except Exception as exc:  # noqa: BLE001
            print(f"[CONSOLE] could not read module state for {row['id']}: {exc}", flush=True)
            states_by_server[row["id"]] = None
    read = sum(1 for states in states_by_server.values() if states is not None)
    rows = []
    for module in AVAILABLE_MODULES:
        on = 0
        cells = []
        for row in fleet:
            states = states_by_server[row["id"]]
            known = states is not None
            value = bool(states.get(module["id"], features.default_enabled(module["id"]))) if known else False
            if known and value:
                on += 1
            cells.append({
                "guild_id": _console_id(row["id"]),
                "server": row["name"],
                "enabled": value,
                # A state the cache could not answer is neither on nor off, and
                # the switch says so rather than offering a toggle whose press
                # would be writing a guess into the database.
                "known": known,
                # A system a plan withholds cannot be switched on, and pressing
                # it anyway gets a refusal — so the cell says why up front.
                "allowed": bool(plans.feature_allowed(row["id"], module["id"])),
            })
        rows.append({
            **module,
            "on": on,
            "read": read,
            # The operator's own word about this system, or ``None`` for automatic.
            # The page draws its lock control from this and nothing else, so a row
            # cannot say "locked" beside a system no lock is on — the count and
            # the control come from the one read.
            "override": modules.override_for(module["id"]),
            # The registry's own answer, so the page can say what a server that
            # has never touched a switch would be doing.
            "default_enabled": features.default_enabled(module["id"]),
            "cells": cells,
        })
    # Most-switched-on first: the question this page answers is whether anything
    # is still on anywhere, and the answer is at the top if it is.
    rows.sort(key=lambda row: (-row["on"], row["name"].lower()))
    return rows, read


def _owner_recent(limit: int = 12) -> dict:
    """The last few god-level actions, with the clock already readable.

    The formatting belongs here rather than in the template: the trail stores an
    epoch, and a page that prints `1758650000.12` beside a sentence is a page
    whose reader has to do arithmetic to answer "was that today?".

    **Readability is reported, not swallowed** — and this is the one place in this
    rebuild where a defect was found by reading the page rather than the code. The
    reader underneath returns an empty list both for "nothing has happened" and
    "the audit table does not exist in this database", and those are different
    answers: on a fresh deployment or a database that predates the table, a page
    that said *Nothing yet* would be claiming the second thing with the first
    one's sentence. So the flag comes back with the rows, and the page picks its
    sentence from the flag.
    """
    raw, read = _loop_reading(db.recent_web_actions(guild_id=0, limit=40), [])
    rows = []
    for row in (raw or []):
        if str(row.get("scope") or "") != "god":
            continue
        when = row.get("at")
        rows.append({
            **row,
            "when": time.strftime("%d %b · %H:%M", time.localtime(float(when))) if when else "",
        })
        if len(rows) >= limit:
            break
    return {"rows": rows, "read": bool(read)}


def _owner_ledger() -> dict:
    """The session ledger, each row's clock already readable, for the Access page."""
    reading = sessions.reading(current_sid=session.get(sessions.COOKIE_KEY))
    rows = []
    for row in reading.get("rows") or []:
        started = row.get("started_at")
        rows.append({
            **row,
            "when": time.strftime("%d %b · %H:%M", time.localtime(float(started))) if started else "",
        })
    return {**reading, "rows": rows}


def _owner_context(page: str, guild_id=None) -> dict:
    """Everything one console page is drawn from, computed in one place.

    The console **reads** where it is not acting, and every reading below comes
    from the function the matching ``/api/console/*`` endpoint already calls — so
    the page an operator has open and the API a script would call cannot answer
    differently about the same server, which is the defect this project keeps
    meeting in its own history.

    Not every page computes everything, and this is the one place the console
    departs from the dashboard's rule on purpose. The dashboard pays for "every
    page computes every rule" with one server and seven sections; the readings
    here are per *fleet* — the systems matrix is a read per server, the economy is
    two ledgers per server — so a page that computed all of them to draw one of
    them would be a page nobody could open on a host that has been asleep. What
    every page shares *is* computed every time: the fleet, its totals, the
    gateway, the tier's own state and the server in hand.
    """
    fleet = _fleet_rows()
    totals = _console_totals(fleet)
    wanted = str(guild_id or "").strip()
    selected = None
    if fleet:
        selected = next(
            (row for row in fleet if _console_id(row["id"]) == wanted), None
        ) or fleet[0]

    title, lede = OWNER_PAGE_COPY.get(page, OWNER_PAGE_COPY["overview"])
    pause_state = pause.state(refresh=True)
    started, connected = _gateway_state()
    nav = _page_nav(OWNER_PAGES, page, groups=OWNER_GROUPS)
    # The rail is the groups with their pages, plus anything the groups did not
    # place — so a page added to the table and not to a group is still reachable,
    # which is the failure mode `_page_nav` documents one level down.
    # The home page first, then the groups. `OWNER_GROUPS` deliberately leaves it
    # out of every group — one page is the console's front door rather than a
    # member of a set, and `tests/test_design_laws.py` holds that arithmetic — so
    # the ungrouped bucket is not an overflow, it is the first row. Drawn last, the
    # front page of the console was the final link in its own rail.
    ungrouped = [item for item in nav if not item["group"]]
    rail = [{"label": "", "pages": ungrouped}] if ungrouped else []
    rail += [
        {"label": label, "pages": [item for item in nav if item["group"] == label]}
        for label, _ids in OWNER_GROUPS
    ]

    # Where a form on *this* page is sent back to, once. The console's controls
    # are forms that post to the same endpoints the JSON API answers, so the
    # redirect has to carry the server in hand — otherwise flipping a switch on a
    # small server lands back on the biggest one, and the operator watches a
    # switch they did not touch. `_console_id` rather than the raw int for the
    # reason that function documents: this value is read back out of a query
    # string by the same matcher that picks the row.
    page_path = "/owner" + ("" if page == "overview" else f"/{page}")
    page_url = page_path + (f"?guild_id={_console_id(selected['id'])}" if selected else "")

    context = {
        "console_page": page,
        "page_title": title,
        "page_lede": lede,
        "page_anchor": _page_anchor(OWNER_PAGES, page),
        "page_url": page_url,
        # The server every form on this page names, as text — a snowflake field
        # that crosses as a number comes back rounded, which is how a switch for
        # one server writes to another.
        "console_guild": _console_id(selected["id"]) if selected else "",
        "rail": rail,
        "operator": session.get("discord_user") or {},
        "csrf_token": get_csrf_token(),
        "fleet": fleet,
        "totals": totals,
        "selected": selected,
        "gateway": {**_badge_state(), "started": started, "key": _gateway_label(started, connected)},
        "bot": {"name": _bot_display_name(), "ready": _bot_ready(), "ping_ms": _bot_ping_ms()},
        "uptime_seconds": max(0, int(time.time() - PROCESS_STARTED_AT)),
        "god": {
            "locked": not _god_unlocked(),
            "remaining_seconds": _god_remaining(),
            "unlock_seconds": GOD_UNLOCK_SECONDS,
            "passkey_enabled": bool(ADMIN_PASSKEY),
            "paused": pause_state,
            "paused_sentence": pause.describe(pause_state),
            "actions": [
                {
                    "id": action_id,
                    "label": label,
                    "blurb": blurb,
                    "reason_required": bool(needs_reason),
                    # Whether it would change anything right now, so a control
                    # that would do nothing says so instead of being pressed to
                    # find out — the same flag the API hands the old app.
                    "is_set": (
                        (action_id == "pause" and pause_state["paused"])
                        or (action_id == "resume" and not pause_state["paused"])
                    ),
                }
                for action_id, label, blurb, needs_reason in GOD_ACTIONS
            ],
            "recent": _owner_recent(12),
        },
        # A sentence carried across a POST, read once. Popped rather than read, so
        # a reload cannot re-announce an action that has already happened.
        # Stripped for the reason the dashboard's pair is: these sentences are
        # written once and read by two doors, and the console is the door that
        # renders them as prose.
        "notice": _plain_marks(session.pop("owner_notice", "")),
        "error": _plain_marks(session.pop("owner_error", "")),
    }

    if page == "live" and selected:
        context["live"] = _overview_live(selected["id"])
    if page in ("servers", "systems") and selected:
        context["systems"], context["systems_known"] = _console_systems_for(selected["id"])
    if page == "systems":
        context["matrix"], context["matrix_read"] = _owner_systems_matrix(fleet)
        # The column heads, taken from the first row rather than read from the
        # fleet in the template: if the registry were ever empty there would be
        # no rows and therefore no cells, and a header built by indexing row 0
        # would raise on a page whose whole job is to draw an empty registry
        # honestly.
        context["matrix_cells"] = context["matrix"][0]["cells"] if context["matrix"] else []
    if page == "commands":
        context["commands"] = {
            "registry": features.command_count(),
            "published": len(_command_tree_names()),
            "publish": command_publish.describe(0, expected=features.command_count()),
            "switchable": len(AVAILABLE_MODULES),
        }
    if page == "economy" and selected:
        ledgers = _progression_context(selected["id"], "economy")
        context["economy"] = ledgers["economy"]
        # The money **as a page prints it** — the same function the member
        # dashboard's own economy page draws from, called here rather than
        # re-derived, so the two surfaces cannot disagree about a server's held
        # coins and neither template does arithmetic on a circulation of zero.
        #
        # The console used to hand the template the raw ledger and render it with
        # ``facts()``, which walks a mapping's own **field names** — so the page
        # read `Enabled True · Label 2% · Describe … · Cap 500 · Stored False`,
        # i.e. it printed its reader's variable names at the one person who is
        # supposed to be reading numbers. A generic renderer is for a mapping
        # whose fields are unknown; this one's fields are the ledger's, and they
        # have names a person would use once they are written down.
        context["economy_display"] = _economy_display(ledgers["economy"])
        # The bank fee as its own reading rather than a nested mapping for a
        # generic renderer to walk: three labelled facts and one sentence, stated
        # by the same function the dashboard's card uses, so the two surfaces
        # cannot describe one rate two ways.
        context["economy_fee"] = _fee_display((ledgers["economy"] or {}).get("fee"))
        # The four moves the adjust endpoint accepts, plus `set`, and the bound
        # `move_economy_sync` enforces — read here rather than typed in the
        # template, because the ceiling is a database constant and a form that
        # advertised a different one would offer a number the write refuses.
        context["economy_actions"] = sorted(ECONOMY_ACTIONS) + ["set"]
        context["economy_ceiling"] = db.MAX_ECONOMY_ADJUSTMENT
    if page == "ops":
        # The three readings in words, from the one function that states them —
        # rather than a mapping handed to `facts()`, which is what this page used
        # to do and why its reader met `status / at / stale / ratio 30d / age
        # seconds / age text` on the one page whose whole job is to be read when
        # something is wrong. `facts()` also renders scalars and skips a list, so
        # the monitor rows — the names behind "2 monitors, 1 up" — never drew.
        context["health"] = _health_display(
            uptime=uptime_status.snapshot(),
            site=site_guard.health_json(),
            config=config_audit.health_json(),
        )
        # Presence, not values: `health_json` counts because `/health` is public;
        # this console is not, so the names come with it. The same reason the
        # console's JSON overview already carries them.
        context["health"]["config"]["named"] = config_audit.attention_json()
    if page == "questions":
        context["inbox"] = qna.owner_inbox_sync(limit=20)
    if page == "support":
        # The bridge's whole reading — the address, the two permissions that
        # decide whether it works, and the tail of the conversation — from the
        # same function the JSON endpoint and the dashboard's own card call. The
        # server in hand is only the fallback for naming the id: the channel is a
        # *deployment* setting, and the server an operator happens to be reading
        # is very often not its home.
        context["support"] = _support_card(
            guild=(
                BOT_INSTANCE.get_guild(_int_or_none(selected["id"]) or 0)
                if BOT_INSTANCE and selected
                else None
            )
        )
    if page == "access":
        context["ledger"] = _owner_ledger()
        context["limits"] = {
            "sign-in": _SIGN_IN.snapshot(),
            "passkey": _PASSKEY.snapshot(),
            "send": _SEND.snapshot(),
        }
        context["doors"] = {
            "passkey_enabled": bool(ADMIN_PASSKEY),
            "discord_elevation": OWNER_DISCORD_ELEVATION,
            "visitor_address_signed": bool(OWNER_PROXY_SECRET),
            "ledger_writable": sessions.writable(),
        }
    if page == "changes":
        entries, total = hub.read_changelog(HUB_CHANGELOG, limit=hub.CHANGES_SHOWN)
        context["changes"] = entries
        context["changes_total"] = total
    return context


def _god_open(entered: str) -> dict:
    """Open the tier for this browser, and say what happened — once, for both doors.

    The same credential as the front door and the same limiter window: an extra
    place to type a passkey is an extra place to guess one. Extracted from the JSON
    endpoint because the served console asks the same question, and two
    implementations of one check is how a door and the API behind it come to
    disagree about what "wrong passkey" means.
    """
    if not ADMIN_PASSKEY:
        return {
            "ok": False,
            "status": 409,
            "error": "No owner passkey is configured in this deployment, so this tier cannot be opened.",
        }
    if _passkey_attempts_left() <= 0:
        return {
            "ok": False,
            "status": 429,
            "error": "Too many attempts. Wait a few minutes and try again.",
        }
    if not entered or not secrets.compare_digest(entered, ADMIN_PASSKEY):
        _record_passkey_failure()
        _god_audit("god_unlock", ok=False, detail="wrong passkey")
        # `locked: True` is not decoration: the browser app reads a 403 as "your
        # session is dead" unless it carries that flag, so a mistyped passkey
        # answered with "Your sign-in is no longer valid" — a sentence that is
        # false and hides the server's own message.
        return {
            "ok": False,
            "status": 403,
            "error": "That is not the owner passkey.",
            "locked": True,
        }
    session["god_until"] = time.time() + GOD_UNLOCK_SECONDS
    _god_audit("god_unlock", ok=True, detail=f"open for {GOD_UNLOCK_SECONDS // 60} minutes")
    return {
        "ok": True,
        "status": 200,
        "said": "God level is open for this browser.",
        "remaining_seconds": _god_remaining(),
    }


def _god_close() -> dict:
    """Close the tier now rather than waiting for the clock.

    An operator who is finished should not have to remember that a page is still
    armed; this is what makes the unlock a thing you hold rather than a thing you
    did.
    """
    was_open = _god_unlocked()
    session.pop("god_until", None)
    if was_open:
        _god_audit("god_lock", ok=True, detail="closed early")
    return {"ok": True, "status": 200, "said": "God level is closed."}


def _god_perform(action: str, reason: str) -> dict:
    """One fleet-wide action, performed once, reported to whoever asked.

    Extracted from the JSON endpoint for the reason ``_god_open`` was: the served
    console and the API must not disagree about what "turn every system off" does.
    The sentences are built here too, because both callers print them — and a
    message written twice is the drift this file keeps writing laws about.
    """
    if action not in GOD_ACTION_IDS:
        return {"ok": False, "status": 400, "error": "Unknown god action"}

    needs_reason = next(spec[3] for spec in GOD_ACTIONS if spec[0] == action)
    if needs_reason and not reason:
        return {
            "ok": False,
            "status": 400,
            "error": "Give a reason — it is what the next person to read the trail will have.",
        }

    _actor_id, actor_name = _god_actor()

    if action in ("pause", "resume"):
        paused = action == "pause"
        state_after = pause.set_state(paused, actor=actor_name, reason=reason)
        _god_audit(
            "god_pause" if paused else "god_resume",
            ok=True,
            detail=reason or "no reason given",
        )
        return {
            "ok": True,
            "status": 200,
            "paused": state_after,
            "said": pause.describe(state_after),
        }

    # The two fleet switches, and they are deliberately not the same write twice.
    #
    # They used to be one `set_enabled` per server per system — which a server's
    # own administrator could undo the moment it landed, because it wrote the
    # very row their dashboard writes. An operator who turns every system off is
    # usually doing it *because* something is wrong, and a switch the person
    # causing the problem can flip back is not a switch. So **off** now takes the
    # decision away from the servers with an operator override, which
    # `modules.get_states` applies last, and **on** is the action that hands it
    # back: it releases those overrides and then switches every system on, server
    # by server, so a guild that refuses is named rather than lost inside a bulk
    # write.
    #
    # The per-guild half of *off* is gone rather than kept beside the override.
    # It would be eleven writes per server to say what one override already says
    # everywhere, and the rows it left behind would be a second answer to what a
    # server wants — which is exactly what releasing the lock is meant to restore.
    enabled = action == "fleet_on"
    fleet_ids = _god_fleet_ids()
    if not fleet_ids:
        return {
            "ok": False,
            "status": 409,
            "error": "The bot is not in any server right now, so there is nothing to switch.",
        }
    changed = 0
    failed: list[str] = []
    if not enabled:
        for module in AVAILABLE_MODULES:
            try:
                if modules.set_operator_override(module["id"], False):
                    changed += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{module['id']} ({type(exc).__name__})")
    else:
        for module in AVAILABLE_MODULES:
            try:
                # ``None`` is "no opinion", so the server's own saved row is
                # what the system falls back to — and the loop below then
                # switches it on for every server that may have it.
                modules.set_operator_override(module["id"], None)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{module['id']} (release: {type(exc).__name__})")
        for guild_id in fleet_ids:
            for module in AVAILABLE_MODULES:
                try:
                    modules.set_enabled(guild_id, module["id"], enabled)
                    changed += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{guild_id}:{module['id']} ({type(exc).__name__})")

    _god_audit(
        "god_fleet_on" if enabled else "god_fleet_off",
        ok=not failed,
        detail=(reason or "no reason given")
        + f" — {len(fleet_ids)} servers, {changed} switches"
        + (f", {len(failed)} refused" if failed else ""),
    )
    return {
        "ok": True,
        "status": 200,
        "changed": changed,
        "servers": len(fleet_ids),
        "failed": failed[:20],
        "error": (
            f"{len(failed)} switch(es) were refused — see the audit trail for which."
            if failed
            else None
        ),
        "said": None
        if failed
        else (
            (
                f"Every system is on again across {len(fleet_ids)} "
                f"server{'s' if len(fleet_ids) != 1 else ''}, and every fleet lock "
                "is released — each server's own switches decide again."
            )
            if enabled
            else (
                f"Every system is off across {len(fleet_ids)} "
                f"server{'s' if len(fleet_ids) != 1 else ''} and **force-closed**: "
                "their administrators cannot switch any of it back on until you "
                "release it here."
            )
        ),
    }


@app.route("/owner")
@app.route("/owner/<path:sub>")
def owner_console(sub: str = ""):
    """One page of the operator console. Operator only — see the note above.

    This served a bundled browser app, and it now serves the console again: one
    page per entry in ``OWNER_PAGES``, rendered from the same readers the
    ``/api/console/*`` endpoints answer with. The app is archived with the rest of
    the pre-rebuild surfaces, so the address that used to hand out its shell — and
    answered ``TemplateNotFound`` once the shell went — draws its own pages.

    The **scope test** is the page's first line, unchanged: a one-time link is a
    real sign-in that belongs to one server, so it is sent to a place it can act
    instead of being given a fleet it may not touch.
    """
    if not is_authenticated():
        return redirect("/owner/login")
    if not _principal().is_operator:
        if session.get("owner_candidate"):
            # The owner, signed in through Discord, and still short of the
            # console: the passkey page is the only thing that can raise the
            # scope, so hiding it behind this redirect left the "Owner Discord
            # Sign In" button leading nowhere the owner could return from.
            return redirect("/owner/login")
        # A Discord sign-in goes to the picker rather than to a dashboard, so
        # nobody is dropped onto a server they did not choose.
        return redirect("/servers" if _visible_guilds() else "/dashboard")

    page = (sub or "overview").strip("/") or "overview"
    if page not in OWNER_PAGE_COPY:
        # An address that names no page of this console is a wrong address, and
        # the styled 404 already says so with ways onward.
        abort(404)
    return render_template(
        "owner.html",
        # The nav's own two outward links, named here rather than defaulted inside
        # the template: this host is a *proxy* of one slice of the application, so
        # a link to either other host has to be an address. Both were a default
        # written into the markup, and each was wrong its own way: `public_site_url`
        # — a name nothing in this file ever set, so the link was a hard-coded
        # address that would have gone on saying `dacat.pages.dev` after the site
        # moved — and a second, hand-typed copy of the member dashboard's address.
        # The same two readers `_owner_login_page` uses, so the two pages of this
        # console cannot name different addresses for one surface.
        site_url=hub.marketing_url(),
        dashboard_url=hub.dashboard_url(),
        **{**_site_context(), **_owner_context(page, request.args.get("guild_id"))},
    )


@app.route("/owner/god", methods=["POST"])
def owner_god_action():
    """The god level's own door and its four actions, from the page itself.

    Three requests arrive here — *unlock*, *lock*, and one of the four fleet-wide
    actions — and each of them goes through the same helper the JSON API uses, so
    the served console cannot perform a variant of an action the API would refuse.

    A form rather than a fetch, and that is a decision rather than a preference:
    the actions are the most consequential controls this project has, and a
    control that only works while a script loaded is a control that fails exactly
    when something is wrong. CSRF is checked, the tier's clock is checked, and the
    answer is a sentence on the page it came from (POST, then redirect, so a reload
    cannot repeat an action — the one repeat that would be genuinely dangerous).
    """
    if not is_authenticated():
        return redirect("/owner/login")
    if not _principal().is_operator:
        return redirect("/owner/login")
    if not validate_csrf():
        session["owner_error"] = "That form had expired. Nothing was done — try again."
        return redirect("/owner/god")

    action = str(request.form.get("action") or "").strip()
    reason = str(request.form.get("reason") or "").strip()

    if action == "unlock":
        outcome = _god_open(str(request.form.get("passkey") or ""))
    elif action == "lock":
        outcome = _god_close()
    elif action in GOD_ACTION_IDS:
        if not _god_unlocked():
            outcome = {
                "ok": False,
                "status": 403,
                "error": "God level is locked. Unlock it with the owner passkey first.",
            }
        else:
            outcome = _god_perform(action, reason)
    else:
        outcome = {"ok": False, "status": 400, "error": "Unknown god action"}

    if outcome.get("ok") and not outcome.get("error"):
        session["owner_notice"] = outcome.get("said") or "Done."
    else:
        session["owner_error"] = outcome.get("error") or "That did not work."
    return redirect("/owner/god")


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


#: God level: the actions that affect **every** server at once, and the reason
#: they are behind a second door.
#:
#: Everything else in this console is scoped to a thing — a server, a wallet, a
#: command list — and a mistake there is a mistake about one server. The four
#: actions below are not: a pause stops the bot answering anywhere, and the two
#: fleet switches write every server's system state at once. They are the kind of
#: control that a page cannot make safe with an "are you sure?" box, because the
#: answer to that question is always yes when somebody is in a hurry.
#:
#: So the tier is a *time-limited unlock* rather than a second password on a
#: second page: the operator proves the passkey again, the console opens the four
#: actions for a quarter of an hour, and every one of them lands in the same
#: audit trail as the rest of the website's actions, with the actor's name and
#: the reason they typed. ``tests/test_god.py`` pins the three properties that
#: make it worth having: it is off by default, it expires, and it is audited.
GOD_UNLOCK_SECONDS = 15 * 60

#: ``(id, label, blurb, needs_a_reason)``, in the order the console offers them.
#: Declared here rather than in the React app for the same reason the pages are:
#: the server is the only party that may decide what this tier can do, and the
#: app renders the list it is handed — including the reason it must ask for.
@app.route("/owner/override", methods=["POST"])
def owner_override():
    """The operator's word about one system, for every server at once.

    A form rather than a fetch, for the reason ``/owner/god`` gives: this is the
    control that takes a decision away from every server's administrator, and a
    control that works only while a script is loaded is a control that fails
    exactly when something is wrong. CSRF is checked and the answer is a sentence
    on the page it came from.

    The three values are a **state** rather than two switches, and the third one
    is the whole point: ``off`` is the operator's word, ``on`` is the same word in
    the other direction, and ``auto`` *clears* it — which is not the same as off.
    "No opinion" has to hand the system back to each server's own saved row, or
    releasing a lock would silently be a second fleet-wide decision.
    """
    if not is_authenticated():
        return redirect("/owner/login")
    if not _principal().is_operator:
        return redirect("/owner/login")
    guild_id = str(request.form.get("guild_id") or "").strip()
    back = "/owner/systems" + (f"?guild_id={guild_id}" if guild_id else "")
    if not validate_csrf():
        session["owner_error"] = "That form had expired. Nothing was done — try again."
        return redirect(back)

    module_id = str(request.form.get("module") or "").strip()
    wanted = str(request.form.get("state") or "").strip().lower()
    if module_id not in ALLOWED_MODULE_IDS or wanted not in ("on", "off", "auto"):
        session["owner_error"] = "That is not a system this console can lock."
        return redirect(back)

    enabled = {"on": True, "off": False, "auto": None}[wanted]
    if not modules.set_operator_override(module_id, enabled):
        session["owner_error"] = (
            f"**{module_id}** could not be locked: it is either a core system with "
            "no switch, or one a licence withholds — a plan-gated system can be "
            "closed for the fleet but not forced open without one."
        )
        return redirect(back)

    _god_audit(
        "god_override",
        ok=True,
        detail=f"{module_id} → {'on' if enabled else 'off' if enabled is False else 'auto'}",
    )
    session["owner_notice"] = {
        "on": f"**{module_id}** is now on in every server, and its switches are locked.",
        "off": (
            f"**{module_id}** is now off in every server and force-closed — its "
            "administrators cannot switch it back on until this is released."
        ),
        "auto": (
            f"**{module_id}** is back on each server's own switch; whatever they "
            "had saved is what applies now."
        ),
    }[wanted]
    return redirect(back)


GOD_ACTIONS = (
    (
        "pause",
        "Pause the bot everywhere",
        "Every server's commands are refused until it is resumed. The gateway stays "
        "connected and both panels keep answering, so this is the switch to reach for "
        "while something is being fixed rather than a way to stop the process.",
        True,
    ),
    (
        "resume",
        "Resume the bot everywhere",
        "Commands are answered again on every server, with each server's own systems "
        "exactly as they were left.",
        False,
    ),
    (
        "fleet_off",
        "Switch every system off, and force-close it fleet-wide",
        "One operator override per system, applied over every server's own switch, "
        "so a server's administrator cannot turn any of it back on. That is the "
        "point of it — this is the switch for *something is wrong*. Releasing it is "
        "the action below, which hands each server its own switches back.",
        True,
    ),
    (
        "fleet_on",
        "Release the fleet lock, and turn every system on",
        "Every override is cleared first, so a server's own saved choice is what its "
        "switches fall back to, and then every system is switched on server by "
        "server. This enables systems a server's admin had deliberately switched "
        "off, which is why it asks for a reason.",
        True,
    ),
)

GOD_ACTION_IDS = tuple(action[0] for action in GOD_ACTIONS)


def _god_remaining() -> int:
    """Seconds left on this session's god unlock, or ``0`` when it is locked."""
    until = float(session.get("god_until") or 0)
    return max(0, int(until - time.time()))


def _god_unlocked() -> bool:
    return _god_remaining() > 0


def _god_actor() -> tuple[object, str]:
    """Who is acting, as the audit trail records them: an id and a name."""
    user = session.get("discord_user") or {}
    return user.get("id") or 0, str(user.get("username") or "the operator")


def _god_fleet_ids() -> list[int]:
    """Every server the bot is in right now, as ids.

    From the gateway rather than the directory: the directory remembers servers
    the bot has *seen*, which includes the ones it has since left, and a fleet
    write that touched those would be a write about nothing.
    """
    if BOT_INSTANCE is None:
        return []
    try:
        return [int(guild.id) for guild in BOT_INSTANCE.guilds]
    except Exception as exc:  # noqa: BLE001
        print(f"[CONSOLE] could not read the fleet for a god action: {exc}", flush=True)
        return []


def _require_operator_api():
    if not is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    if not _principal().is_operator:
        return jsonify({"success": False, "error": "Operator sign-in required"}), 403
    if request.method != "GET" and not validate_csrf():
        return jsonify({"success": False, "error": "Invalid or missing CSRF token"}), 403
    return None


def _require_god_api():
    """The operator gate **and** the god unlock, for the four fleet actions.

    Deliberately cumulative rather than an alternative: being the operator is not
    the same claim as having just re-proved the passkey, and the second one is
    what the console's unlock form is asking for.
    """
    denied = _require_operator_api()
    if denied:
        return denied
    if not _god_unlocked():
        return jsonify({
            "success": False,
            "error": "God level is locked. Unlock it with the owner passkey first.",
            "locked": True,
        }), 403
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
                # The operator's override, so this table's switch can say
                # *locked* rather than letting the press be refused: the state
                # above already reflects it, and a switch that shows Off with no
                # explanation is a switch somebody will press twice.
                "override": modules.override_for(module["id"]),
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
    # ``_console_id``: the list is drawn as links, and a link built from a
    # rounded id is a 404 on the page it points at. See the helper.
    return jsonify({
        "success": True,
        "servers": [{**row, "id": _console_id(row["id"])} for row in fleet],
        "totals": _console_totals(fleet),
    })


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
        return _console_no_server()

    systems, known = _console_systems_for(guild_id)
    # Whether this server is on the blocklist, read through the same bridge every
    # other crossing in this file uses. The page offers **Block** or **Unblock**
    # from this and from nothing else — they are not alternatives, and a page that
    # guessed would be offering to block a server that is already blocked.
    blocked, _read = _loop_reading(db.blocked_guild(guild_id), None)
    return jsonify({
        "success": True,
        "server": {**row, "id": _console_id(row["id"]), "known": known},
        "systems": systems,
        # The difference between the registry's total and its switchable count, by
        # name — printing two numbers about one word without naming the gap is the
        # defect ``features.always_on_names`` exists to stop.
        "always_on": list(features.always_on_names()),
        "publish_at": _last_publish_at(guild_id),
        "blocked": blocked if isinstance(blocked, dict) else None,
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
        matrix[_console_id(row["id"])] = {
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
                "id": _console_id(row["id"]),
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


# ---------------------------------------------------------------------------
# The Live page: what one server is *doing*, as opposed to what it is standing on.
#
# Every other page here answers a question about configuration — which switch is
# on, which command is published, which balance is in the table. None of them can
# answer the one an operator asks while looking at a server in Discord: *who is
# in voice, which channels exist, and where is anybody actually talking.* That
# answer lives in the gateway's own caches, is true for about as long as it takes
# to read it, and is the whole reason this page exists.
#
# Two rules hold every reading below, and they are the same two the public
# figures follow: a reading that could not be taken is **absent rather than
# zero**, because "nobody is in voice" and "I could not ask" are different
# claims and only one of them is worth acting on; and every id crosses to the app
# as **text**, because a JavaScript number cannot hold a snowflake.
# ---------------------------------------------------------------------------

#: How many channels one Live payload carries before it stops listing and starts
#: counting. A server with 300 channels is an ordinary server, and a payload that
#: dutifully carried all of them would be megabytes of JSON for a page that shows
#: a screenful — the rest are reported as a count under the list.
LIVE_CHANNEL_LIMIT = 150

#: The statuses a member can be in, in the order the page reads them. ``offline``
#: swallows ``invisible`` because Discord reports both as "offline" to everyone
#: who is not the member themselves, and inventing a seventh word for it would be
#: a distinction the gateway never made.
LIVE_STATUSES = ("online", "idle", "dnd", "offline")


def _live_channel_source(guild) -> list:
    """Every channel of a guild, from whichever attribute it exposes.

    ``Guild.channels`` is the real one and the only complete one — categories,
    forums, stages and the two kinds an operator thinks about, in one list. The
    split lists are the fallback for a gateway *stand-in* (this project's own
    preview harness, and the suite's fakes), which is not hypothetical: an
    endpoint that answers "0 channels" against a stand-in is an endpoint nobody
    can eyeball, and this console is debugged by eyeballing it.
    """
    if guild is None:
        return []
    listed = list(getattr(guild, "channels", None) or ())
    if listed:
        return listed
    merged: dict = {}
    for name in (
        "text_channels",
        "voice_channels",
        "forum_channels",
        "stage_channels",
        "categories",
    ):
        for channel in list(getattr(guild, name, None) or ()):
            merged[getattr(channel, "id", id(channel))] = channel
    return list(merged.values())


def _live_channel_kind(channel) -> str:
    """What kind of channel this is, by Discord's own type rather than by class.

    ``isinstance`` would be the obvious test and it is the wrong one: this
    project's own test fakes are not ``discord.TextChannel``, and a page that only
    worked against the real library is a page the suite cannot check. The enum's
    ``name`` is what both of them carry.
    """
    raw = getattr(channel, "type", None)
    kind = str(getattr(raw, "name", raw) or "").lower()
    if kind in {"voice", "stage_voice"}:
        return "voice"
    if kind == "category":
        return "category"
    if kind in {"forum", "news_thread", "public_thread", "private_thread"}:
        return "forum"
    return "text"


def _live_voice_person(state, member, user_id) -> dict:
    """One person in one voice channel, with the four flags that matter.

    Read from the *state* where there is one and from the member otherwise, so
    the row is right with or without the Server Members intent — the flags live
    on ``VoiceState`` in the payload the gateway sends, and on ``Member.voice``
    once Discord has resolved the member.
    """
    flags = state if state is not None else getattr(member, "voice", None)
    return {
        "id": _console_id(user_id) if user_id else "",
        "name": _live_name(member) if member is not None else "",
        "bot": bool(getattr(member, "bot", False)),
        # Both kinds of mute in one word, because from outside the channel they
        # look the same and the difference (server-muted vs muted by choice) is a
        # moderation detail no operator reads a roster for.
        "muted": bool(getattr(flags, "mute", False) or getattr(flags, "self_mute", False)),
        "deafened": bool(
            getattr(flags, "deaf", False) or getattr(flags, "self_deaf", False)
        ),
        "streaming": bool(getattr(flags, "self_stream", False)),
        "video": bool(getattr(flags, "self_video", False)),
        "suppressed": bool(getattr(flags, "suppress", False)),
    }


def _live_voice_people(guild, channel) -> list[dict]:
    """Who is sitting in one voice channel.

    Two sources on purpose. ``Guild.voice_states`` is the gateway's own map and is
    complete the moment the connection is up; ``VoiceChannel.members`` is that map
    resolved against the member cache, which is empty without the Server Members
    intent. Where the states exist they win — a name that is missing beats a
    person who is missing.
    """
    people: list[dict] = []
    seen: set = set()
    states = getattr(guild, "voice_states", None) or {}
    for state in list(states.values()):
        if getattr(state, "channel", None) is not channel:
            continue
        user_id = getattr(state, "user_id", None)
        member = getattr(state, "user", None)
        if member is None and user_id:
            member = guild.get_member(int(user_id))
        people.append(_live_voice_person(state, member, user_id))
        seen.add(str(user_id or ""))
    for member in list(getattr(channel, "members", ()) or ()):
        user_id = getattr(member, "id", None)
        if str(user_id or "") in seen:
            continue
        people.append(_live_voice_person(getattr(member, "voice", None), member, user_id))
    people.sort(key=lambda person: (person["bot"], person["name"].lower(), person["id"]))
    return people


def _live_presence(guild):
    """Who is online, as counts — or ``None`` when nobody can say.

    Presence is the one reading here that needs a privileged intent, so it is the
    one that is routinely unavailable — and **the intent is the only honest test
    for that**, which is what this guard is for. The first version of this
    function asked whether ``Member.status`` existed, on the theory that without
    the intent the attribute would be missing. It is not: discord.py always has
    it, and without the intent it is ``offline`` for every member. So the page
    printed **1 online / 35 offline** for a server with people in it — the one
    online being the bot, whose own presence this process sets and therefore
    knows — and an operator reading five figures had no way to tell that four of
    them were a fact about this deployment's configuration rather than about
    anybody in the server. ``None`` is the honest answer, and the page draws the
    dash and the reason instead of a measurement nobody took.
    """
    bot = BOT_INSTANCE
    intents = getattr(bot, "intents", None) if bot is not None else None
    # Both, because Discord's own rule is that presences arrive for the members
    # this client tracks: the Presence intent without the Members intent is a
    # subscription to updates about a roster that is not being kept. "Did we ask
    # for it" is still a different question from "did it work" — the counts
    # below can only be as complete as the member list they walk.
    if not (getattr(intents, "presences", False) and getattr(intents, "members", False)):
        return None
    members = list(getattr(guild, "members", None) or ())
    if not members:
        return None
    counts = {status: 0 for status in LIVE_STATUSES}
    counts["streaming"] = 0
    read = 0
    for member in members:
        status = getattr(member, "status", None)
        if status is None:
            continue
        key = str(getattr(status, "name", status) or "").lower()
        if key == "invisible":
            key = "offline"
        if key not in counts:
            continue
        counts[key] += 1
        read += 1
        for activity in list(getattr(member, "activities", ()) or ()):
            kind = str(getattr(getattr(activity, "type", None), "name", "")).lower()
            if kind == "streaming" and getattr(activity, "name", None):
                counts["streaming"] += 1
                break
    if not read:
        return None
    counts["total"] = read
    return counts


def _live_channel_activity(guild_id: int) -> dict:
    """What the tracker counted per channel, for today and the last seven days.

    Synchronous, like every other read this side of the project makes, and against
    the tracker's own two indexed tables so the window means exactly what
    ``/activity week`` means. Tables that are not there are reported as *not read*
    rather than as zeros — a build machine has no ``bot.db``, and a page printing
    "0 messages" for a server holding thousands is worse than one that says it
    could not read.
    """
    start = figures.period_start("week")
    today = figures.period_start("today")
    messages: dict[str, dict] = {}
    voice: dict[str, dict] = {}
    conn = db.connect()
    try:
        for label, floor in (("week", start), ("today", today)):
            for channel_id, total in conn.execute(
                "SELECT channel_id, SUM(count) FROM activity_messages "
                "WHERE guild_id = ? AND date >= ? GROUP BY channel_id",
                (guild_id, floor),
            ):
                row = messages.setdefault(_console_id(channel_id), {"today": 0, "week": 0})
                row[label] = int(total or 0)
            for channel_id, total in conn.execute(
                "SELECT channel_id, SUM(seconds) FROM activity_voice "
                "WHERE guild_id = ? AND date >= ? GROUP BY channel_id",
                (guild_id, floor),
            ):
                row = voice.setdefault(_console_id(channel_id), {"today": 0, "week": 0})
                row[label] = int(total or 0)
    except sqlite3.OperationalError:
        # No schema to read — see the docstring. Narrow on purpose: anything else
        # is a real fault in a real database and belongs to the caller.
        return {"read": False, "messages": {}, "voice": {}, "since": start}
    finally:
        conn.close()
    return {"read": True, "messages": messages, "voice": voice, "since": start}


def _live_talkers(guild_id: int, guild) -> list[dict]:
    """The people talking most this week, by messages, with their voice time.

    Counts come from the tracker; names come from the gateway, because the stored
    rows only ever held the activity. A member who has left is left out of the
    list rather than shown as a bare id — the totals above still count them.
    """
    start = figures.period_start("week")
    conn = db.connect()
    try:
        rows = list(conn.execute(
            "SELECT user_id, SUM(count) FROM activity_messages "
            "WHERE guild_id = ? AND date >= ? GROUP BY user_id "
            "ORDER BY 2 DESC LIMIT 8",
            (guild_id, start),
        ))
        voice = {
            _console_id(user_id): int(seconds or 0)
            for user_id, seconds in conn.execute(
                "SELECT user_id, SUM(seconds) FROM activity_voice "
                "WHERE guild_id = ? AND date >= ? GROUP BY user_id",
                (guild_id, start),
            )
        }
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    talkers = []
    for user_id, total in rows:
        member = guild.get_member(int(user_id)) if guild is not None else None
        if member is None:
            continue
        talkers.append({
            "id": _console_id(user_id),
            "name": _live_name(member),
            "messages": int(total or 0),
            "voice_seconds": voice.get(_console_id(user_id), 0),
        })
    return talkers


def _live_bot_voice() -> list[dict]:
    """Where *this bot* is connected to voice, if it is anywhere.

    Read from the bot rather than from any server's payload, so the page can say
    where the bot itself is sitting — which is the one voice connection an
    operator is responsible for.
    """
    clients = getattr(BOT_INSTANCE, "voice_clients", None) if BOT_INSTANCE else None
    rows = []
    for client in list(clients or ()):
        guild = getattr(client, "guild", None)
        channel = getattr(client, "channel", None)
        rows.append({
            "guild_id": _console_id(getattr(guild, "id", 0) or 0),
            "guild": getattr(guild, "name", "") or "",
            "channel": getattr(channel, "name", "") or "",
            "connected": bool(getattr(client, "is_connected", lambda: False)()),
            "playing": bool(getattr(client, "is_playing", lambda: False)()),
            "paused": bool(getattr(client, "is_paused", lambda: False)()),
        })
    return rows


def _live_radio(guild_id) -> dict:
    """What the radio is doing in one server, and what this host can do.

    Three halves in one block because they answer one question — *is the radio
    working here, and if not, why not* — and the last two are the ones nobody can
    discover from the page otherwise. The capability reading comes from
    :mod:`utils.voice_caps`, the same probe ``/radio doctor``, the boot line and
    ``/api/voice-debug`` read, so a console that said "fine" while the bot's own
    doctor said otherwise is not a state this can reach.

    ``read.player`` is false when this guild has no player yet, which is a
    different sentence from "a player that is idle" — the first means nobody has
    started the radio since the last restart.

    The stream path is the one that cannot be inferred from either of the others:
    every capability can be present, every station URL healthy and the host
    firewalled off from the outside world. It is read from the **weekly pass's own
    verdict** rather than asked live, because this is a page that polls and
    opening a station is a network round trip — so the panel says when it was last
    asked rather than pretending to have asked.
    """
    try:
        from utils import media_health, radio_stream, voice_caps  # noqa: PLC0415

        caps = voice_caps.probe().to_dict()
        stream = media_health.stream_state()
    except Exception as exc:
        return {
            "read": {"player": False, "caps": False},
            "why": f"the radio modules could not be read ({type(exc).__name__})",
            "caps": None,
            "player": None,
            "stream": None,
            "catalogue": None,
        }

    entry = None
    try:
        key = int(guild_id or 0)
    except (TypeError, ValueError):
        key = 0
    holder = getattr(radio_stream, "PLAYERS", {}).get(key)
    if holder is not None:
        try:
            entry = holder.status()
        except Exception as exc:
            entry = {"error": f"{type(exc).__name__}: {exc}"}
    # The width of the table, which is the one reading here that is about the
    # *members* rather than about the host. It is also the only place the language
    # and country coverage can be read without opening Discord: "is there anything
    # here in my language" is a question a server's administrator gets asked, and
    # its answer was previously a number nobody had.
    catalogue = None
    try:
        from utils import radio_catalogue  # noqa: PLC0415

        index = radio_catalogue.index()
        catalogue = {
            "stations": index.stations,
            "languages": len(index.languages),
            "countries": len(index.countries),
            "wordless": index.wordless,
            "unlabelled": index.unlabelled,
            "ad_free": index.ad_free,
        }
    except Exception:
        catalogue = None
    return {
        "read": {"player": holder is not None, "caps": True},
        "why": "" if caps.get("playable") else "; ".join(caps.get("missing") or []),
        "caps": caps,
        "player": entry,
        "stream": stream,
        "catalogue": catalogue,
    }


def _overview_live(guild_id) -> dict:
    """What this server is doing *right now*, for the page somebody opens first.

    The Overview used to answer four questions about the **deployment** — ping,
    systems, members, commands — and none about the server, so an administrator
    opening the page they open first learned how fast the gateway was and nothing
    about the room they were about to walk into. The page read as empty because it
    was: four numbers and a great deal of space.

    So this is read off the gateway the way the fleet console reads it, on the same
    rule as everything else here: it reports what it could read and says so when it
    could not, rather than painting a zero over a cold process. ``alone`` is carried
    rather than inferred, because a single person in a room is the one reading on
    this page that the companion acts on.
    """
    guild = _live_guild(guild_id)
    if guild is None:
        return {
            "read": False,
            "why": (
                "The gateway has not answered for this server yet, so there is "
                "nothing to read. A free host sleeps when nobody knocks."
            ),
            "rooms": [],
            "people": 0,
            "alone": 0,
            "radio": None,
        }
    rooms: list[dict] = []
    people = 0
    alone = 0
    try:
        for channel in _live_channel_source(guild):
            if _live_channel_kind(channel) != "voice":
                continue
            members = [
                row for row in _live_voice_people(guild, channel) if not row.get("bot")
            ]
            if not members:
                continue
            only = len(members) == 1
            people += len(members)
            alone += 1 if only else 0
            rooms.append({
                "id": str(getattr(channel, "id", "")),
                "name": getattr(channel, "name", "a voice channel"),
                "people": len(members),
                "alone": only,
                "live": any(row.get("streaming") for row in members),
            })
    except Exception as exc:  # noqa: BLE001 — a roster that cannot be read is a sentence
        return {
            "read": False,
            "why": f"The voice roster could not be read ({type(exc).__name__}).",
            "rooms": [],
            "people": 0,
            "alone": 0,
            "radio": None,
        }
    rooms.sort(key=lambda row: (-row["people"], row["name"]))
    return {
        "read": True,
        "why": "",
        "rooms": rooms[:8],
        "people": people,
        "alone": alone,
        "radio": _live_radio(guild_id),
    }


def _live_companion(guild, guild_id, channels: list[dict]) -> dict:
    """Who is alone right now, who is live, and where the companion is sitting.

    One block because it answers one question an operator asks — *is anybody
    sitting in a room on their own, and is the bot in there with them?* — and the
    answer is what the companion acts on. Nothing here is stored: the rooms are
    read off the same voice rosters the page has just been handed, so a room that
    emptied in the last second is already gone from the list.

    The limit is stated rather than papered over: **a bot cannot watch a Go Live
    stream.** Discord has no API for it and the gateway carries no video, so what
    the ``streaming`` flag means here is the fact (``self_stream`` on somebody's
    voice state), and the console shows the fact. A page that showed a picture
    would be showing somebody something that does not exist.
    """
    rooms = []
    live = []
    for channel in channels:
        if channel["kind"] != "voice":
            continue
        people = channel["people"]
        for person in people:
            if person["bot"]:
                continue
            if person["streaming"] or person["video"]:
                live.append({
                    "id": person["id"],
                    "name": person["name"],
                    "channel_id": channel["id"],
                    "channel": channel["name"],
                    "streaming": person["streaming"],
                    "video": person["video"],
                })
        # Somebody on their own: one person in the room and nobody else at all.
        # A second *bot* in there counts, which is the same rule the companion
        # itself follows — a radio bot is company of a kind, and a room with two
        # silent bots in it is not a room where anybody is alone.
        if len(people) == 1 and not people[0]["bot"]:
            person = people[0]
            rooms.append({
                "channel_id": channel["id"],
                "channel": channel["name"],
                "category": channel["category"],
                "person_id": person["id"],
                "person": person["name"],
                "streaming": person["streaming"],
                "video": person["video"],
                "seconds": None,
            })

    cog = None
    getter = getattr(BOT_INSTANCE, "get_cog", None) if BOT_INSTANCE else None
    if callable(getter):
        try:
            cog = getter("Company")
        except Exception:
            cog = None
    snapshot = {}
    if cog is not None and hasattr(cog, "snapshot"):
        try:
            snapshot = dict(cog.snapshot() or {})
        except Exception:
            snapshot = {}

    # How long each person has been on their own, where the live loop has an
    # opinion. The timer only starts on a voice event, so a room this process has
    # never seen change carries no age rather than a made-up one.
    waiting = {
        str(room.get("channel_id")): int(room.get("waiting_seconds") or 0)
        for room in snapshot.get("waiting") or []
    }
    for room in rooms:
        if room["channel_id"] in waiting:
            room["seconds"] = waiting[room["channel_id"]]

    sitting = snapshot.get("sitting") or None
    if sitting is not None and str(sitting.get("guild_id", "")) != _console_id(guild_id):
        # The companion is somewhere else entirely: said, not shown as if it were
        # in this server, because the console is looking at one server at a time.
        sitting = dict(sitting, elsewhere=True)

    settings = None
    if guild_id:
        settings = company.settings_from(
            db.read_setting_sync(int(guild_id), company.MODE_KEY, None),
            db.read_setting_sync(int(guild_id), company.GREETING_KEY, None),
        )

    return {
        # ``loaded`` is about this *process* rather than about the server: a
        # deployment where the companion never loaded still has live voice rooms
        # (those come from the gateway) and no clock of its own.
        "read": {
            "rooms": guild is not None,
            "loaded": bool(snapshot),
            "settings": settings is not None,
        },
        "settings": (
            {"mode": settings.mode, "greeting": settings.greeting, "on": settings.enabled}
            if settings is not None
            else None
        ),
        "rooms": rooms,
        "live": live,
        "sitting": sitting,
        "grace_seconds": snapshot.get("grace_seconds", company.GRACE_SECONDS),
        "cooldown_seconds": snapshot.get("cooldown_seconds", company.COOLDOWN_SECONDS),
        "blocked_seconds": int(snapshot.get("blocked_seconds") or 0),
        "left_us": list(snapshot.get("left_us") or []),
        "emoji": company.EMOJI_URL,
        "emoji_name": company.EMOJI_NAME,
    }


@app.route("/api/console/live")
def api_console_live():
    """What one server is *doing*: its channels, its voice rooms, its chatter.

    Read-only, and every part of it is a reading rather than a stored fact — so
    the payload carries its own ``read`` block. ``read.channels`` is false when
    the gateway has not answered yet (a cold process knows nothing), ``read.voice``
    the same, ``read.presence`` when the Presence intent is off, and
    ``read.tracker`` when this deployment has no tracker tables. A page that
    painted a zero over any of those would be inventing a measurement.
    """
    denied = _require_operator_api()
    if denied:
        return denied

    row = _console_economy_server(request.args.get("guild_id"))
    if row is None:
        return _console_no_server()

    guild = BOT_INSTANCE.get_guild(row["id"]) if BOT_INSTANCE else None
    fleet = _fleet_rows()

    channels: list[dict] = []
    exceeded = 0
    for channel in _live_channel_source(guild):
        if _live_channel_kind(channel) == "category":
            continue
        if len(channels) >= LIVE_CHANNEL_LIMIT:
            exceeded += 1
            continue
        category = getattr(channel, "category", None)
        channels.append({
            "id": _console_id(getattr(channel, "id", 0)),
            "name": str(getattr(channel, "name", "") or ""),
            "kind": _live_channel_kind(channel),
            "category": str(getattr(category, "name", "") or "") if category else "",
            "category_id": (
                _console_id(getattr(category, "id"))
                if getattr(category, "id", None) is not None
                else ""
            ),
            # Position is what puts the page in the same order as the sidebar an
            # operator is looking at; the name is the tie-break so two channels at
            # the same position do not swap on every poll.
            "position": int(getattr(channel, "position", 0) or 0),
            "topic": str(getattr(channel, "topic", "") or "")[:200],
            "nsfw": bool(getattr(channel, "nsfw", False)),
            "slowmode": int(getattr(channel, "slowmode_delay", 0) or 0),
            "limit": int(getattr(channel, "user_limit", 0) or 0) or None,
            "people": (
                _live_voice_people(guild, channel)
                if _live_channel_kind(channel) == "voice"
                else []
            ),
        })
    channels.sort(key=lambda ch: (ch["category"], ch["position"], ch["name"].lower()))

    activity = _live_channel_activity(row["id"])
    for channel in channels:
        counted = activity["messages"].get(channel["id"], {})
        channel["messages_today"] = counted.get("today", 0)
        channel["messages_week"] = counted.get("week", 0)
        channel["voice_seconds_week"] = activity["voice"].get(channel["id"], {}).get("week", 0)

    voice_channels = [channel for channel in channels if channel["kind"] == "voice"]
    in_voice = sum(len(channel["people"]) for channel in voice_channels)
    # Read once: the block walks every voice roster, and the payload carries it
    # twice over (its own key, and its ``read`` flags under the page's ``read``).
    company_block = _live_companion(guild, row["id"], channels)
    started, connected = _gateway_state()
    # Read once, not twice: each call walks every cached member.
    presence = _live_presence(guild) if guild is not None else None

    return jsonify({
        "success": True,
        "server": {
            "id": _console_id(row["id"]),
            "name": row["name"],
            "icon": row["icon"],
            "nick": row["nick"],
            "members": row["members"],
        },
        "servers": [
            {
                "id": _console_id(entry["id"]),
                "name": entry["name"],
                "icon": entry["icon"],
                "members": entry["members"],
            }
            for entry in fleet
        ],
        "gateway": {
            **_badge_state(),
            # The same three-state key every other console payload carries, so a
            # page can tone its own pill off the same word the topbar's badge is
            # drawn from rather than re-deriving it.
            "key": _gateway_label(started, connected),
            "connected": bool(connected),
            "started": bool(started),
            "ping_ms": _bot_ping_ms(),
        },
        "read": {
            "channels": guild is not None,
            "voice": bool(voice_channels),
            "presence": presence is not None,
            "tracker": activity["read"],
            # One flag for the page's own gap notice, because the sentence a
            # reader needs is "the companion is not running here" rather than
            # three sub-flags. The detail stays in ``company.read``.
            "company": bool(company_block["read"]["loaded"]),
        },
        "presence": presence,
        "channels": channels,
        "exceeded": exceeded,
        "company": company_block,
        "talkers": _live_talkers(row["id"], guild),
        "bot_voice": _live_bot_voice(),
        # What the radio is doing here, and whether this host can play at all. The
        # second half is the reason the block exists: "nobody has started the
        # radio" and "the radio cannot play on this host" used to look identical.
        "radio": _live_radio(row["id"]),
        "totals": {
            "channels": len(channels),
            "text": sum(1 for channel in channels if channel["kind"] == "text"),
            "voice": len(voice_channels),
            "forums": sum(1 for channel in channels if channel["kind"] == "forum"),
            "in_voice": in_voice,
            "messages_today": sum(channel["messages_today"] for channel in channels),
            "messages_week": sum(channel["messages_week"] for channel in channels),
            "voice_seconds_week": sum(channel["voice_seconds_week"] for channel in channels),
            "tracker_since": activity["since"],
        },
        "at": int(time.time()),
    })


#: The actions the console's adjustment form may post, and the field and sign
#: each one means. A table rather than six branches, because the *page* offers
#: these names and the server has to accept exactly the ones it printed.
ECONOMY_ACTIONS = {
    "wallet_add": ("wallet", 1),
    "wallet_remove": ("wallet", -1),
    "bank_add": ("bank", 1),
    "bank_remove": ("bank", -1),
}


def _console_member_name(guild, user_id: int) -> str:
    """A member's display name where the gateway knows it, else ``""``.

    The same rule the fleet's nickname column follows: Discord's cache is the
    authority while it is warm, and "User 1234…" invented from an id would be
    worse than a blank the page can render as a number.
    """
    if guild is None:
        return ""
    try:
        member = guild.get_member(int(user_id))
    except Exception:
        return ""
    if member is None:
        return ""
    return _live_name(member)


def _console_economy_server(requested):
    """The server an economy request is about, or ``None``.

    ``requested`` is text, always — a Discord id that travelled as a JSON number
    is a different number on return (see ``_console_id``). The default is the
    biggest server, which is the one an operator is most likely to have meant.
    """
    fleet = _fleet_rows()
    if not fleet:
        return None
    wanted = str(requested or "").strip()
    if wanted:
        for row in fleet:
            if _console_id(row["id"]) == wanted:
                return row
        return None
    return fleet[0]


def _console_no_server():
    """The refusal for a console request that named no readable server.

    Two different situations reach this and they are fixed by different things,
    so they are not answered with one sentence. A *named* server the bot is not
    in is a wrong address, and ``404 No such server`` is exactly right. But a
    deployment that cannot see a single server is not missing one: nothing has
    answered Discord yet and nothing has been written down yet, which is the
    first seconds of every restart and the whole life of a fresh database.

    Driving the console in that state — a process with no bot and an empty
    directory — is how this showed up: ``/owner/live`` answered **"That reading
    failed. No such server"** for a server that was in Discord the whole time,
    while the Fleet page drew the same emptiness as an empty list. The status is
    ``503`` because that is what this file already answers elsewhere when there
    is nobody to act as (see the questions reply route), and a page has to be
    able to tell "come back in a moment" from "that address is wrong".
    """
    if not _known_guilds():
        return jsonify({
            "success": False,
            "error": (
                "No server can be read yet: the bot has not answered Discord, and "
                "none have been written down yet either, so there is nothing to "
                "read from. This fills in by itself once the handshake finishes."
            ),
        }), 503
    return jsonify({"success": False, "error": "No such server"}), 404


def _fee_enabled_for(guild_id) -> bool:
    """Whether this server charges the DaCat Fee, without an event loop.

    ``meowfee.is_enabled`` is async and the Flask thread cannot await it, so the
    rule is restated over the sync read — and restated *exactly*: absent means
    on, a stored word is read by ``parse_enabled``, and a word it does not
    recognise is on rather than off, which is what ``is_enabled`` does with a
    stored string it cannot parse.
    """
    stored = db.read_setting_sync(guild_id, meowfee.SETTING_KEY)
    if stored is None:
        return True
    parsed = meowfee.parse_enabled(stored)
    return True if parsed is None else parsed


@app.route("/api/console/economy")
def api_console_economy():
    """One server's economy: circulation, the richest members, and the fee.

    Read-only. Writes are ``/api/console/economy/adjust`` below, because a page
    that both reads and moves coins on a GET is one refresh away from a
    mistake.

    The columns are the fleet's own rows, so the picker here cannot offer a
    server the Fleet page does not have, and the ids are strings for the reason
    ``_console_id`` documents.
    """
    denied = _require_operator_api()
    if denied:
        return denied

    row = _console_economy_server(request.args.get("guild_id"))
    if row is None:
        return _console_no_server()

    try:
        limit = max(1, min(100, int(request.args.get("limit", 25))))
    except (TypeError, ValueError):
        limit = 25

    try:
        reading = db.read_guild_economy_sync(row["id"], limit=limit)
    except Exception as exc:
        print(f"[CONSOLE] could not read the economy for {row['id']}: {exc}", flush=True)
        return jsonify({
            "success": False,
            "error": "Could not read this server's economy. Try again in a moment.",
        }), 500

    guild = BOT_INSTANCE.get_guild(row["id"]) if BOT_INSTANCE else None
    top = [
        {
            **entry,
            # Text, for the same reason a server id is: a member id is a
            # snowflake too, and the app must never do arithmetic on one.
            "user_id": _console_id(entry["user_id"]),
            "name": _console_member_name(guild, entry["user_id"]),
        }
        for entry in reading["top"]
    ]
    return jsonify({
        "success": True,
        "server": {**row, "id": _console_id(row["id"])},
        "servers": [
            {
                "id": _console_id(entry["id"]),
                "name": entry["name"],
                "icon": entry["icon"],
                "members": entry["members"],
            }
            for entry in _fleet_rows()
        ],
        "totals": {
            "accounts": reading["accounts"],
            "wallet": reading["wallet"],
            "bank": reading["bank"],
            "circulation": reading["circulation"],
            "meow_bank": reading["meow_bank"],
        },
        "top": top,
        "show_limit": limit,
        "fee": {
            # Unset means on, which is ``meowfee.is_enabled``'s own rule read
            # here without an event loop: the page must not describe a fee this
            # server does not charge, and a server that has never touched the
            # switch does charge it.
            "enabled": _fee_enabled_for(row["id"]),
            "rate": meowfee.fee_label(),
            "min": meowfee.MIN_FEE,
            "max": meowfee.MAX_FEE,
        },
        "actions": sorted(ECONOMY_ACTIONS) + ["set"],
        "ceiling": db.MAX_ECONOMY_ADJUSTMENT,
    })


@app.route("/api/console/economy/adjust", methods=["POST"])
@_form_door
def api_console_economy_adjust():
    """Move coins, or refuse in words that name the number that refused them.

    Reached only by the operator's own sign-in — `_require_operator_api` insists
    on the passkey scope, so this is not a server administrator's control and
    there is no approval flow to skip: `/eco-admin set` in Discord is owner-only
    for the same reason, and both surfaces refuse the same people.

    The refusals are the feature. A debit larger than the balance is answered
    with *"that wallet holds 240 — there is not 10,000 to remove"* rather than
    with a silently clamped balance, which is the defect this project has
    already fixed twice in the bot's own commands (see `move_economy_sync`).
    """
    denied = _require_operator_api()
    if denied:
        return denied

    data = _json_or_form()
    row = _console_economy_server(data.get("guild_id"))
    if row is None:
        return _console_no_server()

    user_id = _int_or_none(data.get("user_id"))
    if user_id is None or user_id <= 0:
        return jsonify({
            "success": False,
            "error": "That is not a member id. Paste the id itself — 17 or 18 digits — not a mention.",
        }), 400

    action = str(data.get("action") or "")
    if action not in ECONOMY_ACTIONS and action != "set":
        return jsonify({"success": False, "error": "Unknown action"}), 400

    guild_id = int(row["id"])
    actor = (session.get("discord_user") or {}).get("username") or "the operator"

    try:
        if action == "set":
            wanted = {}
            for field in ("wallet", "bank"):
                raw = data.get(field)
                if raw in (None, ""):
                    continue
                value = _int_or_none(raw)
                if value is None:
                    return jsonify({
                        "success": False,
                        "error": f"Enter a whole number of coins for the {field}.",
                    }), 400
                wanted[field] = value
            if not wanted:
                return jsonify({
                    "success": False,
                    "error": "Give a wallet, a bank, or both to set.",
                }), 400
            result = db.set_economy_sync(guild_id, user_id, **wanted)
        else:
            field, sign = ECONOMY_ACTIONS[action]
            amount = _int_or_none(data.get("amount"))
            if amount is None or amount <= 0:
                return jsonify({"success": False, "error": "Enter a whole number of coins."}), 400
            result = db.move_economy_sync(guild_id, user_id, field, sign * amount)
    except ValueError as exc:
        # A bound or a bad field name, in the module's own words.
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        print(f"[ECONOMY] ⚠️ could not apply {action} for {user_id}: {exc}", flush=True)
        return jsonify({
            "success": False,
            "error": "Could not save that change. Nothing was moved.",
        }), 500

    if not result["ok"]:
        # The debit was refused by the balance itself. Answer with the number
        # that refused it rather than a clamped zero the page would have to
        # interpret (only a *move* can get here; ``set`` always reports ok).
        field = ECONOMY_ACTIONS[action][0]
        asked = _int_or_none(data.get("amount")) or 0
        return jsonify({
            "success": False,
            "error": f"That {field} holds {result[field]:,} 🪙 — there is not {asked:,} to remove.",
            "wallet": result["wallet"],
            "bank": result["bank"],
        }), 409

    # The one line that makes an out-of-band balance change accountable after
    # the fact: this ring is what `/owner/ops` shows, and it is the same redaction
    # pass as everything else there.
    print(
        f"[ECONOMY] {actor} ran {action} on {user_id} in {row['name']} "
        f"({guild_id}) — now wallet={result['wallet']:,} bank={result['bank']:,}",
        flush=True,
    )
    return jsonify({
        "success": True,
        "guild_id": _console_id(guild_id),
        "user_id": _console_id(user_id),
        "action": action,
        "wallet": result["wallet"],
        "bank": result["bank"],
    })


@app.route("/api/console/commands")
def api_console_commands():
    """Every command the bot offers, and the server it is missing from.

    Two claims, side by side, because the difference between them is the only
    honest answer to "is a command missing":

    * the **registry** (``features``) is what this deployment *does* — one row
      per root command with the system that gates it;
    * the **live tree** is what Discord was actually *told* — read from the
      running bot, the same list the publisher writes.

    A name the registry declares and the tree does not publish is a command that
    exists in this build and cannot be typed in it. A name in the tree that the
    registry cannot gate is one no switch can ever take away. Both are shown, in
    their own cards, rather than smoothed into a total.

    Per server, availability is ``command_scope``'s own rule — the same function
    the publisher uses — so "off in DaCat" here means what it means in Discord's
    picker rather than a second opinion about it.
    """
    denied = _require_operator_api()
    if denied:
        return denied

    fleet = _fleet_rows()
    states_by_server: dict[int, dict[str, bool]] = {}
    unreadable: list[str] = []
    for row in fleet:
        try:
            states_by_server[row["id"]] = modules.get_states(row["id"])
        except Exception as exc:
            print(f"[CONSOLE] could not read module state for {row['id']}: {exc}", flush=True)
            unreadable.append(_console_id(row["id"]))

    declared = set(features.ROOT_COMMANDS)
    published = _command_tree_names()
    published_set = set(published)
    # The union, so a command nobody declared still has a row and a command
    # nobody published still has one. Ordering is the system's, then the name,
    # because that is how the registry reads.
    names = sorted(declared | published_set)

    rows: list[dict] = []
    for name in names:
        gate = command_scope.gate_of(name)
        system_id = gate[0] if gate else None
        feature = features.get(system_id) if system_id else None
        off_in = [
            server["name"]
            for server in fleet
            if states_by_server.get(server["id"]) is not None
            and not command_scope.is_available(name, states_by_server[server["id"]])
        ]
        rows.append({
            "name": name,
            "system": system_id or "",
            "system_name": feature.name if feature else "Always on",
            "icon": feature.icon if feature else "◌",
            # A name nobody declared is not "core" — it is ungated, and saying
            # which of the two it is is the whole point of the column.
            "declared": name in declared,
            "published": name in published_set if published else None,
            "off_in": off_in,
        })
    rows.sort(key=lambda row: (row["system"] or "~", row["name"]))

    return jsonify({
        "success": True,
        "commands": rows,
        "servers": [
            {
                "id": _console_id(row["id"]),
                "name": row["name"],
                "icon": row["icon"],
                "members": row["members"],
            }
            for row in fleet
        ],
        "always_on": list(features.always_on_names()),
        # ``tree_read`` is reported rather than assumed: a sleeping gateway
        # answers with an empty tree, and a page that read that as "every
        # command is missing" would be a false alarm about the whole bot.
        "tree_read": bool(published),
        "missing": sorted(declared - published_set) if published else [],
        "unregistered": sorted(published_set - declared),
        "unreadable": unreadable,
        "totals": {
            "commands": features.command_count(),
            "declared": len(declared),
            "published": len(published),
        },
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


def _god_audit(action: str, *, ok: bool, detail: str = "", scope: str = "god") -> None:
    """One line in the website's audit trail, written through the bot's loop.

    ``record_web_action`` is a coroutine, and this is called from a Flask thread,
    so it goes through the same reader the rest of the console uses. A record
    that cannot be written is reported and swallowed: the act itself has already
    happened, and failing the request afterwards would tell the operator it did
    not.
    """
    actor_id, actor_name = _god_actor()
    try:
        _loop_reading(
            db.record_web_action(
                scope=scope,
                actor_id=actor_id,
                actor_name=actor_name,
                guild_id=0,
                action=action,
                target="every server" if scope == "god" else "",
                ok=ok,
                detail=detail,
                at=time.time(),
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[AUDIT] could not record god action '{action}': {exc}", flush=True)


def _god_recent(limit: int = 8) -> list[dict]:
    """The last few god-level actions, read back from the audit trail.

    Read from the trail rather than kept in a ring in memory, because the point
    of the trail is that it outlives the process — and a god page whose history
    emptied on every deploy would be a page that cannot answer "did somebody
    pause the bot last night?".
    """
    rows, read = _loop_reading(db.recent_web_actions(guild_id=0, limit=40), [])
    if not read:
        return []
    return [row for row in rows if str(row.get("scope") or "") == "god"][:limit]


@app.route("/api/console/god")
def api_console_god():
    """The god level: whether it is open, and what its actions would act on.

    Read-only, and it needs no unlock: the page has to be able to draw itself
    while it is locked, or the operator cannot see what "unlock" would open. What
    the lock protects is the *actions* endpoint below.
    """
    denied = _require_operator_api()
    if denied:
        return denied

    pause_state = pause.state(refresh=True)
    fleet = _fleet_rows()
    return jsonify({
        "success": True,
        "locked": not _god_unlocked(),
        "remaining_seconds": _god_remaining(),
        "unlock_seconds": GOD_UNLOCK_SECONDS,
        "paused": pause_state,
        "paused_sentence": pause.describe(pause_state),
        "fleet": fleet,
        "totals": _console_totals(fleet),
        "actions": [
            {
                "id": action_id,
                "label": label,
                "blurb": blurb,
                "reason_required": bool(needs_reason),
                # Whether it would change anything right now, so a button that
                # would do nothing says so instead of being pressed to find out.
                "is_set": (
                    (action_id == "pause" and pause_state["paused"])
                    or (action_id == "resume" and not pause_state["paused"])
                ),
            }
            for action_id, label, blurb, needs_reason in GOD_ACTIONS
        ],
        "recent": _god_recent(),
    })


@app.route("/api/console/god/unlock", methods=["POST"])
def api_console_god_unlock():
    """Re-prove the owner passkey, and open the tier for a quarter of an hour.

    The same credential as the front door, and the same limiter: an extra place
    to type a passkey is an extra place to guess one, so this shares
    ``_PASSKEY``'s window rather than getting a bucket of its own. What it does
    *not* share is the session: unlocking god level leaves the ordinary operator
    session exactly as it was, so the tier can expire without signing anybody out.
    """
    denied = _require_operator_api()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    outcome = _god_open(str(data.get("passkey") or ""))
    if not outcome.get("ok"):
        payload = {"success": False, "error": outcome["error"]}
        # `locked: True` is not decoration: the browser app reads a 403 as "your
        # session is dead" *unless* it carries that flag, so a mistyped passkey
        # answered with "Your sign-in is no longer valid" — a sentence that is
        # false and hides the server's own message.
        if outcome.get("locked"):
            payload["locked"] = True
        return jsonify(payload), outcome["status"]
    return jsonify({
        "success": True,
        "locked": False,
        "remaining_seconds": outcome["remaining_seconds"],
        "said": outcome["said"],
    })


@app.route("/api/console/god/lock", methods=["POST"])
def api_console_god_lock():
    """Close the tier now rather than waiting for the clock.

    An operator who is finished should not have to remember that a page is still
    armed; this is the control that makes the unlock a thing you *hold* rather
    than a thing you did.
    """
    denied = _require_operator_api()
    if denied:
        return denied
    _god_close()
    return jsonify({"success": True, "locked": True, "remaining_seconds": 0})


@app.route("/api/console/god/act", methods=["POST"])
def api_console_god_act():
    """One fleet-wide action, with the reason it was asked for.

    Four actions, and each of them is written down *before* the answer is given:
    the audit line is the record, and a record written after a partial failure
    would be a record of the parts that worked.
    """
    denied = _require_god_api()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    outcome = _god_perform(
        str(data.get("action") or ""), str(data.get("reason") or "").strip()
    )
    if not outcome.get("ok"):
        return jsonify({"success": False, "error": outcome["error"]}), outcome["status"]
    if outcome.get("paused") is not None:
        return jsonify({
            "success": True,
            "paused": outcome["paused"],
            "said": outcome["said"],
        })
    return jsonify({
        "success": not outcome["failed"],
        "changed": outcome["changed"],
        "servers": outcome["servers"],
        "failed": outcome["failed"],
        "error": outcome["error"],
        "said": outcome["said"],
    })


@app.route("/api/console/access")
def api_console_access():
    """Who is signed in, and what the three doors are doing.

    Two readings that belong on one page because they answer one question from
    two sides. The **ledger** says which sessions exist, where each came from and
    which one is this browser's — and it is the thing that can end one. The
    **windows** say whether somebody is being refused right now, which is the
    answer to "is that lockout me mistyping, or a stranger?" that a 429 sentence
    cannot give.

    Nothing here is a secret: a masked session id, an address, a label, and
    counts. The sid is masked because it is a bearer credential — the page's job
    is to name a row, and a console that printed the whole value would make its
    own screen a way in rather than a way out.
    """
    denied = _require_operator_api()
    if denied:
        return denied

    ledger = sessions.reading(current_sid=session.get(sessions.COOKIE_KEY))
    return jsonify({
        "success": True,
        "sessions": ledger,
        # Named windows rather than a list: the console draws one row per name,
        # and an anonymous array would make the page's labels a second copy of
        # these names that could drift from them.
        "limits": {
            "sign-in": _SIGN_IN.snapshot(),
            "passkey": _PASSKEY.snapshot(),
            "send": _SEND.snapshot(),
        },
        "doors": {
            "passkey_enabled": bool(ADMIN_PASSKEY),
            # Both of these are the difference between a limit that counts people
            # and one that counts a proxy, so they are stated rather than left to
            # the reader to infer from a behaviour they cannot see.
            "discord_elevation": OWNER_DISCORD_ELEVATION,
            "visitor_address_signed": bool(OWNER_PROXY_SECRET),
            "ledger_writable": sessions.writable(),
        },
        "process": {
            "uptime_seconds": max(0, int(time.time() - PROCESS_STARTED_AT)),
            "started_at": PROCESS_STARTED_AT,
            "now": time.time(),
        },
        # The same shape the other console payloads use for a reading that can
        # fail: a fact and the reason it is missing, not a zero standing in for
        # an answer nobody took.
        "read": {
            "sessions": {
                "ok": bool(ledger["available"]),
                "because": ledger["because"],
            },
        },
    })


@app.route("/api/console/access/revoke", methods=["POST"])
@_form_door
def api_console_access_revoke():
    """End one session, or every session except the one asking.

    "Every session" deliberately spares the caller's own: an operator who clicks
    it and is signed out by their own click has lost the page that tells them so,
    and the useful outcome — every other browser out — is the same either way.
    Signing this one out is ``/logout``, which is a word they already know.

    A revocation is answered with what actually happened rather than with an
    acknowledgement: a session that had already expired is a different fact from
    one that was live and now is not, and only the second is a change.
    """
    denied = _require_operator_api()
    if denied:
        return denied

    data = _json_or_form()
    mine = session.get(sessions.COOKIE_KEY)

    if data.get("all"):
        closed = sessions.revoke_all(keep=mine)
        print(
            f"[DASHBOARD] Operator ended {closed} session(s); this one stays open.",
            flush=True,
        )
        return jsonify({"success": True, "revoked": closed, "scope": "all"})

    row_id = _int_or_none(data.get("id"))
    if row_id is None:
        return jsonify({"success": False, "error": "Which session?"}), 400

    ledger = sessions.reading(current_sid=mine)
    if not ledger["available"]:
        return jsonify({
            "success": False,
            "error": "The database could not be read, so nothing was ended.",
        }), 503
    row = next((entry for entry in ledger["rows"] if entry["id"] == row_id), None)
    if row is None:
        return jsonify({
            "success": False,
            "error": "That session is not in the list any more.",
        }), 404
    if row["current"]:
        return jsonify({
            "success": False,
            "error": (
                "That is this browser's own session. Use Sign out if you mean "
                "to close it."
            ),
        }), 400
    if row["state"] != "live":
        return jsonify({
            "success": True,
            "revoked": False,
            "error": "",
            "note": f"That session had already {row['state']}.",
        })

    ended = sessions.revoke_row(row_id)
    if ended:
        print(
            f"[DASHBOARD] Operator ended a session ({row['label'] or row['scope']}, "
            f"{row['agent']}) from the Access page.",
            flush=True,
        )
    return jsonify({
        "success": True,
        "revoked": bool(ended),
        "note": "Ended." if ended else "That session had already ended.",
    })


@app.route("/api/console/changes")
def api_console_changes():
    """The project's own changelog, newest first — the same reader the hub uses."""
    denied = _require_operator_api()
    if denied:
        return denied
    entries, total = hub.read_changelog(HUB_CHANGELOG, limit=hub.CHANGES_SHOWN)
    return jsonify({"success": True, "entries": entries, "total": total})


@app.route("/api/console/questions")
def api_console_questions():
    """The anonymous question box, on the one screen allowed to show a name.

    This page puts a member's name beside something the member was promised would
    never carry it, and that is the feature rather than a leak: what the box
    promises is that **the channel** sees no name and that **one person** does.
    The gate is ``_require_operator_api`` like every other console route, and the
    public half is built by ``utils/qna.relay_payload`` from a closed field list,
    which cannot carry a name no matter what this route returns — asserted in
    ``tests/test_qna.py`` rather than intended here.

    Read synchronously and off a connection of its own (``owner_inbox_sync``):
    this has to answer while the bot is restarting, which is exactly when nobody's
    event loop is available and when an operator opens the page.
    """
    denied = _require_operator_api()
    if denied:
        return denied

    try:
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20

    reading = qna.owner_inbox_sync(limit=limit)
    return jsonify({
        "success": True,
        "questions": reading["rows"],
        "totals": {"open": reading["open"], "answered": reading["answered"]},
        # Whether there is anywhere to relay *to*, which is the difference between
        # a quiet box and a broken one — `/qna ask` refuses by naming this
        # setting, and the page says the same sentence rather than leaving an
        # operator to wonder why nothing is arriving.
        "relay": {"configured": reading["configured"], "setting": reading["setting"]},
        "read": {
            "questions": {"ok": reading["available"], "because": reading["because"]},
        },
        "max_answer": qna.MAX_ANSWER,
    })


@app.route("/api/console/questions/reply", methods=["POST"])
@_form_door
def api_console_questions_reply():
    """Answer one question from the console — the same DM ``/qna reply`` sends.

    The delivery is ``cogs.qna.deliver_answer``, called on the bot's own loop
    rather than reimplemented: the order inside it (claim the question, then DM,
    then put it back if Discord refuses the DM) is what stops an asker being DMed
    twice and what stops an answer being lost, so a second copy here would be a
    second place for that order to be wrong. All this route adds is the gate, the
    parsing, and the console's word for where a question goes back to.

    An outcome that changed nothing — an empty box, a question already answered,
    a refused DM — is answered ``success: true`` with the sentence that says so,
    the way the Access page reports a revocation that had already happened. A
    *failure to ask the question* (the bot is not in this process, the loop did
    not answer) is a refusal instead, because nothing was attempted.
    """
    denied = _require_operator_api()
    if denied:
        return denied

    data = _json_or_form()
    question_id = _int_or_none(data.get("id"))
    if question_id is None:
        return jsonify({"success": False, "error": "Which question?"}), 400
    answer = str(data.get("answer") or "")

    if BOT_INSTANCE is None:
        return jsonify({
            "success": False,
            "error": (
                "This process has no bot in it, so there is nobody to send the "
                "DM as. Nothing was lost — the question is still waiting."
            ),
        }), 503

    from cogs import qna as qna_cog  # noqa: PLC0415 — after the gate, by design

    try:
        result = _run_on_bot_loop(
            qna_cog.deliver_answer(BOT_INSTANCE, question_id, answer), timeout=25
        )
    except Exception as exc:  # noqa: BLE001 — a refused request is an answer
        # A timeout means *unknown*, never "did not happen" (see
        # ``_run_on_bot_loop``), so the sentence does not claim nothing was sent.
        print(f"[QNA] The console could not deliver an answer: {exc}", flush=True)
        return jsonify({
            "success": False,
            "error": (
                "Discord did not answer in time, so this is unknown rather than "
                "failed — reopen the page before sending it again."
            ),
        }), 500

    # ``bold=False``: the sentence is authored for a Discord card, and this is the
    # one surface where ``**DaCat**`` would reach a person as four asterisks.
    said = qna_cog.answer_sentence(result, inbox="this list", bold=False)
    if result.get("ok"):
        print(
            f"[QNA] Answered {result.get('handle')} from the console — DMed, "
            "nothing posted.",
            flush=True,
        )
    return jsonify({
        "success": True,
        "sent": bool(result.get("ok")),
        "said": said,
        "handle": result.get("handle") or "",
        "asker": result.get("asker") or "",
    })


@app.route("/api/reaction-roles", methods=["POST"])
@_form_door
def api_reaction_roles():
    """Link an emoji on a message to a role, or stop a mapping.

    The same rows ``/reactionrole`` writes, so the page and the command cannot
    disagree about what a server has. Adding is the only write that touches
    Discord — the bot reacts to the message, for the same reason the command
    does: a mapping nobody can see is a mapping nobody uses — and a message it
    could not reach is still **saved**, with the sentence saying so. Refusing the
    save would pretend the mapping is not real while the listener, which needs no
    channel, would work anyway.
    """
    denied = _require_auth_api()
    if denied:
        return denied

    data = _json_or_form()
    refused = _foreign_guild_refusal(data.get("guild_id"))
    if refused:
        return refused
    guild_id = _resolve_guild_id(data.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server to save this for."}), 400

    action = str(data.get("action") or "add").strip().lower()
    if action == "remove":
        return _remove_reaction_role(guild_id, data)
    if action != "add":
        return jsonify({"success": False, "error": "Unknown action"}), 400
    return _add_reaction_role(guild_id, data)


def _add_reaction_role(guild_id, data):
    """One new mapping, from a pasted message link or a bare id."""
    raw = str(data.get("message") or "").strip()
    emoji = str(data.get("emoji") or "").strip()
    role_id = _int_or_none(data.get("role_id"))
    if not raw:
        return jsonify({
            "success": False,
            "error": "Paste the message's link (or its id) so I know what to watch.",
        }), 400
    if not emoji:
        return jsonify({"success": False, "error": "Pick the emoji members react with."}), 400
    if len(emoji) > 100:
        return jsonify({"success": False, "error": "That emoji is too long to be one."}), 400
    if not role_id:
        return jsonify({"success": False, "error": "Pick the role that reaction gives."}), 400

    found = _REACTION_ROLE_LINK_RE.search(raw)
    if found:
        link_guild, channel_id, message_id = (int(part) for part in found.groups())
        if link_guild != int(guild_id):
            return jsonify({
                "success": False,
                "error": "That link is for another server — paste a message link from this one.",
            }), 400
    else:
        digits = raw.strip("<>")
        if not digits.isdigit():
            return jsonify({
                "success": False,
                "error": (
                    "That is not a message link or id — copy the message's link "
                    "(right-click → Copy Message Link)."
                ),
            }), 400
        channel_id, message_id = 0, int(digits)

    roles = _role_choices(guild_id)
    match = next((row for row in roles if row["id"] == str(role_id)), None)
    if roles and match is None:
        return jsonify({
            "success": False,
            "error": "That role is not in this server any more — reload the page for the list Discord just gave.",
        }), 400
    if match is not None and match["above"]:
        return jsonify({
            "success": False,
            "error": (
                f"**@{match['name']}** sits at or above the bot's own role, so Discord "
                "refuses the assignment — the bot would report success while nothing "
                "changed on the member. Pick a lower role."
            ),
        }), 400

    # The escalation guard `/addrole` and `/reactionrole add` both use, asked on
    # the web door too: a reaction that hands out a role is a grant nobody has to
    # approve at the moment it happens, so the person wiring it up must be able
    # to give that role themselves.
    guild = _live_guild(guild_id)
    role_obj = guild.get_role(int(role_id)) if guild is not None else None
    if role_obj is not None:
        actor = guild.get_member(int(_principal().user_id or 0))
        blocked = permissions.role_grant_problem(role_obj, actor, guild)
        if blocked:
            return jsonify({"success": False, "error": blocked}), 403

    try:
        db.write_reaction_role_sync(guild_id, message_id, channel_id, emoji, role_id)
    except (sqlite3.Error, OSError) as exc:
        print(f"[DASHBOARD] ⚠️ could not save a reaction role for {guild_id}: {exc}", flush=True)
        return jsonify({
            "success": False,
            "error": "The database refused that write, so nothing changed.",
        }), 500

    session["guild_id"] = guild_id
    name = match["name"] if match else str(role_id)
    reacted = {"success": False, "error": "the message id alone does not say which channel it is in"}
    if channel_id:
        reacted, _status = _on_bot_loop(_react_to_message(channel_id, message_id, emoji))
    if reacted.get("success"):
        said = (
            f"Reacting with {emoji} on that message now gives **@{name}**, and "
            "removing the reaction takes it back."
        )
    else:
        said = (
            f"Saved: {emoji} on message `{message_id}` now gives **@{name}**, and "
            "removing the reaction takes it back. I could not add the reaction "
            f"myself ({reacted.get('error') or 'Discord refused'}) — add it by hand "
            "and it will work, or delete the reaction to retire the mapping."
        )
    return jsonify({"success": True, "said": said})


async def _react_to_message(channel_id, message_id, emoji):
    """Put the reaction on a newly mapped message, on the bot's own loop.

    A dict rather than a raise: failing to draw the emoji is not failing to make
    the mapping, and the sentence the page shows distinguishes the two.
    """
    if BOT_INSTANCE is None:
        return {"success": False, "error": "the bot is not connected"}
    channel = BOT_INSTANCE.get_channel(int(channel_id))
    if channel is None:
        return {"success": False, "error": "that channel is not visible to the bot"}
    try:
        message = await channel.fetch_message(int(message_id))
        await message.add_reaction(emoji)
    except Exception as exc:  # noqa: BLE001 — a refusal, never a 500
        return {"success": False, "error": type(exc).__name__}
    return {"success": True}


def _remove_reaction_role(guild_id, data):
    """Forget one mapping — the reactions already on the message stay."""
    message_id = _int_or_none(data.get("message_id"))
    emoji = str(data.get("emoji") or "").strip()
    if not message_id or not emoji:
        return jsonify({
            "success": False,
            "error": "That row is missing the message or the emoji — reload the page.",
        }), 400
    try:
        removed = db.delete_reaction_role_sync(message_id, emoji)
    except (sqlite3.Error, OSError) as exc:
        print(f"[DASHBOARD] ⚠️ could not remove a reaction role for {guild_id}: {exc}", flush=True)
        return jsonify({
            "success": False,
            "error": "The database refused that write, so nothing changed.",
        }), 500
    if not removed:
        return jsonify({
            "success": True,
            "said": (
                "That mapping was already gone — the list above is what Discord and "
                "the database agree on now."
            ),
        })
    return jsonify({
        "success": True,
        "said": (
            f"{emoji} no longer changes anybody's roles on that message. The "
            "reactions already on it stay, and the roles members already have are "
            "untouched."
        ),
    })


@app.route("/api/module-settings", methods=["POST"])
@_form_door
def api_module_settings():
    """One system's own settings — thresholds, filters, ceilings — from a form.

    The controls the registry declares for a system, written through the same
    ``guild_settings`` table every other per-server choice lives in. The shape is
    ``/api/level-settings``'s for the same reason: only the fields present in the
    request move, so a script may change one threshold without restating the
    other six — while the page's own form always sends every field, which is what
    makes an unticked box mean *off* rather than *unchanged*.

    Three refusals matter and each is in its own words:

    * an unknown system, or one that declares no settings;
    * a value outside the declaration's own bounds — never silently clamped,
      because a page that saves 500 and prints 200 is a page arguing with itself;
    * a system the operator has force-closed for every server, refused with the
      same operator sentence the switch above it uses. There is nothing to tune
      in a system that may not run anywhere.
    """
    denied = _require_auth_api()
    if denied:
        return denied

    data = _json_or_form()
    refused = _foreign_guild_refusal(data.get("guild_id"))
    if refused:
        return refused
    guild_id = _resolve_guild_id(data.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server to save these for."}), 400

    feature_id = str(data.get("module") or "").strip()
    feature = features.get(feature_id)
    if feature is None or not feature.settings:
        return jsonify({
            "success": False,
            "error": "That system does not have settings to change.",
        }), 400

    forced = modules.override_for(feature_id)
    if forced is False:
        return jsonify({
            "success": False,
            "error": _operator_lock_sentence({feature_id: False}),
            "operator_locked": True,
        }), 403

    saved = []
    for spec in feature.settings:
        if spec.id not in data:
            continue
        value, error = _posted_setting_value(spec, data.get(spec.id))
        if error:
            return jsonify({"success": False, "error": error}), 400
        try:
            db.write_setting_sync(guild_id, features.setting_key(feature_id, spec.id), value)
        except (sqlite3.Error, OSError) as exc:
            print(f"[DASHBOARD] ⚠️ could not write {spec.id} for {guild_id}: {exc}", flush=True)
            return jsonify({
                "success": False,
                "error": (
                    "The database refused that write, so nothing changed — this "
                    "system is still running on the rules it had."
                ),
            }), 500
        saved.append(f"{spec.label} — {_setting_display(spec, value)}")

    if not saved:
        return jsonify({"success": False, "error": "Nothing to change."}), 400

    session["guild_id"] = guild_id
    _invalidate_module_settings(guild_id)
    _schedule_cloud_backup()
    return jsonify({
        "success": True,
        "said": f"Saved for **{feature.name}**: " + "; ".join(saved) + ".",
        "module": feature_id,
    })


def _posted_setting_value(spec, raw):
    """One posted value, validated against its declaration.

    Returns ``(value, "")`` when it is acceptable and ``(None, sentence)`` when it
    is not. Deliberately stricter than :func:`features.setting_value`: that one
    answers "what is in force" for a *stored* value and falls back to the default,
    which is the right answer for a reader and the wrong one for a writer — a
    save that quietly became the default is a control that does not do what it
    says.
    """
    if spec.kind == "bool":
        text = str(raw).strip().lower()
        if text in features.BOOL_TRUE:
            return True, ""
        if text in features.BOOL_FALSE:
            return False, ""
        return None, f"{spec.label} has to be on or off."
    if spec.kind == "int":
        value = _int_or_none(raw)
        if value is None:
            return None, f"{spec.label} has to be a whole number."
        if spec.minimum is not None and value < spec.minimum:
            return None, f"{spec.label} cannot be below {spec.minimum}."
        if spec.maximum is not None and value > spec.maximum:
            return None, f"{spec.label} cannot be above {spec.maximum}."
        return value, ""
    if spec.kind == "choice":
        text = str(raw).strip()
        for value, _label in spec.choices:
            if text == value:
                return value, ""
        offered = ", ".join(value for value, _label in spec.choices)
        return None, f"{spec.label} has to be one of: {offered}."
    text = str(raw or "").strip()
    if len(text) > 500:
        return None, f"{spec.label} is too long — 500 characters at most."
    return text, ""


def _setting_display(spec, value) -> str:
    """One saved value as the notice says it back, in the control's own words."""
    if spec.kind == "bool":
        return "on" if value else "off"
    text = str(value if value is not None else "")
    return f"`{text}`" if text else "empty"


def _invalidate_module_settings(guild_id) -> None:
    """Tell the live bot its cached rules changed.

    ``cogs.automod`` caches its settings per guild, and this endpoint writes them
    from the Flask thread. Without this, a threshold saved on the page would keep
    judging messages by the old rule until a restart — the drift the registry
    exists to prevent, one cache deep. A missing cog is not a failure: the
    dashboard and the bot are separate deployments more often than not.
    """
    lookup = getattr(BOT_INSTANCE, "get_cog", None)
    if not callable(lookup):
        return
    try:
        live = lookup("AutoMod")
    except Exception:  # noqa: BLE001 — a stand-in bot in a test render
        return
    cache = getattr(live, "settings_cache", None)
    if isinstance(cache, dict):
        cache.pop(int(guild_id), None)


@app.route("/api/toggle", methods=["POST"])
@_form_door
def api_toggle():
    denied = _require_auth_api()
    if denied:
        return denied

    data = _json_or_form()
    refused = _foreign_guild_refusal(data.get("guild_id"))
    if refused:
        return refused
    guild_id = _resolve_guild_id(data.get("guild_id"))
    module_id = data.get("module")
    enabled = bool(data.get("enabled"))

    session["guild_id"] = guild_id
    payload, status = _flip_module(guild_id, module_id, enabled)
    return jsonify(payload), status


def _operator_lock_sentence(locked: dict) -> str:
    """The sentence for "the operator has the last word about these systems".

    Named here because **two doors say it**: one switch at a time, and the two
    bulk buttons on the Systems page. The bulk door used to answer differently —
    *"Could not save: leveling, economy, market, …"* with a **500** — which
    describes the operator's decision as a server error and sends an
    administrator looking for one. ``locked`` is ``{module_id: forced_on}`` for
    the systems the operator holds; one system keeps the sentence the dashboard
    has always printed, and several are counted and named.
    """
    names = sorted(locked)
    if len(names) == 1:
        forced = bool(locked[names[0]])
        return (
            f"{names[0]} is switched {'on' if forced else 'off'} for every server "
            "by the operator, so this server cannot change it."
        )
    return (
        f"{len(names)} systems are switched for every server by the operator "
        f"({', '.join(names)}), so this server cannot change them."
    )


def _flip_module(guild_id, module_id, enabled):
    """One system switched on or off — the whole write, in one place.

    **Two doors ask for this now**, and that is why it is a function: the JSON
    route a console or a script calls, and the dashboard's own switch, which is a
    form that posts and redirects. A switch that wrote through a second copy of
    these rules is the defect this project keeps meeting in its own history —
    one of the two would eventually refuse a plan, or skip the republish, or
    report a save that failed. The endpoint's answer and the page's notice are
    both rendered from the mapping returned here, so they cannot disagree about
    whether the write landed.

    Returns ``(payload, status)`` — exactly what the endpoint answered before
    the second door existed.
    """
    if guild_id is None:
        return {"success": False, "error": "No guild available"}, 400
    if module_id not in ALLOWED_MODULE_IDS:
        return {"success": False, "error": "Unknown module"}, 400

    # A system this server's plan does not include is refused in its own words.
    # ``modules.set_enabled`` refuses it too (that is what keeps every reader
    # agreeing), but it can only answer "could not save", and a switch that
    # springs back with no reason is the kind of defect this dashboard has
    # already been reported for twice.
    if enabled and not plans.feature_allowed(guild_id, module_id):
        return {
            "success": False,
            "error": (
                f"{module_id} is part of a paid plan this server does not have. "
                "Activate a licence with /bot plan to switch it on."
            ),
            "plan_required": True,
        }, 403

    # The operator has the last word, and it is refused in its own words rather
    # than as "could not save". ``modules.set_enabled`` refuses it too — that is
    # what keeps every reader agreeing — but this is the sentence the person who
    # pressed the switch reads, and "the owner took this away from you" is a very
    # different message from "something went wrong".
    forced = modules.override_for(module_id)
    if forced is not None and forced != enabled:
        return {
            "success": False,
            "error": _operator_lock_sentence({module_id: forced}),
            "operator_locked": True,
        }, 403

    # Persist BEFORE updating memory. If the write fails we must not report
    # success, otherwise the toggle silently reverts on the next restart.
    if not modules.set_enabled(guild_id, module_id, enabled):
        return {"success": False, "error": "Could not save that change. Please try again."}, 500

    # Read *before* the republish is asked for, and into a local rather than
    # inside the response below: this is the stamp the page has to beat, so it
    # must be the publish from before this switch. The queueing call returns at
    # once today — the publish happens three seconds later on the bot's loop —
    # but a page whose stamp came from the publish it just asked for would wait
    # out its whole timeout and then report that Discord had never been reread.
    publish_before = _last_publish_at(guild_id)
    queued = _resync_commands(guild_id)
    _schedule_cloud_backup()

    return {
        "success": True,
        # Text, like every other id this app hands the browser. A snowflake that
        # crosses as a JSON number comes back as a different number (see
        # ``_console_id``), and this route is called by the console, whose schema
        # says string — so a raw int here fails the page's own payload check and
        # the whole click lands in its catch: the switch springs back, the toast
        # reads "the console and the bot are out of step", and the publish stamp
        # below is never read. One type, or the switch looks broken while the
        # server did exactly what it was asked.
        "guild_id": _console_id(guild_id),
        "module": module_id,
        "enabled": enabled,
        "sync_latency_ms": 0,
        # The publish this switch supersedes. The page uses it to tell when
        # Discord has been read again: a later stamp on the scope panel means the
        # republish this click asked for has finished. Read here rather than in
        # the page so the comparison is between two readings of one clock.
        "publish_at": publish_before,
        # Whether the republish was actually asked for. A switch flips in the
        # database either way, but the *picker* only changes if the bot was up to
        # hear the request — and a click during a restart would otherwise look
        # like a command that refused to appear with no cause anywhere.
        "republish": queued,
    }, 200


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


#: What each rate is called in a sentence, for a refusal that names the box the
#: operator actually typed in rather than the column it is stored in.
_LEVEL_RATE_LABELS = {
    "text_min_coins": "The lowest coins per message",
    "text_max_coins": "The highest coins per message",
    "text_xp": "XP per message",
    "vc_coins_per_min": "Coins per minute in voice",
    "vc_xp_per_min": "XP per minute in voice",
}


def _level_settings_sentence(settings: dict) -> str:
    """What was stored, in the page's own words — the figures, not a claim.

    The rate clamps are real: a maximum below the minimum is raised, and the
    voice pair is bounded. So the sentence quotes the row that was written rather
    than the numbers that were typed, which is the only way "saved" can be
    checked against the page underneath it.
    """
    band = f"{settings['text_min_coins']}-{settings['text_max_coins']} 🪙"
    return (
        f"Saved. A message here earns {band} and {settings['text_xp']} XP; "
        f"a minute in voice earns {settings['vc_coins_per_min']} 🪙 and "
        f"{settings['vc_xp_per_min']} XP. Rewards are "
        f"{'on' if settings['enabled'] else 'off'} for this server."
    )


@app.route("/api/server-settings", methods=["POST"])
@_form_door
def api_server_settings():
    """Point this server's moderation record at a channel — the row ``/modlog`` writes.

    The same setting ``/modlog`` writes, through the constant the bot's own
    reader resolves it with (``utils.modlog.SETTING``): one row and two doors, so
    the page's picker and the slash command cannot disagree about where a case
    goes. Before this the checklist told administrators to set it "on the Server
    page" and the Server page had no such control, which is worse than saying
    nothing — the advice sent them looking and the search ended in the lockdown
    button.

    Saving an empty value **deletes** the row rather than storing a word, which
    is what "off" means everywhere in ``guild_settings`` (see
    ``db.write_setting_sync``).

    A channel the bot cannot post cards in is still saved, and the sentence says
    so: ``/modlog``'s own answer to that case, kept here because the permissions
    may be fixed a minute later and a setting refused twice is a setting somebody
    stops trusting.
    """
    denied = _require_auth_api()
    if denied:
        return denied

    data = _json_or_form()
    refused = _foreign_guild_refusal(data.get("guild_id"))
    if refused:
        return refused
    guild_id = _resolve_guild_id(data.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server to save this for."}), 400

    # Two settings, one door, and the second one is here for the same reason the
    # first is: the overview's checklist sends an administrator to this page, and
    # the page named in a finding has to hold the control that answers it.
    if "default_role_id" in data:
        return _save_default_role(guild_id, data.get("default_role_id"))

    if "log_channel_id" not in data:
        return jsonify({"success": False, "error": "Nothing to change."}), 400

    raw = str(data.get("log_channel_id") or "").strip()
    if not raw:
        try:
            db.write_setting_sync(guild_id, modlog.SETTING, None)
        except (sqlite3.Error, OSError) as exc:
            print(f"[DASHBOARD] ⚠️ could not clear {modlog.SETTING} for {guild_id}: {exc}", flush=True)
            return jsonify({
                "success": False,
                "error": (
                    "The database refused that write, so nothing changed — moderation "
                    "is still logging wherever it was before."
                ),
            }), 500
        session["guild_id"] = guild_id
        return jsonify({
            "success": True,
            "log_channel_id": "",
            "said": (
                "Moderation logging is off in this server: nothing new is written to "
                "a channel. The cases already recorded are untouched, and picking a "
                "channel here — or /modlog #channel in the server — starts it again."
            ),
        })

    channel_id = _int_or_none(raw)
    if channel_id is None:
        return jsonify({
            "success": False,
            "error": "That is not a channel id — pick a channel from the list.",
        }), 400

    try:
        db.write_setting_sync(guild_id, modlog.SETTING, channel_id)
    except (sqlite3.Error, OSError) as exc:
        print(f"[DASHBOARD] ⚠️ could not write {modlog.SETTING} for {guild_id}: {exc}", flush=True)
        return jsonify({
            "success": False,
            "error": (
                "The database refused that write, so nothing changed — moderation is "
                "still logging wherever it was before."
            ),
        }), 500

    session["guild_id"] = guild_id
    return jsonify({
        "success": True,
        "log_channel_id": str(channel_id),
        "said": _log_channel_sentence(guild_id, channel_id),
    })


def _save_default_role(guild_id, raw):
    """Point this server's welcome role — the row ``/defaultrole`` writes.

    Empty clears it, which is the same move ``/defaultrole clear`` makes and the
    same meaning ``guild_settings`` gives every absent row.

    A role that is not in this server is refused rather than stored: the bot
    would go on quietly assigning nothing while the page showed the role
    selected, which is the failure this control exists to make visible. When the
    role list could not be read at all the id is accepted — a write refused
    because Discord was unreachable is a write somebody stops trusting.
    """
    text = str(raw or "").strip()
    if not text:
        try:
            db.write_setting_sync(guild_id, DEFAULT_ROLE_SETTING, None)
        except (sqlite3.Error, OSError) as exc:
            print(
                f"[DASHBOARD] ⚠️ could not clear {DEFAULT_ROLE_SETTING} for {guild_id}: {exc}",
                flush=True,
            )
            return jsonify({
                "success": False,
                "error": (
                    "The database refused that write, so nothing changed — the role "
                    "below is still the one new members get."
                ),
            }), 500
        session["guild_id"] = guild_id
        return jsonify({
            "success": True,
            "default_role_id": "",
            "said": (
                "New members will not be given a role on arrival. `/defaultrole clear` "
                "in the server does the same thing."
            ),
        })

    role_id = _int_or_none(text)
    if role_id is None:
        return jsonify({
            "success": False,
            "error": "That is not a role id — pick a role from the list.",
        }), 400

    choices = _role_choices(guild_id)
    match = next((row for row in choices if row["id"] == str(role_id)), None)
    if choices and match is None:
        return jsonify({
            "success": False,
            "error": (
                "That role is not in this server any more — reload the page for the "
                "list Discord just gave."
            ),
        }), 400

    try:
        db.write_setting_sync(guild_id, DEFAULT_ROLE_SETTING, role_id)
    except (sqlite3.Error, OSError) as exc:
        print(
            f"[DASHBOARD] ⚠️ could not write {DEFAULT_ROLE_SETTING} for {guild_id}: {exc}",
            flush=True,
        )
        return jsonify({
            "success": False,
            "error": (
                "The database refused that write, so nothing changed — new members "
                "still get whatever they got before."
            ),
        }), 500

    name = match["name"] if match else str(role_id)
    note = (
        " The bot's own role sits at or below it, so Discord will refuse the "
        "assignment until the bot's role is dragged above it in Server Settings → Roles."
        if (match and match["above"]) else ""
    )
    session["guild_id"] = guild_id
    return jsonify({
        "success": True,
        "default_role_id": str(role_id),
        "said": f"New members will be given **@{name}** on arrival.{note}",
    })


def _log_channel_sentence(guild_id, channel_id) -> str:
    """What just happened, from what Discord was actually asked.

    Three answers rather than one, because the third is the one that costs a
    server its record: the channel is set and the bot cannot draw a card in it,
    and the only other place that fact appears is a line in a host log.
    """
    guild = _live_guild(guild_id)
    lookup = getattr(guild, "get_channel", None)
    channel = lookup(channel_id) if callable(lookup) else None
    if channel is None:
        # No cause is named here, deliberately: a channel that cannot be looked up
        # is a disconnected bot, a deleted channel **or** a guild object that
        # simply has no lookup — and a sentence that picked one of the three
        # would be a claim nobody read.
        return (
            f"Moderation logs are set to channel {channel_id}. Whether the bot may "
            "write there could not be checked just now — the checklist on the "
            "overview says that as soon as it can."
        )
    name = f"#{getattr(channel, 'name', channel_id)}"
    verdict = modlog.can_post(channel, getattr(guild, "me", None))
    if verdict is False:
        return (
            f"Moderation logs will be sent to {name}, and the bot cannot post there "
            "yet: its role needs Send Messages and Embed Links on that channel, or "
            "every case stays in the database where no moderator reads it."
        )
    if verdict is None:
        return (
            f"Moderation logs will be sent to {name}. Whether the bot may write "
            "there could not be read from Discord just now, so the checklist will "
            "say — it is not an answer that it can."
        )
    return f"Moderation logs will now be sent to {name}."


@app.route("/api/support")
def api_support():
    """The support conversation, as the card draws it — the same reading, twice.

    A read, so it is separate from the write door and needs no CSRF token beyond
    the operator gate: the page renders from ``_support_card`` directly, and this
    is the same answer for a script, for a refresh, and for whatever the console
    grows next. One reader behind both doors, for the reason every other reading
    in this file is built that way.
    """
    denied = _require_operator_api()
    if denied:
        return denied
    return jsonify({"success": True, **_support_card()})


@app.route("/api/support", methods=["POST"])
@_form_door
def api_support_write():
    """Send one message into the support channel, or take its history in.

    Two actions behind one address because they are the same conversation and the
    same door: ``send`` posts as the bot, ``pull`` reads what Discord already has.
    Both are operator-only — the channel is the bot's own, not a server's — and
    both answer with a sentence the form's own redirect carries back to the page.
    """
    denied = _require_operator_api()
    if denied:
        return denied

    data = _json_or_form()
    action = str(data.get("action") or "send").strip()
    if action == "pull":
        payload, status = _support_pull()
    elif action == "send":
        payload, status = _support_send(data)
    else:
        payload, status = {"success": False, "error": "Unknown support action"}, 400
    return jsonify(payload), status


def _support_card(*, limit=None, guild=None, find=None) -> dict:
    """Everything one support card draws, read the same way on all three surfaces.

    The dashboard, the console and the preview harness draw this card, and a card
    built three times is three cards that start to differ. The two Discord lookups
    are passed in rather than reached for here so the module underneath stays
    pure: ``find`` is the client's own lookup, which answers for every server the
    bot is in, and ``guild`` is only the fallback for a caller with one server.
    """
    return support.card(
        limit=limit or support.SHOWN,
        guild=guild,
        find=find or getattr(BOT_INSTANCE, "get_channel", None),
    )


def _support_channel():
    """The live support channel, or ``None`` — used by both writes."""
    return support.resolve(find=getattr(BOT_INSTANCE, "get_channel", None))


async def _support_room(channel, row):
    """The thread one conversation is answered in, opening it if it has none yet.

    Three cases, and the third is the one that makes this a conversation rather
    than a broadcast: a reply already in a thread is answered there; a message
    that started a conversation in Discord has the thread the listener made; and
    a message nobody has threaded yet gets one **here**, on the message that
    started it, because otherwise the operator answering from the console would be
    talking over the top of a conversation instead of into it.

    Returns ``(thread, why_not)``: exactly one of the two is ever set, so a caller
    cannot answer into ``None`` by accident.
    """
    thread_id = support.thread_target([row])
    if thread_id:
        thread = BOT_INSTANCE.get_channel(thread_id) if BOT_INSTANCE is not None else None
        if thread is None:
            return None, (
                "The thread this conversation lives in is gone — archived, deleted, "
                "or in a server the bot has left. Start a new message in the room "
                "and the conversation picks up there."
            )
        return thread, ""

    parent_id = support.as_int((row or {}).get("message_id"))
    lookup = getattr(channel, "get_partial_message", None)
    parent = lookup(parent_id) if parent_id and callable(lookup) else None
    if parent is None:
        return None, (
            "That message is not one this bot can still answer — Discord keeps no "
            "copy of it here. Start a new message in the room instead."
        )
    # ``await``, not ``_on_bot_loop``: this coroutine is *itself* running on the
    # bot's loop (the caller hands it over with ``_on_bot_loop``), and scheduling a
    # second coroutine onto the loop thread and blocking for it is a deadlock.
    try:
        thread = await parent.create_thread(
            name=support.thread_name((row or {}).get("body"), (row or {}).get("author_name")),
            auto_archive_duration=1440,
        )
    except Exception as exc:  # noqa: BLE001 — a refusal, never a 500
        print(f"[SUPPORT] could not open a thread to reply into: {exc}", flush=True)
        return None, (
            f"Discord would not open a thread on that message ({type(exc).__name__}). "
            "The bot needs Create Public Threads in the channel."
        )
    await support.attach_thread((row or {}).get("id"), getattr(thread, "id", 0))
    return thread, ""


def _support_send(data):
    """One message from the web: posted as the bot, then written down.

    The order matters and it is the whole contract — the row is written **after**
    Discord accepted the message, because the record is the bridge's claim that
    the room has this line and a claim written first is a conversation with a
    message in it that nobody in the room can see. A refusal is answered with the
    exception's own name and never as a success, for the reason the composer's
    own send is.

    ``reply_to`` is a row of the conversation. With one, the reply goes **into
    that conversation's thread** — the difference between answering somebody and
    shouting in a room — and without one it is a new message in the channel, which
    is how an operator starts something rather than continues it.
    """
    body = str(data.get("body") or "").strip()
    if not body:
        return {
            "success": False,
            "error": "There is nothing to send — write the message first.",
        }, 400

    principal = _principal()
    who = principal.name or "the operator"
    reply_to = support.as_int(data.get("reply_to"))

    async def work():
        channel = _support_channel()
        if channel is None:
            return {
                "success": False,
                "error": (
                    "The support channel could not be reached, so nothing was posted. "
                    "The address is the console's status card, and the bot has to be "
                    "in that server for this to work."
                ),
            }, 409
        target, room = channel, None
        if reply_to:
            row = await support.get(reply_to)
            if row is None:
                return {
                    "success": False,
                    "error": (
                        "That message is not in the conversation any more — the page "
                        "you replied on may be showing a transcript from before a "
                        "restore. Refresh it and reply again."
                    ),
                }, 409
            room, why_not = await _support_room(channel, row)
            if room is None:
                return {"success": False, "error": why_not}, 409
            target = room
        content = support.compose(body, who)
        try:
            message = await target.send(content)
        except Exception as exc:  # noqa: BLE001 — a refusal, never a 500
            print(f"[SUPPORT] send failed: {exc}", flush=True)
            return {
                "success": False,
                "error": (
                    f"Discord did not take that message ({type(exc).__name__}), so "
                    "nothing was posted. Check that the bot may send in the channel."
                ),
            }, 409
        row = await support.record_outbound(
            body=body,
            author_id=principal.user_id or 0,
            author_name=who,
            message=message,
            thread=room,
        )
        where = support.one_line(getattr(room, "name", "")) if room is not None else ""
        said = (
            f"Sent as the bot, signed with your name — into the thread {where}, where "
            "the person you are answering will read it."
            if where
            else (
                "Sent as the bot, signed with your name. The replies land on this "
                f"card — Discord has it now ({support.one_line(getattr(channel, 'name', ''))})."
            )
        )
        return {
            "success": True,
            "said": said,
            "message": support.reading(row),
            "transcript": support.transcript_sync(support.SHOWN),
        }, 200

    return _on_bot_loop(work())


def _support_pull():
    """Take the channel's recent history into the conversation, on demand.

    The listener only sees what arrives while the bot is running, so a bridge
    switched on after the conversation started would open on an empty card. This
    is the door for that — and it is a *button on the card* rather than a boot
    step, because a boot that read history would be a network call on every start
    for a conversation nobody had asked to see.
    """

    async def work():
        channel = _support_channel()
        if channel is None:
            return {
                "success": False,
                "error": "The support channel could not be reached, so nothing was read.",
            }, 409
        report = await support.pull(channel)
        if not report["ok"]:
            return {
                "success": False,
                "error": (
                    f"{report['detail']} {report['taken']} message(s) were taken in "
                    "before it stopped."
                    if report["taken"]
                    else report["detail"]
                ),
            }, 409
        return {
            "success": True,
            "said": (
                f"Read the last {report['seen']} message(s) in the channel; "
                f"{report['taken']} were not on this card yet."
            ),
            "transcript": support.transcript_sync(support.SHOWN),
        }, 200

    return _on_bot_loop(work())


@app.route("/api/level-settings", methods=["POST"])
@_form_door
def api_level_settings():
    """Change what being in this server earns — the row ``/levelsettings`` writes.

    One row and eight fields, written through :func:`db.set_level_settings_sync`,
    which applies the *cog's* clamps, so a rate set here is a rate the bot will
    actually pay. The page has printed these figures as a reading since it was
    written; this is the control that makes the reading a decision.

    Only the fields present in the request move, so a script can change one rate
    without restating the other seven — while the page's own form always sends
    every field, which is what makes an unticked box mean *off* rather than
    *unchanged*.
    """
    denied = _require_auth_api()
    if denied:
        return denied

    data = _json_or_form()
    refused = _foreign_guild_refusal(data.get("guild_id"))
    if refused:
        return refused
    guild_id = _resolve_guild_id(data.get("guild_id"))
    if guild_id is None:
        return jsonify({"success": False, "error": "No server to save these for."}), 400

    changes = {}
    for field in _LEVEL_RATE_LABELS:
        if field not in data or data.get(field) in (None, ""):
            continue
        value = _int_or_none(data.get(field))
        if value is None:
            return jsonify({
                "success": False,
                "error": f"{_LEVEL_RATE_LABELS[field]} has to be a whole number.",
            }), 400
        changes[field] = value

    # A form sends a box as "1" when it is ticked and sends nothing at all when
    # it is not, so presence is the whole answer — the same rule the systems grid
    # uses, spelled out here because ``bool("0")`` is true and a script may
    # legitimately send "0".
    for field in ("enabled", "dm_notifications"):
        if field in data:
            changes[field] = str(data.get(field)).strip().lower() not in (
                "", "0", "false", "off", "no")

    if "channel_id" in data:
        raw = str(data.get("channel_id") or "").strip()
        channel_id = 0 if not raw else _int_or_none(raw)
        if channel_id is None:
            return jsonify({
                "success": False,
                "error": "That is not a channel id — pick a channel from the list.",
            }), 400
        changes["channel_id"] = channel_id

    if not changes:
        return jsonify({"success": False, "error": "Nothing to change."}), 400

    try:
        stored = db.set_level_settings_sync(guild_id, **changes)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except sqlite3.Error:
        return jsonify({
            "success": False,
            "error": (
                "The database refused that write, so nothing was saved — the rates "
                "below are the ones still in force."
            ),
        }), 500

    session["guild_id"] = guild_id
    return jsonify({
        "success": True,
        "said": _level_settings_sentence(stored),
        "settings": {
            "enabled": stored["enabled"],
            "channel_id": _console_id(stored["channel_id"]) if stored["channel_id"] else "",
            "dm_notifications": stored["dm_notifications"],
            "text_min_coins": stored["text_min_coins"],
            "text_max_coins": stored["text_max_coins"],
            "text_xp": stored["text_xp"],
            "vc_coins_per_min": stored["vc_coins_per_min"],
            "vc_xp_per_min": stored["vc_xp_per_min"],
        },
    })


@app.route("/api/toggle-all", methods=["POST"])
@_form_door
def api_toggle_all():
    denied = _require_auth_api()
    if denied:
        return denied

    data = _json_or_form()
    refused = _foreign_guild_refusal(data.get("guild_id"))
    if refused:
        return refused
    guild_id = _resolve_guild_id(data.get("guild_id"))
    enabled = bool(data.get("enabled"))

    session["guild_id"] = guild_id
    payload, status = _flip_all(guild_id, enabled)
    return jsonify(payload), status


def _flip_all(guild_id, enabled):
    """Every system at once — the bulk switch's write, shared with the page.

    The dashboard offers the same two buttons the console's app had ("all on",
    "all off") as forms, so this is the one implementation behind them. Returns
    ``(payload, status)``.
    """
    if guild_id is None:
        return {"success": False, "error": "No guild available"}, 400

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
        # Two very different things arrive in this one list, and they were
        # reported as one. ``modules.set_enabled`` refuses a system the operator
        # has locked for every server — that refusal is what keeps every reader
        # agreeing — and it answers the same ``False`` as a write that genuinely
        # did not land. So "all off" with a fleet lock on said *"Could not save:
        # leveling, economy, …"* with a 500, when what happened is that the
        # operator had the last word and nothing was wrong anywhere.
        locked: dict[str, bool] = {}
        for mid in failed:
            forced = modules.override_for(mid)
            if forced is not None:
                locked[mid] = bool(forced)
        held = {"success": False, "guild_id": _console_id(guild_id), "enabled": enabled}
        if locked and len(locked) == len(failed):
            return {
                **held,
                "error": _operator_lock_sentence(locked),
                "operator_locked": True,
            }, 403
        if locked:
            return {
                **held,
                "error": (
                    f"{_operator_lock_sentence(locked)} The rest did not save: "
                    f"{', '.join(mid for mid in failed if mid not in locked)}."
                ),
            }, 500
        return {**held, "error": f"Could not save: {', '.join(failed)}"}, 500

    publish_before = _last_publish_at(guild_id)
    queued = _resync_commands(guild_id)
    _schedule_cloud_backup()

    return {
        "success": True,
        # Text, for the reason spelled out on ``/api/toggle`` above: the console
        # reads these two answers through one schema, so one of them answering
        # with a number is the same defect on the bulk switch.
        "guild_id": _console_id(guild_id),
        "enabled": enabled,
        "sync_latency_ms": 0,
        "publish_at": publish_before,
        "republish": queued,
    }, 200


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
    """Whether this session may change the bot's own member in one server.

    The same set of servers as acting on it, asked as its own question because it
    reads like a narrower one: a Discord sign-in may rename the bot in **any**
    server it administers, not only the one it happened to land on — the panel is
    about the bot's own member, and somebody who runs two servers should not have
    to sign in twice. The rule is in ``utils/principal.py``; this is the sentence
    that says why the question exists.
    """
    return _principal().may_act_in(getattr(guild, "id", None))


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
@_form_door
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
            # Shrunk here rather than only on the way into the database, because
            # these bytes are sent to Discord as well — and both copies are
            # counted against this host's egress. A picture past the edge, or
            # past the small-file bound, comes back re-encoded; anything Pillow
            # cannot read (including the magic-byte-only fixtures the tests use)
            # comes back exactly as uploaded.
            edge = identity.AVATAR_MAX_EDGE if field == "avatar" else identity.BANNER_MAX_EDGE
            wants[field] = identity.tame_image(data, max_edge=edge)
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
    return jsonify({"success": True, "guild_id": _console_id(guild_id), **state})


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

#: The composer's window: a request is counted when it is *sent*, not when it is
#: refused, which is the opposite of the sign-in form and the reason ``allow``
#: exists beside ``record`` beside ``remaining``.
_SEND = ratelimit.RateLimiter("send", SEND_WINDOW_SECONDS, SEND_MAX_PER_WINDOW)
_send_attempts = _SEND.buckets


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
    return _SEND.allow(ip)


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


def _send_error(exc: BaseException) -> str:
    """A sentence for a send that raised, with the exception's own words left out.

    An exception's message is a description of the inside of this process — a
    channel id, a Discord status, occasionally a path — and the two endpoints
    that report it answer over HTTP. The whole of it goes to the log, where the
    operator is the reader, and the answer says what happened without becoming a
    mirror of the host's internals.
    """
    print(f"[SEND] Discord did not accept the message: {type(exc).__name__}: {exc}", flush=True)
    return "Discord did not accept the message — the bot's log names the channel and the reason."


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
@_form_door
def api_send_message():
    """Post a dashboard-written message as the bot."""
    denied = _require_auth_api()
    if denied:
        return denied

    data = _json_or_form()
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
        return jsonify({"success": False, "error": _send_error(e)}), 502

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
    # A ``datetime-local`` field, read as **UTC**. The composer's own script sends
    # milliseconds, which is an absolute instant and needs no zone; a plain form
    # cannot know the reader's zone at all, so the field says UTC on its label and
    # this is the one place that understands it. Ambiguity refused rather than
    # guessed — a message queued for the wrong hour is worse than a page that asks.
    raw = data.get("run_at_utc")
    if raw:
        try:
            moment = datetime.datetime.strptime(str(raw).strip(), "%Y-%m-%dT%H:%M")
        except (TypeError, ValueError):
            return None
        return int(moment.replace(tzinfo=datetime.timezone.utc).timestamp())
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
@_form_door
def api_schedule_message():
    """Queue a composed message to be posted later, once or on a repeat."""
    denied = _require_auth_api()
    if denied:
        return denied

    data = _json_or_form()
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
@_form_door
def api_cancel_scheduled(msg_id: int):
    """Stop a queued message. Only the server it belongs to can do this."""
    denied = _require_auth_api()
    if denied:
        return denied

    data = _json_or_form()
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
@_form_door
def api_fire_scheduled(msg_id: int):
    """Send a queued message immediately, through the normal send path."""
    denied = _require_auth_api()
    if denied:
        return denied

    data = _json_or_form()
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
        reason = _send_error(e)
        print(f"[SCHEDULE] Sending #{msg_id} now failed: {e}", flush=True)
        scheduled.mark_failed(msg_id, now, reason)
        return jsonify({"success": False, "error": reason}), 502

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
        error = _send_error(e)

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
SITE_GUARD_INTERVAL_SECONDS = int(
    config_audit.get("SITE_GUARD_INTERVAL_SECONDS") or 6 * 3600
)
SITE_GUARD_FIRST_CHECK_SECONDS = int(
    config_audit.get("SITE_GUARD_FIRST_CHECK_SECONDS") or 90
)


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


#: How often the quiet-window loop looks at the clock. Half a minute, against a
#: window thirty minutes wide: the tick is what makes "03:33" mean a minute
#: rather than "some time after the next restart", and anything faster would be
#: asking a question whose answer changes once a day.
RELEASE_WINDOW_TICK_SECONDS = 30

#: How long to wait after a window's own attempt did not land everywhere. A note
#: that failed is still owed (nothing is recorded as announced), so it is retried
#: — inside the open window, because that is the hour the server agreed to hear
#: from the bot — and this is what stops sixty attempts in thirty minutes from
#: becoming the thing the channel remembers about the deploy.
RELEASE_WINDOW_RETRY_SECONDS = 300


async def release_window_loop():
    """Post each deploy's release notes inside the server's quiet window.

    A boot is a deploy and deploys happen whenever somebody pushes, so the note
    about a change made at noon used to arrive at noon — in the channel of every
    server that had bound one. The fix is a clock: between the windows the bot
    says nothing about itself, and inside one it says the same thing it always
    said, to the same places, through the same per-server records.

    Every branch is best-effort, like the loops beside it: this one is allowed to
    do nothing for hours at a time by design, so a single exception that ended it
    would be indistinguishable from a quiet night until somebody noticed the
    notes had stopped.
    """
    import asyncio

    from utils import version

    if not version.windows():
        # ``UPDATE_WINDOW_IST=always``: the boot has already announced, and there
        # is nothing for a clock to do. Said out loud rather than silently not
        # running, because "why did this loop not start" is a question a log has
        # to be able to answer.
        print(
            "[UPDATE] No quiet window configured — release notes go out when a "
            "deploy boots.",
            flush=True,
        )
        return None
    print(
        f"[UPDATE] Next release note: {version.describe_next()} "
        f"(windows: {version.WINDOW_ENV}={','.join(f'{t:%H:%M}' for t in version.windows())}).",
        flush=True,
    )
    while True:
        try:
            if version.window_is_open():
                if await _post_owed_release_notes():
                    await asyncio.sleep(RELEASE_WINDOW_RETRY_SECONDS)
                    continue
            await asyncio.sleep(RELEASE_WINDOW_TICK_SECONDS)
        except Exception as e:
            print(f"[UPDATE] Quiet-window loop error (will retry): {e}", flush=True)
            await asyncio.sleep(60)


async def _post_owed_release_notes() -> bool:
    """One pass through every server, and whether another attempt is worth making.

    True means "a server's record did not move", which is the trigger for a
    slower retry rather than a pause — either a note is still owed somewhere (so
    "nothing to do" would be wrong) or there was nothing owed at all (so asking
    again in half a minute would be pointless).

    Through the client's own handshake rather than this module's own copy of it:
    ``bot.py`` owns the silence check, the per-server "already told" records and
    the legacy update channel, and a loop with a second copy of those would drift
    from the boot that shares them.
    """
    announce = getattr(BOT_INSTANCE, "announce_release_notes", None)
    if announce is None:
        print(
            "[UPDATE] No release-note handshake on the client — nothing will be "
            "posted by the quiet window.",
            flush=True,
        )
        return False
    before = _announced_state()
    await announce()
    return _announced_state() == before


def _announced_state() -> dict:
    """Every server's recorded last-announced hash, for a before/after comparison.

    Cheap enough to do twice a minute in a window: one indexed read of a table
    with one row per server. Read straight from the database rather than from a
    count kept in this loop, because the writer is on the other side of a
    handshake and this side must not be able to claim a note landed that did not.
    """
    from utils import version

    try:
        conn = db.connect()
    except Exception as e:
        print(f"[UPDATE] Could not read the announcement records: {e}", flush=True)
        return {}
    try:
        rows = conn.execute(
            "SELECT key, value FROM kv_store WHERE key LIKE 'changelog_hash%'"
        ).fetchall()
        return {str(key): str(value) for key, value in rows}
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def start_release_window(bot):
    """Start the quiet-window loop on the bot's own event loop."""
    if bot is None or getattr(bot, "loop", None) is None:
        print("[UPDATE] No event loop — release notes will not be windowed.", flush=True)
        return None
    return bot.loop.create_task(release_window_loop())


#: How long a media pass stays good for, and how long after boot the first one
#: runs. Both come from the module that owns the interval, so the loop cannot
#: disagree with what the check says it promises; the loop waits a while after a
#: boot on purpose, because a boot is a deploy and the gateway, the database
#: restore and the command sync all happen first.
async def _run_media_check() -> None:
    """One pass, but only if the last one is old enough to be worth repeating.

    The *clock* is in the database rather than in this process, and that is the
    whole reason this is weekly in practice: Render restarts this bot often
    enough that an in-memory timer would mean a fresh sweep of forty URLs on
    every deploy — which is how a check becomes load and then gets switched off.
    """
    if not await media_health.due():
        print("[MEDIA] Last pass is still fresh — nothing to do.", flush=True)
        return
    verdict = await media_health.check()
    media_health.record(verdict)
    await media_health.mark_ran()
    print(f"[MEDIA] {media_health.describe(verdict)}", flush=True)
    message = media_health.note(verdict)
    if message and BOT_INSTANCE is not None:
        from utils import operator_log

        await operator_log.media_alert(BOT_INSTANCE, verdict, message)


async def media_health_loop():
    """Probe every media URL this bot depends on, on a weekly clock.

    Nothing in the product can notice this failure: a dead link is not raised,
    logged or refused — it is a card with an empty box in it, and the only
    repair is somebody seeing it. So the loop exists to be the thing that sees
    it, and every branch is best-effort so that the loop outlives a bad response
    from a CDN.
    """
    import asyncio

    await asyncio.sleep(max(1, media_health.FIRST_CHECK_SECONDS))
    while True:
        try:
            await _run_media_check()
        except Exception as e:
            print(f"[MEDIA] Loop error (will retry): {e}", flush=True)
        # One hour is the floor even when a pass is not due: the question is
        # cheap to ask, and a weekly sleep would make a boot the only time it is
        # ever considered.
        await asyncio.sleep(3600)


def start_media_health(bot):
    """Start the media watch on the bot's own event loop."""
    if bot is None or getattr(bot, "loop", None) is None:
        print("[MEDIA] No event loop — the pictures will not be watched.", flush=True)
        return None
    return bot.loop.create_task(media_health_loop())


#: How often to re-read UptimeRobot. Five minutes is chosen against the source,
#: not against the page: the free API allows ten calls a minute and the numbers
#: it returns are computed on UptimeRobot's side over a day or a month, so a
#: faster poll would spend somebody's rate limit to learn the same thing. The
#: first pass is early because a boot is a deploy, and "did the deploy stay up?"
#: is the question this panel answers.
UPTIME_INTERVAL_SECONDS = int(config_audit.get("UPTIME_INTERVAL_SECONDS") or 300)
UPTIME_FIRST_CHECK_SECONDS = int(config_audit.get("UPTIME_FIRST_CHECK_SECONDS") or 60)


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
    """The first port nothing is already listening on, from ``start`` upwards.

    The probe binds the **loopback** address, which is the question being asked:
    "is this port free on this machine". ``0.0.0.0`` answers a different one — it
    asks whether the whole interface range is available — and it needs more
    privilege than the probe is worth. (``app.run`` below still binds every
    interface, deliberately: Render reaches the container from outside it.)
    """
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def run():
    port = int(config_audit.get("PORT", "8080"))
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


_SERVER_STARTED = False

def keep_alive(bot=None):
    global BOT_INSTANCE, _SERVER_STARTED
    BOT_INSTANCE = bot
    if _SERVER_STARTED:
        return
    _SERVER_STARTED = True
    t = Thread(target=run)
    t.daemon = True
    t.start()

