"""Every setting ``/decat`` reads, in one place, on both sides of the wire.

The feature has two halves running on two machines, so "which environment
variable turns this on" is a question somebody will ask twice and answer
differently once. Both halves read their settings from here.

Two rules this module keeps, both learned from the rest of this deployment:

* **A blank value switches a thing off rather than half-enabling it** — the same
  rule ``.env.example`` states for every other optional setting. A blank
  ``DECAT_TOKEN`` is not "the empty token", it is "no token configured", and the
  caller says so rather than comparing two empty strings and calling it a match.
* **The scope is a deployment fact, not a literal in the logic.** The two servers
  this runs in are named in ``DECAT_GUILDS``; leaving it blank means the
  per-server switch is the only gate, which is exactly how every other beta here
  is scoped — see the *Alone Time* entry in ``utils/features.py``. Naming them in
  the environment is how the harder promise ("only these two, ever") is kept
  without a guild id anywhere in the code.
"""
from __future__ import annotations

import os
from pathlib import Path

from decat import protocol

#: Where the Mac keeps everything of its own: config, the switch's state, and —
#: for `public` questions only — the transcript.
#:
#: Named `HOME_SETTING` rather than `HOME_ENV` on purpose, and the reason is worth
#: a paragraph because it is the one thing in this module that is not obvious.
#: ``utils/config_audit.py`` is the registry of **this deployment's** environment —
#: what the hosted service can be configured with — and ``tests/test_config_audit``
#: reads the tree for environment names to keep that registry complete. That
#: scanner recognises a constant whose name ends in ``ENV``, so calling this
#: ``HOME_ENV`` would enrol exactly one of the twelve settings below in a registry
#: about the *host*, while the other eleven stayed invisible: a half-covered
#: surface, which is worse than either whole answer. The settings here are the
#: laptop's, they are read by a program running on a different machine, and they
#: are documented where that machine's operator reads them — ``decat/README.md``
#: and the file ``decat/mac/install.sh`` writes. The two names the *deployment*
#: itself needs (``DECAT_TOKEN``, ``DECAT_GUILDS``) are in ``.env.example``.
HOME_SETTING = "DECAT_HOME"


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def home() -> Path:
    """The laptop's own directory. Never read by the Space."""
    raw = env(HOME_SETTING)
    return Path(raw).expanduser() if raw else Path.home() / ".decat"


def token() -> str:
    """The credential the laptop presents, falling back to the admin passkey.

    A dedicated ``DECAT_TOKEN`` is better than reusing ``ADMIN_PASSKEY`` for a
    reason worth writing down: the passkey opens the whole operator surface, and
    a laptop is the machine most likely to be lost. Until one is set the fallback
    keeps the feature working with the secrets the deployment already has, and
    ``/bot decat`` says plainly which one is in use so the weaker choice is
    visible rather than assumed.
    """
    return env("DECAT_TOKEN") or env("ADMIN_PASSKEY")


def token_is_scoped() -> bool:
    return bool(env("DECAT_TOKEN"))


def allowed_guilds() -> frozenset[int]:
    """The servers named in ``DECAT_GUILDS``, or an empty set for "unnamed".

    Empty means *no hard allow-list*: the per-server switch decides, and an
    administrator turning the system on is the decision. A non-empty list is a
    second, stricter gate that no dashboard click can widen.
    """
    raw = env("DECAT_GUILDS")
    found: set[int] = set()
    for piece in raw.replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            found.add(int(piece))
        except ValueError:
            print(f"[DECAT] Ignoring DECAT_GUILDS entry {piece!r}: not a server id", flush=True)
    return frozenset(found)


def guild_allowed(guild_id: int | None) -> bool:
    """Whether the deployment names this server. Blank list allows everything."""
    if guild_id is None:
        return False
    named = allowed_guilds()
    return not named or int(guild_id) in named


# ── the laptop's half ────────────────────────────────────────────────────────

def space_url() -> str:
    """Where the bot lives, as the laptop reaches it."""
    return env("DECAT_SPACE", "https://1tsrajowo-meow.hf.space").rstrip("/")


#: The model this half runs when nobody says otherwise, and the reason it is the
#: small one rather than the better one: the machine is a laptop.
#:
#: The 8b was the default until this feature ran on the Air it was written for.
#: Six gigabytes of weights plus a KV cache is a fine trade on a desktop; on a
#: 16 GB fanless Air that is also running a browser and Discord it *paged about
#: 1.9 GB out* during the first live question, and the answer took 31 seconds of
#: which a visible part was load and swap. The 4b answers the same questions from
#: the same sources at 3.2 GB resident, and `Deep` still means what it always
#: meant — reads more pages, then re-reads its own draft — so the depth choice
#: survives the smaller model intact. `decat/mac/install.sh` picks by the RAM it
#: finds; this is the value for a machine nobody has measured.
DEFAULT_MODEL = "qwen3-vl:4b"

#: The bigger model, named here so the installer and the docs agree on one string.
BIG_MODEL = "qwen3-vl:8b"

#: The RAM a machine needs before the big model is the sensible default.
BIG_MODEL_RAM_BYTES = 24 * 1024**3

#: How long the weights stay resident after an answer. Ten minutes, not the half
#: hour this started as: the memory coming back matters more on a laptop than the
#: next question skipping a load, and ten minutes still covers a conversation.
DEFAULT_KEEP_ALIVE = "10m"


def model() -> str:
    """This machine's model. `qwen3-vl` because it is the one that does all of it."""
    return env("DECAT_MODEL", DEFAULT_MODEL)


def fast_model() -> str:
    """The lane for short questions. On one model this is the same model, and Fast
    is still a real choice — it reads two pages instead of three and skips the
    re-check pass, which is most of the time."""
    value = env("DECAT_FAST_MODEL")
    return value or model()


def keep_alive() -> str:
    """How long Ollama holds the weights after an answer (`0` gives them back at
    once, `-1` holds them until Ollama stops, durations like `10m` are the middle).
    """
    return env("DECAT_KEEP_ALIVE") or DEFAULT_KEEP_ALIVE


def ollama_host() -> str:
    return env("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def search_provider() -> str:
    """`none`, `ddg`, `brave` or `searxng` — the only third party that sees anything.

    The model is local, so no vendor ever receives a question. A *search* is
    different: the query leaves the laptop for whichever engine is named here.
    `none` is therefore a real answer, not a broken configuration — it means the
    laptop answers from the model's own knowledge and the card says so.
    """
    value = env("DECAT_SEARCH", "ddg").lower()
    return value if value in {"none", "ddg", "brave", "searxng"} else "ddg"


def search_key() -> str:
    return env("DECAT_SEARCH_KEY")


def searxng_url() -> str:
    return env("DECAT_SEARXNG_URL").rstrip("/")


def poll_wait() -> float:
    """How long one poll is held open. The answer's latency floor is zero either
    way; this is how often an idle laptop speaks to the Space."""
    try:
        return max(1.0, min(20.0, float(env("DECAT_POLL_WAIT", "15"))))
    except ValueError:
        return 15.0


# ── the Space's half ─────────────────────────────────────────────────────────

def heartbeat_stale() -> float:
    """Seconds without a poll after which the laptop is called offline.

    Comfortably longer than one held-open poll, because the failure this must not
    have is calling a working laptop offline: a false "offline" is a question
    nobody can ask, while a few seconds of patience costs nothing.
    """
    try:
        return max(8.0, float(env("DECAT_STALE_SECONDS", "40")))
    except ValueError:
        return 40.0


def deadline_seconds() -> float:
    """How long a question may stay open before the asker is told it went quiet.

    Bounded well below Discord's own fifteen-minute deferred-response window, so
    the card that says "gone quiet" is always the card the asker actually sees.
    """
    try:
        return max(20.0, min(600.0, float(env("DECAT_DEADLINE_SECONDS", "180"))))
    except ValueError:
        return 180.0


def max_question_chars() -> int:
    return protocol.MAX_QUESTION
