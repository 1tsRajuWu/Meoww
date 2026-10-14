"""What the cat has to fetch before it can say anything: facts, pictures, sentences.

`utils/devtoy` owns every line the cat is *written* to say. This module owns the
things it cannot know in advance, and there are three of them:

* **A fact.** ``/cat fact``. ``catfact.ninja`` first, ``meowfacts`` second, and
  the written pool in ``utils/devtoy`` if neither answers. One fact is not
  interesting twice, so the last few are remembered and not repeated.
* **A picture.** A real cat, from a real service, **checked to be an image**
  before it is used. This is `utils/gifs`'s job already — ``pet_picture("cat")``
  resolves and verifies one for the soundboard — so the work is not done twice
  here; :func:`photo` is the one name the cat's cog calls.
* **A sentence.** ``/cat ask``, the answers it gives in chat, and the cards it
  sends by itself. A language model writes those, and this is the module that
  talks to it.

Three rules, and they are `utils/gifs`'s rules because the reasoning is the same
one: **verified, or absent** (a fact that did not arrive is refused rather than
invented); **cached** (a free API is not asked the same question every time a
command is typed); and **never raising** (this is decoration on a toy — a
provider that is down must cost the cat a sentence, not the command).

The brain is **keyless by default and better with a key**, which is the shape the
rest of this tree already uses for anything optional: with ``CAT_AI_KEY`` set the
cat is answered by the operator's own endpoint (``CAT_AI_URL``, ``CAT_AI_MODEL``,
any OpenAI-compatible one), and with it unset it is answered by a public keyless
model instead, so the feature works in a fresh checkout and improves the moment a
key is added. If neither answers, the caller gets ``None`` and says one of the
lines written for it — which is why the cat is never mute, and never speaks in
somebody else's voice for long.

Nothing a model writes reaches Discord unfiltered: :func:`clean` is the one door,
and it exists because the words are somebody else's. It drops every mention and
every link — a toy that can be talked into pinging ``@everyone`` is a toy that
should not have been given a model — and it refuses anything empty.
"""
from __future__ import annotations

import re
import time

import aiohttp

from utils import config_audit, gifs

#: Long enough for a free lookup API on a slow day; short enough that a command
#: still answers. The same budget `utils/gifs` allows a provider.
REQUEST_TIMEOUT = 8.0
#: A model *writes*, it does not look up, so this is the slowest call in the toy.
#: 20s is the point at which a member has already given up on the card.
BRAIN_TIMEOUT = 20.0

#: How many recent facts are remembered so the same one is not shown twice in a
#: row. Small enough to be a ring, large enough that a session does not loop.
FACT_HISTORY = 12

#: The public fact APIs, in the order they are tried. Both answer plain JSON and
#: neither wants a key; ``pick`` reads the text out of that provider's shape.
FACT_SOURCES: tuple[dict, ...] = (
    {"id": "catfact", "url": "https://catfact.ninja/fact", "pick": lambda payload: payload.get("fact")},
    {
        "id": "meowfacts",
        "url": "https://meowfacts.herokuapp.com/",
        "pick": lambda payload: (payload.get("data") or [None])[0],
    },
)

#: What the cat asks a model, when it is configured with a key of its own. Any
#: OpenAI-compatible endpoint answers this shape, which is why one URL default is
#: enough for Groq, OpenAI, Together, OpenRouter and a local Ollama alike.
DEFAULT_AI_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_AI_MODEL = "llama-3.1-8b-instant"
#: The keyless provider, used when no key is set. It speaks the same shape, so
#: there is one request builder and the difference between the two providers is
#: three fields: the URL, the model, and whether an ``Authorization`` is sent.
PUBLIC_AI_URL = "https://text.pollinations.ai/openai"
PUBLIC_AI_MODEL = "openai"

#: :func:`clean`'s ceiling. Discord allows far more; a chat card does not.
CLEAN_LIMIT = 280

#: Anything that could notify a person or point somewhere else. Both are refused
#: rather than escaped: a sentence that mentions somebody is not a sentence this
#: toy is allowed to send, and a link in a cat's mouth is a phishing vector with a
#: tail on it.
_MENTION = re.compile(r"<@[!&]?\d+>|<#\d+>|@(?:everyone|here)\b", re.IGNORECASE)
_LINK = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
#: Quotes and code fences a model wraps its answer in. A card that shows
#: ``"I like tuna."`` is showing the model's formatting, not the cat talking.
_WRAPPED = re.compile(r"^[\s\"'`\u201c\u2018]+|[\s\"'`\u201d\u2019]+$")

#: Facts already shown, newest last, so the next one is not a repeat. This is the
#: only memory facts keep: **there is no cache that serves a repeat**, because a
#: card asks for a new fact every time by design, and a fresh fetch is also how a
#: provider that came back is noticed.
_FACT_HISTORY_SEEN: list[str] = []
#: ``provider id -> failed_at``, so a provider that just refused is skipped rather
#: than adding its timeout to every card for the next quarter of an hour.
_FAILED_AT: dict[str, float] = {}
#: How long a provider that just failed is left alone.
FAILURE_COOLDOWN = 15 * 60


def clear_caches() -> None:
    """Drop every memory (a test seam, and a way to re-probe after a fix)."""
    _FACT_HISTORY_SEEN.clear()
    _FAILED_AT.clear()


def _remember(fact: str) -> None:
    _FACT_HISTORY_SEEN.append(fact)
    del _FACT_HISTORY_SEEN[:-FACT_HISTORY]


def fact_text(payload, source_id: str) -> str | None:
    """The fact inside a provider's payload, or ``None`` if it is not there.

    Read through the table rather than by hand at each call site: a provider that
    changes its shape then reads as *no fact* instead of as an exception on a
    member's screen.
    """
    for spec in FACT_SOURCES:
        if spec["id"] != source_id:
            continue
        try:
            text = spec["pick"](payload)
        except Exception:
            return None
        text = _SPACE.sub(" ", str(text or "")).strip()
        if not text or len(text) > 600:
            return None
        return text
    return None


async def _fetch_json(session, url: str, timeout: float):
    """A provider's JSON body, or ``None`` on any failure at all."""
    try:
        response = await session.get(url, timeout=timeout)
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
        await _release(response)


async def _release(response) -> None:
    """Hand a response back to the pool, whichever spelling this aiohttp uses."""
    close = getattr(response, "release", None) or getattr(response, "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if hasattr(result, "__await__"):
            await result
    except Exception:
        pass


async def fact(*, session=None, now: float | None = None) -> str | None:
    """A real cat fact, or ``None`` when no provider answered.

    Every call asks for a *new* fact — that is what the command is for — so there
    is no cache here that serves one: a fact already shown is skipped in favour of
    the next provider, and ``None`` is returned when none of them answers at all,
    which is the caller's cue to say something from the written pool instead.
    """
    moment = time.monotonic() if now is None else now

    #: The first *repeat* a provider offered, kept in case every one of them is
    #: out of sentences this session: the same fact twice still beats no card.
    repeat: str | None = None
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        for spec in FACT_SOURCES:
            source_id = spec["id"]
            failed_at = _FAILED_AT.get(source_id)
            if failed_at is not None and moment - failed_at < FAILURE_COOLDOWN:
                continue
            payload = await _fetch_json(session, spec["url"], REQUEST_TIMEOUT)
            text = fact_text(payload, source_id) if payload is not None else None
            if not text:
                _FAILED_AT[source_id] = moment
                continue
            _FAILED_AT.pop(source_id, None)
            if text in _FACT_HISTORY_SEEN:
                # A provider that keeps handing back the same sentence is not
                # broken, it is just out of new ones — try the next one.
                repeat = repeat or text
                continue
            _remember(text)
            return text
    finally:
        if own_session:
            try:
                await session.close()
            except Exception:
                pass
    # A repeat only when a provider offered one this call: better the same fact
    # twice than an empty card. ``None`` means every provider failed, and the
    # caller says something from ``utils/devtoy``'s written pool instead.
    return repeat


async def photo(*, session=None) -> str | None:
    """One verified cat picture URL, or ``None``.

    Done by the resolver that already exists for the soundboard's cat, so the
    verification rule lives in exactly one place (see ``utils/gifs``). Nothing is
    cached on purpose: these services are *designed* to answer with a different
    cat each time, and a ``/cat photo`` that shows the same picture forever is
    worse than one that shows none.
    """
    return await gifs.pet_picture("cat", session=session)


def ai_endpoint() -> dict:
    """The brain's endpoint: the operator's own if a key is set, else the public one.

    Returns ``{"id", "url", "model", "key"}`` — ``key`` is empty for the public
    provider, which is the only difference the request builder cares about.
    """
    key = config_audit.get("CAT_AI_KEY")
    if key:
        return {
            "id": "configured",
            "url": config_audit.get("CAT_AI_URL", DEFAULT_AI_URL) or DEFAULT_AI_URL,
            "model": config_audit.get("CAT_AI_MODEL", DEFAULT_AI_MODEL) or DEFAULT_AI_MODEL,
            "key": key,
        }
    return {"id": "public", "url": PUBLIC_AI_URL, "model": PUBLIC_AI_MODEL, "key": ""}


def brain_chain() -> list[dict]:
    """The endpoints :func:`say` will try, in order.

    The public model is appended to a **configured** brain on purpose: a key that
    is wrong, expired or out of credit must not silence the cat, so the keyless
    model is the second opinion and the written lines are the third.
    """
    chosen = ai_endpoint()
    if chosen["id"] == "public":
        return [chosen]
    return [
        chosen,
        {"id": "public", "url": PUBLIC_AI_URL, "model": PUBLIC_AI_MODEL, "key": ""},
    ]


def provider_id() -> str:
    """``"configured"`` or ``"public"`` — which brain a card would be using.

    Read by the settings card so an operator can see which of the two is
    answering, since "the cat is being clever today" and "the cat is being clever
    on your key" are different facts about a deployment.
    """
    return ai_endpoint()["id"]


def clean(text, *, limit: int = CLEAN_LIMIT) -> str | None:
    """A model's answer, made safe to put in front of people, or ``None``.

    The one door between somebody else's words and this bot's chat. Mentions and
    links are **removed** rather than neutralised — there is no sentence a pet
    needs them for — surrounding quotes are stripped so the card shows the words
    rather than the model's formatting, whitespace is collapsed, and anything
    over ``limit`` is cut at a word boundary. Empty is ``None``: a caller that
    gets ``None`` says something it wrote itself, which is always better than an
    empty card.
    """
    value = _SPACE.sub(" ", str(text or "")).strip()
    value = _LINK.sub("", value)
    value = _MENTION.sub("", value)
    value = _WRAPPED.sub("", value)
    value = _SPACE.sub(" ", value).strip()
    if not value:
        return None
    if len(value) > limit:
        cut = value[:limit]
        space = cut.rfind(" ")
        value = (cut[:space] if space > limit // 2 else cut).rstrip() + "…"
    return value or None


def _chat_payload(endpoint: dict, messages: list[dict], *, temperature: float) -> dict:
    """One request body in OpenAI's chat shape, which both providers speak."""
    return {
        "model": endpoint["model"],
        "messages": list(messages),
        "temperature": temperature,
        "max_tokens": 120,
    }


async def _chat(session, endpoint: dict, payload: dict):
    """One chat completion, or ``None``. Never raises, never waits forever."""
    headers = {"Content-Type": "application/json"}
    if endpoint["key"]:
        headers["Authorization"] = f"Bearer {endpoint['key']}"
    try:
        response = await session.post(
            endpoint["url"], json=payload, headers=headers, timeout=BRAIN_TIMEOUT
        )
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
        await _release(response)


def reply_text(payload) -> str | None:
    """The sentence inside a chat completion, or ``None``.

    Only ``choices[0].message.content`` is read: some providers put their working
    in a sibling field, and the model's reasoning is not the cat's answer.
    """
    try:
        choices = payload.get("choices") or []
        message = choices[0].get("message") or {}
        return str(message.get("content") or "") or None
    except (AttributeError, IndexError, TypeError):
        return None


async def say(
    messages: list[dict],
    *,
    session=None,
    temperature: float = 0.8,
    providers=None,
) -> str | None:
    """What the cat says, in its own words, or ``None`` to use its written ones.

    The operator's endpoint first when one is configured, the public one
    otherwise, and if a provider fails it is skipped for :data:`FAILURE_COOLDOWN`
    so one dead API cannot put twenty seconds on every card.

    ``None`` is a complete answer and the common one on a bad day: every caller
    falls back to a line from ``utils/devtoy``, so the cat always speaks.
    """
    chain = list(providers) if providers is not None else brain_chain()

    moment = time.monotonic()
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        for endpoint in chain:
            failed_at = _FAILED_AT.get(endpoint["id"])
            if failed_at is not None and moment - failed_at < FAILURE_COOLDOWN:
                continue
            payload = await _chat(session, endpoint, _chat_payload(endpoint, messages, temperature=temperature))
            said = clean(reply_text(payload)) if payload is not None else None
            if not said:
                _FAILED_AT[endpoint["id"]] = moment
                continue
            _FAILED_AT.pop(endpoint["id"], None)
            return said
    finally:
        if own_session:
            try:
                await session.close()
            except Exception:
                pass
    return None
