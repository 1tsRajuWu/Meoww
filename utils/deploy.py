"""Handing a deploy to Render, and saying plainly when there is nothing to hand.

A bot process cannot commit. Whatever is running here was built by the host from
the repository, so "push the update" from inside Discord can only ever mean
*deploy what is already on GitHub* — which is one POST to a Render Deploy Hook.
With no hook configured, the honest answer is that no code moved, and that is
what :func:`configured` is for: the caller can tell the operator which of the two
things just happened instead of implying a deploy that never started.

The hook URL is a credential: anyone holding it can trigger a build, so it is
read from the environment and never appears in a message, a log line or an
error — including the ``URLError`` that would happily quote it back.
"""

import asyncio
import urllib.error
import urllib.request

from utils import config_audit

#: Where the deploy hook lives. Unset means this bot can read its own version and
#: post release notes, but cannot make the host build anything.
ENV = "RENDER_DEPLOY_HOOK_URL"


def hook_url() -> str:
    """The configured deploy hook, or ``""``."""
    return config_audit.get(ENV)


def configured() -> bool:
    """True when this process can actually ask Render for a deploy."""
    return hook_url().startswith("http")


def _post(url: str, timeout: float) -> int:
    """POST the hook once. Returns the HTTP status; raises on anything else."""
    request = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(getattr(response, "status", 200) or 200)


async def trigger(timeout: float = 15.0) -> tuple[bool, str]:
    """Ask Render to build the newest commit. Never raises.

    Returns ``(accepted, sentence)`` where the sentence is safe to show a member:
    it names the status and the failure kind, and never the URL.
    """
    url = hook_url()
    if not url.startswith("http"):
        return False, (
            f"No deploy hook is configured, so nothing was deployed. Set {ENV} to a "
            "Render Deploy Hook URL and this command can ship the code itself."
        )
    try:
        status = await asyncio.to_thread(_post, url, timeout)
    except urllib.error.HTTPError as exc:
        # 401/404 is the usual answer to a hook that was rotated or deleted.
        return False, (
            f"Render refused the deploy (HTTP {exc.code}). The hook may have been "
            "rotated or deleted — copy a fresh Deploy Hook URL into the host's "
            f"{ENV}."
        )
    except Exception as exc:
        # Deliberately the exception's *name* only: its message can contain the
        # hook, which is the credential this module exists to keep quiet.
        return False, (
            f"Render could not be reached ({type(exc).__name__}). The hook URL is "
            "never printed, so check it on the host if this keeps happening."
        )
    if status >= 400:
        return False, f"Render answered HTTP {status}, so no deploy was started."
    return True, (
        f"Render accepted the deploy (HTTP {status}) and is building the newest "
        "commit."
    )
