"""Action GIFs and pet pictures, **fetched and checked** rather than hardcoded.

Every roleplay card in this bot used to carry a hand-typed ``media.tenor.com``
URL. Those URLs rot: the file is deleted, the id is mistyped once, or it never
existed at all — and the failure is invisible to the bot, because Discord accepts
the message and silently draws an empty placeholder where the picture was. That
is exactly what was reported ("no gif"), and every single URL in ``ACTIONS_DATA``
answered **404** when it was checked.

The fix cannot be "paste better links", because the same thing happens again on
the next link. So nothing here is hardcoded to a file: an action is *resolved* at
the moment it is used, from free providers that need no API key, and the answer
is **verified to be a real image** before it is allowed into a card.

Three rules:

* **Verified, or absent.** A URL is used only after a request confirms it answers
  with an ``image/*`` body. If nothing can be confirmed the caller gets ``None``
  and simply sends no picture — an absent image is a smaller lie than a broken
  one.
* **Cached per action.** Verification costs a request, so a confirmed URL is
  reused for :data:`CACHE_TTL` seconds; a provider that just failed is skipped for
  :data:`FAILURE_COOLDOWN` seconds. Neither cache is shared with the caller, so a
  command never waits on a provider it already knows is down.
* **Never raising.** This module returns ``None`` on every error. A GIF is
  decoration; it must not be able to take a command down.

The providers here are third parties and can disappear, which is why there are
two of them and why the routes are a data table rather than control flow.
"""
from __future__ import annotations

import time

import aiohttp

#: How long a confirmed URL is reused before it is fetched again.
CACHE_TTL = 6 * 3600
#: How long a provider that just failed is skipped, so one dead API cannot add a
#: timeout to every command for the rest of the day.
FAILURE_COOLDOWN = 15 * 60
#: Long enough for a slow free API; short enough that a command still answers.
REQUEST_TIMEOUT = 6.0

#: How much of a response body is read to confirm it is an image. Enough for the
#: content-type and the magic bytes, and small enough that a 2 MB GIF is not
#: downloaded just to be looked at.
PROBE_BYTES = 64

#: Providers a reaction can be resolved from, by name. ``pick`` reads the URL out
#: of that provider's payload shape; ``render`` builds the request URL.
PROVIDERS: dict[str, dict] = {
    "purrbot": {
        "url": "https://api.purrbot.site/v2/img/sfw/{reaction}/gif",
        "pick": lambda payload: payload.get("link"),
    },
    "otakugifs": {
        "url": "https://api.otakugifs.xyz/gif?reaction={reaction}",
        "pick": lambda payload: payload.get("url"),
    },
}

#: Which providers can serve which action, in the order they are tried. Reads as
#: "a poke is a purrbot poke, else an otakugifs poke"; ``bonk`` has no provider of
#: its own anywhere, so it borrows a punch and then a slap, which is what a bonk
#: is drawn as anyway.
ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    "poke": (("purrbot", "poke"), ("otakugifs", "poke")),
    "hug": (("purrbot", "hug"), ("otakugifs", "hug")),
    "pat": (("purrbot", "pat"), ("otakugifs", "pat")),
    "kiss": (("purrbot", "kiss"), ("otakugifs", "kiss")),
    "cuddle": (("purrbot", "cuddle"), ("otakugifs", "cuddle")),
    "slap": (("purrbot", "slap"), ("otakugifs", "slap")),
    "bite": (("purrbot", "bite"), ("otakugifs", "bite")),
    "punch": (("otakugifs", "punch"), ("purrbot", "slap")),
    "bonk": (("otakugifs", "punch"), ("purrbot", "slap")),
    "feed": (("purrbot", "feed"),),
}

#: Pet pictures, by the flavour that asked for them. ``cat`` is an *animated* gif
#: because that is the thing a soundboard card was missing. A source either
#: answers with JSON to be read by ``pick``, or serves the image itself
#: (``direct``), in which case the response *is* the answer.
PET_SOURCES: dict[str, tuple[dict, ...]] = {
    "cat": (
        {"url": "https://cataas.com/cat/gif", "direct": True},
        {"url": "https://cataas.com/cat", "direct": True},
    ),
    "dog": (
        {"url": "https://dog.ceo/api/breeds/image/random", "pick": lambda payload: payload.get("message")},
        {
            "url": "https://api.thedogapi.com/v1/images/search",
            "pick": lambda payload: (payload[0] or {}).get("url") if isinstance(payload, list) and payload else None,
        },
    ),
}

#: ``action -> (verified_at, url)``. Only confirmed images are stored.
_ACTION_CACHE: dict[str, tuple[float, str]] = {}
#: ``provider -> (failed_at, reason)``, so a dead provider is not retried per call.
_FAILED_AT: dict[str, float] = {}


def clear_caches() -> None:
    """Drop both caches (a test seam, and a way to re-probe after a fix)."""
    _ACTION_CACHE.clear()
    _FAILED_AT.clear()


def candidates(action: str) -> tuple[tuple[str, str], ...]:
    """The ``(provider, reaction)`` pairs to try for ``action``, in order."""
    return ROUTES.get(str(action or "").strip().lower(), ())


def pick_url(provider: str, payload) -> str | None:
    """Read a URL out of a provider's payload, or ``None`` if it is not there."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        return None
    try:
        url = spec["pick"](payload)
    except Exception:
        return None
    url = str(url or "").strip()
    if not url.startswith("http"):
        return None
    return url


def request_url(provider: str, reaction: str) -> str | None:
    """The URL to ask a provider for one reaction."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        return None
    return spec["url"].format(reaction=reaction)


async def _probe(session, url: str) -> bool:
    """True when ``url`` really answers with an image.

    Asks for the first few bytes only and hangs up, so confirming a 2 MB GIF
    costs one round-trip instead of a download.
    """
    try:
        response = await session.get(url, headers={"Range": f"bytes=0-{PROBE_BYTES - 1}"}, timeout=REQUEST_TIMEOUT)
    except Exception:
        return False
    try:
        status = getattr(response, "status", 0) or 0
        if status not in (200, 206):
            return False
        content_type = ""
        headers = getattr(response, "headers", None) or {}
        content_type = str(headers.get("Content-Type", "")).lower()
        if not content_type.startswith("image/"):
            return False
        content = getattr(response, "content", None)
        if content is not None:
            try:
                await content.read(PROBE_BYTES)
            except Exception:
                pass
        return True
    finally:
        close = getattr(response, "release", None) or getattr(response, "close", None)
        if callable(close):
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass


async def _fetch_json(session, url: str):
    """A provider's JSON body, or ``None`` on any failure."""
    try:
        response = await session.get(url, timeout=REQUEST_TIMEOUT)
    except Exception:
        return None
    try:
        status = getattr(response, "status", 0) or 0
        if status != 200:
            return None
        try:
            return await response.json(content_type=None)
        except TypeError:
            return await response.json()
        except Exception:
            return None
    finally:
        close = getattr(response, "release", None) or getattr(response, "close", None)
        if callable(close):
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass


async def action_gif(action: str, *, session=None, now: float | None = None) -> str | None:
    """A verified GIF URL for ``action``, or ``None``.

    Cached, verified, and total: an unreachable provider, a provider that changed
    its payload, or a URL that 404s all end in ``None`` rather than an exception
    or a broken image in somebody's chat.
    """
    action = str(action or "").strip().lower()
    if action not in ROUTES:
        return None

    moment = time.monotonic() if now is None else now
    cached = _ACTION_CACHE.get(action)
    if cached and moment - cached[0] < CACHE_TTL:
        return cached[1]

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        for provider, reaction in candidates(action):
            failed_at = _FAILED_AT.get(provider)
            if failed_at is not None and moment - failed_at < FAILURE_COOLDOWN:
                continue
            url = request_url(provider, reaction)
            if not url:
                continue
            payload = await _fetch_json(session, url)
            if payload is None:
                _FAILED_AT[provider] = moment
                continue
            candidate = pick_url(provider, payload)
            if not candidate:
                _FAILED_AT[provider] = moment
                continue
            if await _probe(session, candidate):
                _FAILED_AT.pop(provider, None)
                _ACTION_CACHE[action] = (moment, candidate)
                return candidate
            # The provider answered but its file is gone: try the next one, and
            # remember only that this provider is unhealthy.
            _FAILED_AT[provider] = moment
    finally:
        if own_session:
            try:
                await session.close()
            except Exception:
                pass
    return None


async def pet_picture(kind: str, *, session=None) -> str | None:
    """A verified picture for a flavour's pet, or ``None``.

    Deliberately not cached per request the way an action is: these endpoints are
    *designed* to return something new each time, and a soundboard that shows the
    same cat forever is worse than one that shows a different cat.
    """
    sources = PET_SOURCES.get(str(kind or "").strip().lower(), ())
    if not sources:
        return None

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        for spec in sources:
            provider = spec["url"]
            if _FAILED_AT.get(provider) is not None and time.monotonic() - _FAILED_AT[provider] < FAILURE_COOLDOWN:
                continue
            candidate = None
            if not spec.get("direct"):
                payload = await _fetch_json(session, spec["url"])
                if payload is not None:
                    try:
                        candidate = spec["pick"](payload)
                    except Exception:
                        candidate = None
                    candidate = str(candidate or "").strip()
                    if not candidate.startswith("http"):
                        candidate = None
            else:
                # The endpoint serves the image itself, so a successful probe of
                # the endpoint *is* the picture (and costs the one request it
                # would have taken to verify a JSON-provided URL anyway).
                candidate = spec["url"]
            if candidate and await _probe(session, candidate):
                _FAILED_AT.pop(provider, None)
                return candidate
            _FAILED_AT[provider] = time.monotonic()
    finally:
        if own_session:
            try:
                await session.close()
            except Exception:
                pass
    return None
