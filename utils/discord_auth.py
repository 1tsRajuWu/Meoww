"""Signing in *as yourself*, so the dashboard can show the servers you run.

Both ways into the dashboard so far belonged to the operator: the one-time link
minted by ``/help`` is scoped to a single server at the moment it is minted, and
the passkey is the whole bot. Neither can answer the question every other bot's
dashboard answers — *which of my servers is this bot in, and which of those may I
manage?* — because that question is about the person, not about a server.

This module is that rule and the two HTTP calls it needs. The rule is pure and
testable; the calls are isolated in one function each so a test can replace them
without a network and so a caller can wrap them in its own sentences.

Nothing here reads Flask, and nothing here holds a token for longer than the
request that fetched it: the access token is used twice (who are you, which
servers do you see) and thrown away, and the session keeps only what the pages
actually need. Discord's own guidance is that the token is not stored, and the
scopes asked for are ``identify`` and ``guilds`` — no ``email``, no message
access, nothing that could be used for anything but showing those names.
"""
from __future__ import annotations

import base64
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from utils import config_audit

#: Discord's permission bits. The bar for "may configure a bot in this server"
#: is **Administrator, or being the owner** — not Manage Server.
#:
#: Manage Server was the bar until it was held against the bot's own behaviour.
#: It is the bit Discord hands to moderators, and servers routinely grant it to a
#: whole staff role, so every one of those people could sign in at
#: ``/login/discord`` and reach that server's dashboard: all eleven system
#: switches, the command list, and the bot's name, picture and bio in that server.
#: The administrator-only one-time link beside it already refused exactly those
#: people — so the dashboard had **two doors into one room with different locks,
#: and the looser one decided**. This is the tighter bar, so both doors agree and
#: the list a person sees is what a server owner means by "the admins".
#:
#: A consequence worth stating: a member who can genuinely run the server but is
#: not an Administrator now signs in and is told so plainly by the picker, rather
#: than silently receiving control the owner did not mean to hand them.
MANAGE_GUILD = 0x20
ADMINISTRATOR = 0x8
ADMIN_BITS = ADMINISTRATOR

AUTHORIZE_ENDPOINT = "https://discord.com/oauth2/authorize"
TOKEN_ENDPOINT = "https://discord.com/api/oauth2/token"
API_BASE = "https://discord.com/api/v10"

#: ``identify`` is the user id, name and avatar; ``guilds`` is the list this
#: module filters. Nothing else is requested, because nothing else is used.
LOGIN_SCOPES = ("identify", "guilds")

#: Adding a bot needs both scopes: without ``applications.commands`` the server
#: gets a bot with no slash commands at all, which is the failure ``README``
#: documents under "No slash commands at all".
INVITE_SCOPES = ("bot", "applications.commands")

#: The grant the invite link asks for. **Administrator**, one bit, and this is a
#: deliberate reversal of what used to live here: for a while the link carried
#: the explicit list below, each entry naming the call site that needs it.
#:
#: The argument for the long list was that a public invite is the page's real job
#: and "Administrator" is the first thing a careful owner reads before deciding
#: whether to add a bot. That is still true. The operator chose the other trade
#: anyway, for a reason that is equally real: **the long list is always one
#: permission behind the code.** Every feature added after it was written — a
#: moderation path Discord gates behind Manage Server, a webhook the bot grew, a
#: command needing something nobody listed — arrives in somebody's server as
#: *Missing Permissions*, which reads to them as a broken bot and costs a support
#: conversation every time it happens. One bit can never be behind.
#:
#: What it costs, said plainly rather than left in a comment: an Administrator
#: bot can do anything in any server that adds it. A fault in one command is
#: unbounded — it reaches the whole server rather than the one thing that command
#: does — and a leaked token is worse here than anywhere else.
#:
#: Which is why this is not the only link the bot offers. ``MINIMAL_PERMISSION_BITS``
#: is the old list, kept current, and ``invite_url(..., minimal=True)`` builds a
#: second link from it. Both are offered wherever somebody decides to add the
#: bot, so an owner who will not grant Administrator can still let it in — and
#: knows, from the copy beside that link, that a feature may then report a missing
#: permission they can grant one at a time.
ADMINISTRATOR_PERMISSIONS = 1 << 3

#: The bitfield the **default** invite link carries. One bit, and nothing else:
#: Administrator makes every other permission irrelevant, so OR-ing the list below
#: into it would only make the number harder to read.
INVITE_PERMISSIONS = ADMINISTRATOR_PERMISSIONS

#: The least-privilege alternative, as ``(name, bit)``, so the list can be read
#: and checked rather than decoded. **Each entry names the call site that needs
#: it** — a permission with no call site is one this bot does not need, and that
#: is the whole point of the list.
#:
#: What is *not* here is as deliberate as what is. There is no **Mention
#: Everyone** (no code path pings ``@everyone``; announcements ping a role, which
#: needs no permission), no **Connect** (music is gone and nothing joins a voice
#: channel — the voice tracker reads state changes, which needs only
#: ``view_channel``), no **Manage Server**, no **Manage Webhooks**, no **Create
#: Invite**, and none of the member-move/mute/deafen bits. A grant nothing uses
#: is still a grant: it is what a hijacked token reaches for.
MINIMAL_PERMISSION_BITS = (
    # --- answering at all ---
    ("view_channel", 1 << 10),
    ("send_messages", 1 << 11),
    ("embed_links", 1 << 14),            # every card the bot posts is an embed
    ("attach_files", 1 << 15),           # level-up cards, ticket transcripts
    ("read_message_history", 1 << 16),   # starboard, purge, automod lookback
    ("add_reactions", 1 << 6),           # starboard
    ("use_external_emojis", 1 << 18),
    # --- moderation (cogs/moderation.py, cogs/automod.py) ---
    ("kick_members", 1 << 1),            # /kick
    ("ban_members", 1 << 2),             # /ban
    ("moderate_members", 1 << 40),       # /mute, /unmute, automod's 5-minute timeout
    ("manage_messages", 1 << 13),        # /purge
    # --- structure ---
    ("manage_channels", 1 << 4),         # /lock, /unlock, ticket channels
    ("manage_roles", 1 << 28),           # level rewards, /defaultrole
    # --- identity, and the operator's own log ---
    ("change_nickname", 1 << 26),        # the per-server name in utils/identity.py
    ("view_audit_log", 1 << 7),          # who added the bot (utils/operator_log.py)
    # --- threads: the ticket panel and the /help panel ---
    ("create_public_threads", 1 << 35),
    ("send_messages_in_threads", 1 << 38),
)

#: The bitfield the **limited** invite link carries.
MINIMAL_INVITE_PERMISSIONS = sum(bit for _name, bit in MINIMAL_PERMISSION_BITS)

#: How long any single call may take. These are two small JSON requests made
#: while somebody waits for a sign-in, so a hung socket is worse than a clear
#: refusal.
TIMEOUT_SECONDS = 15.0


class AuthError(Exception):
    """A refusal that can be printed as a sentence.

    Every raise site passes something a person can act on — "Discord said the
    sign-in code had already been used; start again" rather than a status code.
    """

    def __init__(self, reason: str, *, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _client_id_from_token(token: str) -> str:
    """Extract the application/client ID encoded in the first segment of a Discord bot token."""
    if not token or "." not in token:
        return ""
    part = token.split(".", 1)[0].strip()
    padded = part + "=" * (-len(part) % 4)
    try:
        decoded = base64.b64decode(padded).decode("utf-8")
        if decoded.isdigit():
            return decoded
    except Exception:
        pass
    return ""


def client_id() -> str:
    """This application's client id, or empty when OAuth is not configured."""
    explicit = config_audit.get("DISCORD_CLIENT_ID")
    if explicit:
        return explicit
    return _client_id_from_token(config_audit.get("DISCORD_TOKEN"))


def client_secret() -> str:
    """The client secret. Server-side only; never sent to a browser."""
    return config_audit.get("DISCORD_CLIENT_SECRET")


def enabled() -> bool:
    """Whether Discord sign-in can work at all.

    Both halves are required and there is no fallback credential: a client id
    with no secret cannot complete the code exchange, and inventing a default
    for either is how a published credential gets into a deployment.
    """
    return bool(client_id() and client_secret())


def invite_url(*, guild_id: int | str | None = None, minimal: bool = False) -> str:
    """The URL that adds this bot to a server.

    With ``guild_id`` the target server is chosen already and Discord hides its
    picker (``disable_guild_select``), which is what a "add it here" button
    needs; without it the person picks any server they manage, which is what
    "add it somewhere else" needs. Somebody who manages nothing sees Discord's
    own "you need Manage Server" refusal, which is clearer than ours would be.

    ``minimal`` builds the least-privilege link instead of the Administrator one
    — same scopes, a different bitfield — for an owner who will not hand a bot
    the whole server. It is a second *link*, not a second code path: whichever
    one is used, the bot that arrives is the same bot.
    """
    params = {
        "client_id": client_id(),
        "scope": " ".join(INVITE_SCOPES),
        "permissions": str(MINIMAL_INVITE_PERMISSIONS if minimal else INVITE_PERMISSIONS),
    }
    if guild_id:
        params["guild_id"] = str(guild_id)
        params["disable_guild_select"] = "true"
    return f"{AUTHORIZE_ENDPOINT}?{urllib.parse.urlencode(params)}"


def authorize_url(redirect_uri: str, state: str, *, prompt: str | None = None) -> str:
    """Where to send somebody to sign in.

    ``state`` is the caller's anti-CSRF nonce: it is echoed back on the callback
    and must be compared before the code is exchanged, because the callback is a
    URL a browser can be sent to by anyone.
    """
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(LOGIN_SCOPES),
        "state": state,
    }
    if prompt:
        params["prompt"] = prompt
    return f"{AUTHORIZE_ENDPOINT}?{urllib.parse.urlencode(params)}"


def is_admin_of(guild: dict, user_id: int | str | None) -> bool:
    """Whether this person may configure a bot in that server.

    Discord reports ``owner`` separately from ``permissions`` (an owner has the
    bit set too, but the flag is what the API means), and ``permissions`` arrives
    as a **string** — so a comparison against an int silently excludes every
    administrator, which is the shape of mistake this function exists to make
    impossible.
    """
    if not isinstance(guild, dict):
        return False
    # ``owner`` is a JSON boolean, but a JSON-ish client may hand back "true"
    # or 1; all three mean the same thing and none of them is falsy-by-accident.
    owner = guild.get("owner")
    if owner is True or str(owner).strip().lower() in ("true", "1"):
        return True
    try:
        permissions = int(str(guild.get("permissions", 0)))
    except (TypeError, ValueError):
        return False
    return bool(permissions & ADMIN_BITS)


def servers_for(user_guilds, user_id, bot_guild_ids) -> list[dict]:
    """The servers this person may manage *and* this bot is in.

    Both halves matter and both are subtracted from the same list, so the page
    can never offer a server the bot has left (where every control would fail)
    or one the person cannot actually configure (which is the same refusal
    Discord would give, arriving later and less clearly).

    Ordered by name so the picker is stable between requests: an unordered set
    is not a choice anybody can make predictably.
    """
    known = {str(gid) for gid in (bot_guild_ids or ())}
    found: dict[str, dict] = {}
    for guild in user_guilds or ():
        if not isinstance(guild, dict):
            continue
        gid = str(guild.get("id") or "")
        if not gid or gid not in known:
            continue
        if not is_admin_of(guild, user_id):
            continue
        found[gid] = {
            "id": gid,
            "name": str(guild.get("name") or "Unnamed server"),
            "icon": str(guild.get("icon") or ""),
        }
    return sorted(found.values(), key=lambda entry: (entry["name"].lower(), entry["id"]))


def avatar_url(user: dict, size: int = 64) -> str:
    """A person's picture, or empty when they have none."""
    user = user or {}
    avatar = user.get("avatar") or ""
    uid, discriminator = user.get("id"), user.get("discriminator")
    if not avatar:
        return ""
    if avatar.startswith("a_"):
        return f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.gif?size={size}"
    return f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png?size={size}"


#: The only host a picture is ever fetched from. A helper that fetches a URL a
#: browser asked for is a request forger waiting to happen; this one is handed
#: addresses built here, from Discord's own answers, and refuses anything else —
#: so the worst a crafted parameter can reach is a Discord picture.
CDN_HOST = "https://cdn.discordapp.com/"

#: How much of one picture to read, and how long to wait. Discord's own ceiling is
#: 8 MB while the dashboard asks for 64–256 px thumbnails, so this bounds a
#: response that is not a thumbnail rather than one that is.
IMAGE_MAX_BYTES = 2 * 1024 * 1024
IMAGE_TIMEOUT_SECONDS = 6.0

#: One identity for these fetches. Discord answers a bare ``urllib`` request from
#: some networks with a 403, and a picture that arrives only on one machine is
#: worse than one that never does.
CDN_USER_AGENT = "MeowDashboard (image, 1.0)"


def fetch_image(url: str) -> tuple[bytes, str] | None:
    """One picture from Discord's CDN, or ``None``.

    Only ever called with an address built by this module or by the dashboard
    from a cached Discord object, and it re-checks anyway: the check is what
    makes the route safe, not the caller being careful.

    Returns ``(bytes, mime)``. Anything unexpected — a redirect off the CDN, a
    non-image content type, a body past the bound, a timeout — is ``None``, which
    the caller renders as "no picture" rather than as a broken one.
    """
    if not isinstance(url, str) or not url.startswith(CDN_HOST):
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": CDN_USER_AGENT})
        with urllib.request.urlopen(request, timeout=IMAGE_TIMEOUT_SECONDS) as response:
            if getattr(response, "status", 200) != 200:
                return None
            declared = (response.headers.get("Content-Type") or "").split(";")[0]
            data = response.read(IMAGE_MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None
    # One byte past the bound is how a too-large body is caught without reading
    # the whole thing; refusing it beats truncating a picture into a corrupt one.
    if not data or len(data) > IMAGE_MAX_BYTES:
        return None
    mime = declared.strip().lower()
    if not mime.startswith("image/"):
        return None
    return data, mime


def display_name(user: dict) -> str:
    """What to call somebody in the header.

    ``global_name`` is the display name Discord now encourages and is absent for
    accounts that never set one, so the username is the fallback rather than the
    other way round.
    """
    user = user or {}
    return str(user.get("global_name") or user.get("username") or "Discord user")


# ---------------------------------------------------------------------------
# The two calls, isolated
# ---------------------------------------------------------------------------


def _read_json(url: str, *, token: str | None = None, form: dict | None = None) -> dict:
    """One HTTP request, turned into JSON or into a sentence.

    Isolated so every caller above and below stays testable without a network,
    and so the *reason* Discord gives survives: a wrong redirect URI is Discord's
    most common sign-in refusal and its body says exactly which URI it expected.
    """
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    headers = {"User-Agent": "MeowDashboard (sign-in, 1.0)"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace") or "{}")
            detail = str(payload.get("error_description") or payload.get("error") or "")
        except Exception:
            detail = ""
        raise AuthError(
            f"Discord refused that ({exc.code}{': ' + detail if detail else ''}).",
            status=exc.code,
        ) from exc
    except Exception as exc:
        raise AuthError(
            "Could not reach Discord to finish signing in. Try again in a moment."
        ) from exc
    try:
        return json.loads(body.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise AuthError("Discord answered something unreadable.") from exc


def exchange_code(code: str, redirect_uri: str) -> str:
    """Trade the one-time code for an access token. Returns the token.

    The code is single-use and short-lived, and the secret proves this is the
    application the code was issued to — which is why the exchange happens here
    and never in the browser.
    """
    payload = _read_json(
        TOKEN_ENDPOINT,
        form={
            "client_id": client_id(),
            "client_secret": client_secret(),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    token = str(payload.get("access_token") or "")
    if not token:
        raise AuthError("Discord did not return an access token.")
    return token


def fetch_user(token: str) -> dict:
    """Who just signed in: id, username and avatar. Nothing else is asked for."""
    return _read_json(f"{API_BASE}/users/@me", token=token)


def fetch_user_guilds(token: str) -> list[dict]:
    """The servers that person is in, with the permission bits for each."""
    payload = _read_json(f"{API_BASE}/users/@me/guilds", token=token)
    if not isinstance(payload, list):
        raise AuthError("Discord did not return a server list.")
    return [entry for entry in payload if isinstance(entry, dict)]
