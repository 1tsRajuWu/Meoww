"""Every outbound request this half makes, through one door that checks the host.

``utils/plans.py`` and ``utils/discord_auth.py`` already made this argument on the
bot's side: two of the URLs this project sends to carry a **secret** — one a
bearer token, one a licence key somebody paid for — and "the URL is a constant"
is a comment while a guard that refuses every other host is a rule. This module is
the same rule for the laptop, where the secret is the machine's own credential.

Three hosts are reachable, and they are named here:

* the Space this bot runs on, which receives the credential;
* a search engine, which receives only a query somebody typed;
* Discord's CDN, which receives only an attachment URL Discord itself issued.

Nothing else, ever — and the reason it matters is not abstract. A question can be
"what does this error mean" with a stack trace in it, and the failure this guards
against is a future edit that makes that text a *request* to somewhere the
operator never agreed to.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

#: Sent when a caller does not set its own. A bare ``Python-urllib`` gets refused
#: by plenty of sites, and an honest name is better than a spoofed browser.
USER_AGENT = "Decat/1.0 (+https://dacat.pages.dev; the operator's own machine)"


class Blocked(OSError):
    """A URL this module refuses to request. An ``OSError`` so callers that
    already handle "the network failed" handle this too — the same choice
    ``utils.plans`` made, and for the same reason."""


def host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    """Whether ``host`` is exactly one of ``allowed`` or a subdomain of one.

    ``endswith`` is not enough on its own and the difference is the whole point:
    ``evil-discord.com`` ends with ``discord.com``, and ``discord.com.evil.test``
    starts with it. Both are refused here by comparing labels.
    """
    host = (host or "").strip().lower().strip(".")
    if not host:
        return False
    for name in allowed:
        name = name.strip().lower().strip(".")
        if host == name or host.endswith("." + name):
            return True
    return False


def checked_endpoint(url: str, allowed: tuple[str, ...]) -> str:
    """``url`` if its host is allowed, else raise :class:`Blocked`.

    HTTPS only, and no userinfo: ``https://user:pass@discord.com@evil.test/`` is
    a URL whose *userinfo* spells a name the real host does not have, which is the
    trick a host comparison written quickly falls for.
    """
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme != "https":
        raise Blocked(f"refusing a non-HTTPS request: {parsed.scheme or 'no scheme'}")
    if "@" in parsed.netloc:
        raise Blocked("refusing a URL with credentials in it")
    host = parsed.hostname or ""
    if not host_allowed(host, allowed):
        raise Blocked(f"refusing to send anything to {host or 'a nameless host'}")
    return url


def request_bytes(
    url: str,
    *,
    allowed: tuple[str, ...],
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    method: str | None = None,
    body: bytes | None = None,
) -> tuple[bytes, dict]:
    """One request, checked before it is built. Returns ``(body, headers)``."""
    checked_endpoint(url, allowed)
    merged = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    merged.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=merged, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            # Discard the body if the caller asked for something huge: a page that
            # turns out to be a 200 MB video is not worth the memory.
            return response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        raise OSError(f"{url} answered {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise OSError(f"{url} was unreachable: {exc.reason}") from exc
    except socket.timeout as exc:
        raise OSError(f"{url} timed out after {timeout:.0f}s") from exc


def get_text(
    url: str,
    *,
    allowed: tuple[str, ...],
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    limit: int = 2_000_000,
) -> str:
    """Fetch a page or an API answer as text, capped and decoded forgivingly."""
    raw, response_headers = request_bytes(
        url, allowed=allowed, headers=headers, timeout=timeout
    )
    if len(raw) > limit:
        raw = raw[:limit]
    charset = "utf-8"
    content_type = str(response_headers.get("Content-Type", ""))
    if "charset=" in content_type:
        charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
    return raw.decode(charset, errors="replace")


def get_json(
    url: str,
    *,
    allowed: tuple[str, ...],
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> object:
    text = get_text(url, allowed=allowed, headers=headers, timeout=timeout, limit=4_000_000)
    return json.loads(text)


def post_json(
    url: str,
    payload: dict,
    *,
    allowed: tuple[str, ...],
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> object:
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    body = json.dumps(payload).encode("utf-8")
    raw, _headers = request_bytes(
        url, allowed=allowed, headers=merged, timeout=timeout, method="POST", body=body
    )
    return json.loads(raw.decode("utf-8", errors="replace") or "{}")
