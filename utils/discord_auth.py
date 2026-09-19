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

import json
import os
import urllib.error
import urllib.parse
import urllib.request

#: Discord's permission bits. The standard bar for "may configure a bot in this
#: server": Manage Server, or the blanket Administrator that implies it. An
#: owner is allowed by definition and Discord reports the bit separately.
MANAGE_GUILD = 0x20
ADMINISTRATOR = 0x8
ADMIN_BITS = MANAGE_GUILD | ADMINISTRATOR

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

#: Every permission the invite asks for, as ``(name, bit)``, so the list can be
#: read and checked rather than decoded. **Each entry names the call site that
#: needs it** — a permission with no call site is one this bot does not need, and
#: that is the whole point of the list.
#:
#: This used to be Discord's blanket ``ADMINISTRATOR``, and the argument for it
#: was that a single digit in the URL is easier to check than a long bitfield.
#: That was the wrong trade for *this* link: it is published on a public page
#: whose entire job is to earn enough trust to be added, and "Administrator" is
#: the first thing a careful owner reads and the most common reason to close the
#: tab. Administrator also makes every other bit irrelevant, so a fault in one
#: command is unbounded — it can do anything, anywhere, instead of being limited
#: to what that command actually does.
#:
#: What is *not* here is as deliberate as what is. There is no **Mention
#: Everyone** (no code path pings ``@everyone``; announcements ping a role, which
#: needs no permission), no **Connect** (music is gone and nothing joins a voice
#: channel — the voice tracker reads state changes, which needs only
#: ``view_channel``), no **Manage Server**, no **Manage Webhooks**, no **Create
#: Invite**, and none of the member-move/mute/deafen bits. A grant nothing uses
#: is still a grant: it is what a hijacked token reaches for.
INVITE_PERMISSION_BITS = (
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

#: The bitfield the invite link carries.
INVITE_PERMISSIONS = sum(bit for _name, bit in INVITE_PERMISSION_BITS)

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


def client_id() -> str:
    """This application's client id, or empty when OAuth is not configured."""
    return (os.environ.get("DISCORD_CLIENT_ID") or "").strip()


def client_secret() -> str:
    """The client secret. Server-side only; never sent to a browser."""
    return (os.environ.get("DISCORD_CLIENT_SECRET") or "").strip()


def enabled() -> bool:
    """Whether Discord sign-in can work at all.

    Both halves are required and there is no fallback credential: a client id
    with no secret cannot complete the code exchange, and inventing a default
    for either is how a published credential gets into a deployment.
    """
    return bool(client_id() and client_secret())


def invite_url(*, guild_id: int | str | None = None) -> str:
    """The URL that adds this bot to a server.

    With ``guild_id`` the target server is chosen already and Discord hides its
    picker (``disable_guild_select``), which is what a "add it here" button
    needs; without it the person picks any server they manage, which is what
    "add it somewhere else" needs. Somebody who manages nothing sees Discord's
    own "you need Manage Server" refusal, which is clearer than ours would be.
    """
    params = {
        "client_id": client_id(),
        "scope": " ".join(INVITE_SCOPES),
        "permissions": str(INVITE_PERMISSIONS),
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


def icon_url(server: dict, size: int = 64) -> str:
    """A server's picture, or empty when it has none."""
    icon = (server or {}).get("icon") or ""
    if not icon:
        return ""
    if icon.startswith("a_"):
        return f"https://cdn.discordapp.com/icons/{server['id']}/{icon}.gif?size={size}"
    return f"https://cdn.discordapp.com/icons/{server['id']}/{icon}.png?size={size}"


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
