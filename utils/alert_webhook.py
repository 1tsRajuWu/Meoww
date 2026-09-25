"""Out-of-band alerts: what the deployment says when the ordinary path cannot.

Every other way this project tells an operator something depends on the gateway.
``utils/operator_log`` posts a card into a channel, which needs a live
connection, a permission, a channel that still exists and a server the bot is
still in. The events worth waking somebody for are exactly the ones where none of
that can be assumed: the bot was **just added** to a server, so it is in none of
its channels yet; the gateway is **down**, which is why the web process is
answering in the first place; or the thing happening is a flood of requests
against the web surface, which has no gateway at all.

So this is a *webhook* — one HTTP POST to a channel nobody has to be in — and it
is deliberately the plainest transport in the codebase: no library, no
dependency, no session, no retry. Four properties, each because the alternative
is worse:

* **Never raises and never blocks.** An alert is a courtesy to the operator; it
  may not fail a request, a command, or a boot. The POST happens on a daemon
  thread, so a slow or wedged receiver cannot hold a page open.
* **Bounded.** A flood is precisely when an unbounded notifier is worst, so one
  message per kind per :data:`MIN_INTERVAL_SECONDS`, and a hard cap on what one
  process will ever send. The thing watching a flood must not be the amplifier.
* **SILENT.** Discord's ``SUPPRESS_NOTIFICATIONS`` flag, so an alert channel can
  be watched without paging anybody who is asleep. An alert is for the record
  first and for a person second.
* **Off until configured — and it says so.** An unset URL is a supported state
  (nothing is sent, nothing is broken), and the surfaces that read this module
  report it as a *missing setting* rather than as a quiet success. A silently
  disabled feature is this repository's oldest recurring complaint, and "we would
  have told you, if it had been switched on" is the version of it that costs
  somebody a night.

**What leaves the deployment**, in full, because a policy is only as good as the
field list behind it — this list is the same one ``PRIVACY.md`` section 5 and
``TERMS.md`` section 12 describe, and the two are meant to be read together:

* the **kind** of event, in one word;
* the **request address** of whoever caused it, and the **user-agent string** it
  presented, which names an operating system and a browser;
* the **path** requested, and the **coarse country** Cloudflare reports;
* the **counts** that make the event worth reading at all — how many times, how
  many members, how old an account is;
* a server's **name and id** when the event is about a server.

Nothing else. Not a message, not a member's name, not an email, not a token, not
a cookie. The receiving channel is a Discord webhook, so Discord Inc. is the
sub-processor for it in exactly the sense section 5 of the Privacy Policy means.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

from . import config_audit

#: The setting that turns this on. A webhook URL is a credential — anybody
#: holding it can post into the channel — so it is declared ``secret`` in the
#: audit and is never committed with a value.
SETTING = "ALERT_WEBHOOK_URL"

#: How often one *kind* of event may be sent. A flood produces events by the
#: thousand; the operator needs to be told that it started, not once per request.
MIN_INTERVAL_SECONDS = 30.0

#: The hard ceiling for one process, whatever the kinds are. A deployment under
#: attack for an hour should send a handful of messages, not an hour of them.
MAX_PER_PROCESS = 120

#: Short, because this is a courtesy: a webhook that has not answered in eight
#: seconds is a webhook that is down, and the event is already in the ledger.
TIMEOUT = 8.0

#: Discord's ``SUPPRESS_NOTIFICATIONS`` — what "silent" means in one integer.
SILENT = 1 << 12

#: The strip along the embed's left edge. Three states, used consistently
#: everywhere in this project: something was refused, something was noticed,
#: something merely happened.
_COLORS = {"alert": 0xE74C3C, "warn": 0xE0A44A, "info": 0x5A9FD4}

_lock = threading.Lock()
_sent_at: dict[str, float] = {}
_ledger: list[dict] = []
_totals = {"sent": 0, "failed": 0, "folded": 0, "capped": 0, "unset": 0}

#: How much of the ledger is kept in memory for the dashboard to read. Small on
#: purpose: this is "what has been sent lately", not an archive — the channel is
#: the archive.
LEDGER_KEEP = 40


def configured() -> bool:
    """Whether an alert channel has been configured at all."""
    # Read through the literal rather than through :data:`SETTING`, deliberately.
    # The audit's own guard scans the tree for a quoted setting name passed to the
    # getter, and it cannot see a name that arrives in a variable — so a setting
    # read only through this constant would be reported as one *nothing* reads, and
    # its absence would stop being mentioned at all. The constant stays for the
    # declaration, the dashboard and the tests, which is where a name is useful
    # rather than where it is invisible.
    return bool(config_audit.get("ALERT_WEBHOOK_URL"))


def send(
    kind: str,
    title: str,
    *,
    detail: str = "",
    fields: list[tuple[str, str]] | None = None,
    severity: str = "warn",
) -> dict:
    """Send one silent alert, or record why it was not sent.

    Returns the ledger entry either way, because "we decided not to send this"
    is a thing the caller — and the test suite — is entitled to see. The
    ``why`` is a sentence a person can read, which is the point: an alerting
    system that fails quietly is worse than one that is switched off, because
    the operator believes it is watching.
    """
    now = time.time()
    entry = {
        "at": now,
        "kind": (kind or "event")[:40],
        "title": title[:200],
        "detail": detail[:600],
        "severity": severity if severity in _COLORS else "warn",
        "sent": False,
        "why": "",
    }

    url = config_audit.get("ALERT_WEBHOOK_URL")
    if not url:
        entry["why"] = f"{SETTING} is not set, so there is nowhere to send this"
        with _lock:
            _totals["unset"] += 1
            _remember(entry)
        print(f"[ALERT] {entry['kind']}: not sent — {entry['why']}", flush=True)
        return entry

    with _lock:
        last = _sent_at.get(entry["kind"])
        if last is not None and (now - last) < MIN_INTERVAL_SECONDS:
            entry["why"] = (
                f"another {entry['kind']} went out {int(now - last)}s ago; "
                f"one per kind per {int(MIN_INTERVAL_SECONDS)}s"
            )
            _totals["folded"] += 1
            _remember(entry)
            return entry
        if _totals["sent"] >= MAX_PER_PROCESS:
            entry["why"] = f"this process has already sent {MAX_PER_PROCESS} alerts"
            _totals["capped"] += 1
            _remember(entry)
            return entry
        _sent_at[entry["kind"]] = now

    payload = _payload(entry, fields or [])
    entry["sent"] = True
    with _lock:
        _totals["sent"] += 1
        _remember(entry)
    threading.Thread(
        target=_post, args=(url, payload, entry), name=f"alert-{entry['kind']}", daemon=True
    ).start()
    return entry


def _remember(entry: dict) -> None:
    """Keep the newest :data:`LEDGER_KEEP` entries. Caller holds the lock."""
    _ledger.append(entry)
    del _ledger[:-LEDGER_KEEP]


def _payload(entry: dict, fields: list[tuple[str, str]]) -> dict:
    """The webhook body: an embed, a silent flag, and a name to post under."""
    embed: dict = {
        "title": entry["title"][:256],
        "color": _COLORS[entry["severity"]],
        "footer": {"text": "Meow · out-of-band alert · silent by design"},
    }
    if entry["detail"]:
        embed["description"] = entry["detail"][:4000]
    if fields:
        embed["fields"] = [
            {"name": str(name)[:256], "value": str(value)[:1024] or "—", "inline": True}
            for name, value in fields[:25]
        ]
    return {
        "username": "Meow Watch",
        # The whole point of the flag: this lands in the channel without ringing
        # anybody's phone. Somebody watching the channel sees it immediately.
        "flags": SILENT,
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }


def _post(url: str, payload: dict, entry: dict) -> None:
    """One POST, on a thread, that cannot take anything down with it."""
    try:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "MeowBot/1.0 (+https://dacat.pages.dev)",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            response.read(1)
    except Exception as exc:  # noqa: BLE001 — an alert may never break its caller
        with _lock:
            _totals["failed"] += 1
            entry["sent"] = False
            entry["why"] = f"the webhook did not answer ({type(exc).__name__})"
        print(f"[ALERT] the webhook did not answer: {type(exc).__name__}: {exc}", flush=True)


def sent(limit: int = 10) -> list[dict]:
    """The newest entries, newest first — what the dashboard shows."""
    with _lock:
        rows = list(_ledger)[-max(1, limit):]
    return rows[::-1]


def totals() -> dict:
    """Counts, so a panel can say "three sent, two folded" rather than guess."""
    with _lock:
        return dict(_totals)


def reset() -> None:
    """Forget the ledger and the rate gates. For tests, and for an operator who
    wants the next event reported again after fixing a webhook that was down."""
    with _lock:
        _sent_at.clear()
        _ledger.clear()
        for key in _totals:
            _totals[key] = 0


def state() -> dict:
    """Everything a page may read about this, with nothing that needs a request."""
    return {
        "configured": configured(),
        "setting": SETTING,
        "interval_seconds": int(MIN_INTERVAL_SECONDS),
        "cap": MAX_PER_PROCESS,
        "totals": totals(),
        "recent": sent(8),
    }


def guard_event(kind: str, event: dict, *, label: str = "") -> dict:
    """The one adapter from a recorded security event to an alert.

    Kept here rather than in the Flask hook so that the *field list* — which is
    the privacy commitment, not an implementation detail — lives in one file
    beside the paragraph that describes it. The caller decides *whether* an event
    is worth sending (:func:`utils.ai_guard.should_alert`); this decides what it
    looks like when it is.
    """
    severity = "alert" if event.get("kind") == "honeytoken" else "warn"
    titles = {
        "honeytoken": "A trap page was fetched",
        "crawler": "A declared crawler kept coming back",
    }
    fields = [
        ("Address", event.get("ip") or "not reported"),
        ("Country", event.get("country") or "unknown"),
        ("Client", (event.get("agent") or "no user-agent")[:120]),
        ("Path", event.get("path") or "/"),
        ("Times", str(event.get("hits") or 1)),
    ]
    if label:
        fields.append(("Where", label))
    return send(
        kind,
        f"🛡️ {titles.get(kind, 'A security event')}",
        detail=(
            "This is the web surface refusing an automated client. It is recorded "
            "here because it is the reading that precedes abuse: one request is a "
            "crawler, a thousand is something else."
        ),
        fields=fields,
        severity=severity,
    )
