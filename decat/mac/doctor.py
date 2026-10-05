"""``decat doctor`` — prove this machine can answer, before anybody asks it to.

A feature whose model lives on somebody's laptop fails in ways the bot cannot see:
Ollama not installed, installed but not running, running but with the model never
pulled, pulled but six gigabytes short in memory, reachable but with the search
engine blocked, or pointed at a credential the host does not accept. Each of those
is one sentence here and a mystery in Discord.

Two levels, because they have different costs:

* :func:`quick_checks` — no tokens spent, short timeouts, cached for half a
  minute, and safe to draw on a page somebody reloads.
* :func:`full_checks` — spends real work: a completion with a token count so the
  speed on *this* machine is measured rather than assumed, one search, and a
  one-pixel vision probe. This is the one that answers "is the model I picked
  actually fast enough here", which is the question a benchmark table cannot
  answer for a particular laptop.
"""
from __future__ import annotations

import base64
import time

from decat import config
from decat.mac import net, ollama as ollama_module

#: The quick checks run on page loads, so they are cached. Long enough that a
#: reload does not re-probe, short enough that flipping something and reloading
#: shows the change.
QUICK_TTL = 30.0
_quick_cache: tuple[float, list[tuple[str, bool, str]]] | None = None

#: A 1x1 transparent PNG — the smallest thing a vision model can be asked about.
_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _ollama() -> ollama_module.Ollama:
    return ollama_module.Ollama(host=config.ollama_host(), model=config.model(), timeout=8.0)


def quick_checks(*, fresh: bool = False) -> list[tuple[str, bool, str]]:
    """The cheap facts: is it wired, is Ollama up, is the model pulled, is the host there."""
    global _quick_cache
    now = time.time()
    if not fresh and _quick_cache and now - _quick_cache[0] < QUICK_TTL:
        return _quick_cache[1]

    checks: list[tuple[str, bool, str]] = []

    token = config.token()
    checks.append((
        "credential",
        bool(token),
        "DECAT_TOKEN is set" if config.token_is_scoped()
        else ("falling back to ADMIN_PASSKEY — set DECAT_TOKEN to scope it" if token
              else "no DECAT_TOKEN and no ADMIN_PASSKEY"),
    ))

    # Two models, because `/decat` has two depths: `Deep` answers with the big one
    # and `Fast` with the small one. A `Fast` question on a machine that never
    # pulled the small one is an error about a model, not a slow answer, so it gets
    # its own line here rather than being discovered in Discord.
    wanted = config.model()
    fast = config.fast_model()
    separate_fast = bool(fast) and fast != wanted

    try:
        names = _ollama().models()
        checks.append(("ollama", True, f"answering on {config.ollama_host()} ({len(names)} model(s))"))
        checks.append((
            "model",
            any(name.split(":")[0] == wanted.split(":")[0] for name in names),
            f"{wanted} is pulled" if any(name == wanted for name in names)
            else f"{wanted} is not pulled — run: ollama pull {wanted}",
        ))
        if separate_fast:
            checks.append((
                "fast model",
                any(name == fast for name in names),
                f"{fast} is pulled (what `Fast` answers with)"
                if any(name == fast for name in names)
                else f"{fast} is not pulled — run: ollama pull {fast} (or blank DECAT_FAST_MODEL)",
            ))
    except Exception as exc:  # noqa: BLE001 — the check reports, it does not raise
        checks.append(("ollama", False, f"not reachable ({type(exc).__name__}) — is `ollama serve` running?"))
        checks.append(("model", False, "unknown until Ollama answers"))
        if separate_fast:
            checks.append(("fast model", False, "unknown until Ollama answers"))

    if token:
        try:
            from urllib.parse import urlsplit

            host = urlsplit(config.space_url()).hostname or ""
            net.get_json(f"{config.space_url()}/health", allowed=(host,), timeout=6.0)
            checks.append(("host", True, f"{config.space_url()} answers /health"))
        except Exception as exc:  # noqa: BLE001
            checks.append(("host", False, f"{config.space_url()} did not answer ({type(exc).__name__})"))
    else:
        checks.append(("host", False, "not checked — no credential to present"))

    _quick_cache = (now, checks)
    return checks


def full_checks() -> list[tuple[str, bool, str]]:
    """Everything :func:`quick_checks` does, plus the three that cost something."""
    checks = list(quick_checks(fresh=True))

    # Speed, measured here rather than quoted from a table.
    try:
        client = _ollama()
        started = time.time()
        reply = client.chat(
            [{"role": "user", "content": "Reply with the single word: ready"}],
            timeout=120.0,
        )
        wall = max(0.001, time.time() - started)
        rate = (reply.eval_count / wall) if reply.eval_count else 0.0
        checks.append((
            "speed",
            True,
            f"first answer in {wall:.1f}s"
            + (f" · ~{rate:.1f} tokens/s" if rate else " · (the model reported no token count)"),
        ))
        # Warm now, so the first real question does not pay the load.
        checks.append(("warm", True, f"{reply.model or config.model()} is loaded and held for {ollama_module.KEEP_ALIVE}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("speed", False, f"the model did not answer ({type(exc).__name__})"))
        checks.append(("warm", False, "not measured"))

    # Vision, with the smallest image that exists.
    try:
        reply = _ollama().chat(
            [
                {"role": "user", "content": "What colour is this one pixel?", "images": [base64.b64encode(_PIXEL_PNG).decode()]}
            ],
            timeout=120.0,
        )
        checks.append(("vision", True, f"the model accepted an image and said {reply.text[:40]!r}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("vision", False, f"the model refused an image ({type(exc).__name__}) — pick a vision model"))

    # The one third party that ever sees anything.
    provider = config.search_provider()
    if provider == "none":
        checks.append(("search", True, "off — answers come from the model alone and say so"))
    else:
        from decat.mac.tools import web

        try:
            found = web.search("what is today's date", provider=provider,
                               api_key=config.search_key(), searxng_url=config.searxng_url())
            checks.append(("search", bool(found), f"{provider}: {len(found)} result(s) for a test query"))
        except Exception as exc:  # noqa: BLE001
            checks.append(("search", False, f"{provider} did not answer ({type(exc).__name__})"))
    return checks


def main(argv: list[str] | None = None) -> int:
    """Print the checks, and exit non-zero if anything that must work does not."""
    quick_only = bool(argv and "--quick" in argv)
    checks = quick_checks(fresh=True) if quick_only else full_checks()
    width = max(len(name) for name, _ok, _detail in checks)
    print("Decat doctor — this machine\n")
    for name, ok, detail in checks:
        print(f"  {'✅' if ok else '❌'} {name.ljust(width)}  {detail}")
    # `search` and `host` are warnings rather than failures: a question still gets
    # an answer when either is down, and the card says how.
    must = [name for name, ok, _d in checks if not ok and name not in {"search", "host"}]
    print()
    if must:
        print(f"Not ready: {', '.join(must)}.")
        return 1
    print("Ready. Nothing about a question reaches a model vendor; the search engine"
          " named above is the only third party that sees anything.")
    return 0
