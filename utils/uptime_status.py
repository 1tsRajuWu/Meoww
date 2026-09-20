"""Asks UptimeRobot how this deployment has actually been behaving.

The bot has always known whether it is *connected right now* — that is the
gateway line in ``/health`` and the pill on the landing page. It has never known
whether it has been connected *for the last thirty days*, which is a different
question and the one a person asking "is this thing reliable?" means. That
history already exists: UptimeRobot is already pointed at this host (its probe
is the reason ``/`` answers a monitor with one line instead of a page), and its
API can say what it has seen.

Three surfaces read this module, and each gets a different amount:

* ``/health`` — counts and one ratio. It is unauthenticated, so it names no
  monitor and no URL, exactly like ``config_audit`` and ``site_guard`` beside
  it. It is also the field a *second* monitor of the Pages site can read.
* ``/api/uptime`` — the full rows, behind the dashboard's sign-in, which is
  where monitor names and URLs belong.
* The dashboard's status panel — the same rows, rendered.

What it is careful about:

* **Nothing is fetched while a request is being answered.** The API call is a
  network round trip to a third party, and a dashboard that made it inline
  would take as long as UptimeRobot takes. A background loop refreshes a
  snapshot and every surface reads that; ``refresh()`` is the only function
  here that touches the network, and nothing in a route calls it.
* **Absent configuration is a state, not an error.** With no API key the
  snapshot says ``unconfigured`` and names the two variables to set, so the
  panel explains itself instead of showing an empty list that reads as "no
  monitors" — which is a different and much more alarming claim.
* **A failed refresh does not erase what was known.** The last good snapshot is
  kept and marked stale, because "we could not ask" and "everything is down"
  are different sentences and only one of them is true.
* **The parse is total.** UptimeRobot's payload is JSON from someone else's
  server: every field is read defensively, a monitor that arrives without a
  name or a status is skipped rather than raising, and the whole thing is
  testable without a network because :func:`check` takes its fetcher.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

#: Where the key and the optional public status page come from. The key is a
#: read-only credential (UptimeRobot -> My Settings -> API Settings -> Read-Only
#: API Key), which is why it is documented in ``.env.example`` as optional: the
#: dashboard simply says the integration is off until somebody pastes it.
ENV_KEY = "UPTIMEROBOT_API_KEY"
ENV_PAGE = "UPTIMEROBOT_STATUS_PAGE"

#: ``getMonitors`` rather than a status-page scrape: it is documented, stable,
#: and returns the ratios in the same response as the state.
API_URL = "https://api.uptimerobot.com/v2/getMonitors"

#: A monitor's own health, as UptimeRobot numbers it.
STATUS_LABELS = {
    0: "paused",
    1: "waiting for its first check",
    2: "up",
    8: "seems down",
    9: "down",
}
DOWN_CODES = (8, 9)

#: The statuses that mean UptimeRobot *answered*. Everything else is an attempt
#: that produced no reading, whatever the snapshot's ``at`` says — and ``at`` is
#: set by every attempt, including the one that never left the process, which is
#: why it cannot be aged on its own.
READ_STATUSES = ("ok", "attention", "down", "empty")

#: What the age line says when there is nothing to put an age on, by status. The
#: panel showed **"read 0 seconds ago"** directly beside "Not connected: set
#: UPTIMEROBOT_API_KEY", which is the one line that makes a reader conclude the
#: integration is working when it is not — and it was on screen the first time a
#: real deployment pasted the key.
NO_READING_TEXT = {
    "unconfigured": "not connected",
    "unreachable": "no reading",
    "refused": "no reading",
}

#: The windows asked for, in the order UptimeRobot returns them in
#: ``custom_uptime_ratio`` (a ``-`` joined string, so the order is the contract).
RATIO_WINDOWS = (1, 7, 30)

REQUEST_TIMEOUT = 15

#: Under this, a monitor is worth mentioning even when it is up: a service that
#: answers most of the time is a service somebody should look at.
SHAKY_RATIO = 99.9


def configured() -> bool:
    """Is there a key to ask with?"""
    return bool(api_key())


def api_key() -> str:
    # The name is read literally here, rather than through ``ENV_KEY``, because
    # the configuration audit finds what the code reads by looking for exactly
    # this shape in the source — and a setting it cannot see is a setting that
    # silently vanishes from the report that exists to list them all.
    return (os.environ.get("UPTIMEROBOT_API_KEY") or "").strip()


def page_url() -> str:
    """The public status page, if the operator made one, else empty.

    UptimeRobot's status pages are the one part of it a *visitor* can be sent
    to, so it is the link the dashboard offers beside the rows.
    """
    return (os.environ.get("UPTIMEROBOT_STATUS_PAGE") or "").strip()


def _number(value):
    """A float from something that may be a string, a number, or nothing."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _fetch(payload: dict) -> dict:
    """POST to UptimeRobot and return its JSON.

    Raises on anything that is not a 200 with a JSON body, because the caller
    is a background loop that reports and retries.
    """
    body = urllib.parse.urlencode(payload).encode()
    request = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "MeowBot/1.0 (+reads its own uptime monitors)",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def parse(payload: dict, *, now: int | None = None) -> dict:
    """Turn UptimeRobot's answer into the snapshot every surface reads.

    Total by construction: a payload with no monitors, a monitor with no name,
    a status that is not one of the declared codes and a ratio string with fewer
    parts than were asked for are all handled here rather than at the three call
    sites that would each have to remember to.
    """
    moment = int(time.time() if now is None else now)
    rows = []
    for raw in (payload or {}).get("monitors") or []:
        if not isinstance(raw, dict):
            continue
        code = raw.get("status")
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        ratios = {}
        parts = str(raw.get("custom_uptime_ratio") or "").split("-")
        for window, part in zip(RATIO_WINDOWS, parts):
            ratios[f"{window}d"] = _number(part)
        if not ratios:
            single = _number(raw.get("uptime_ratio") or raw.get("all_time_uptime_ratio"))
            if single is not None:
                ratios["30d"] = single
        rows.append({
            "id": str(raw.get("id") or ""),
            "name": str(raw.get("friendly_name") or "").strip() or "Unnamed monitor",
            "url": str(raw.get("url") or "").strip(),
            "status": code,
            "label": STATUS_LABELS.get(code, "unknown"),
            "up": code == 2,
            "down": code in DOWN_CODES,
            "paused": code == 0,
            "ratios": {k: v for k, v in ratios.items() if v is not None},
            "response_ms": _number(raw.get("average_response_time")),
        })

    down = [row for row in rows if row["down"]]
    paused = [row for row in rows if row["paused"]]
    waiting = [row for row in rows if row["status"] in (None, 1)]
    weakest = None
    for row in rows:
        value = row["ratios"].get("30d")
        if value is None or row["paused"]:
            continue
        weakest = value if weakest is None else min(weakest, value)

    if not rows:
        status = "empty"
        detail = "UptimeRobot answered, but this account has no monitors in it yet."
    elif down:
        status = "down"
        detail = (
            "UptimeRobot is reporting "
            + ", ".join(f"{row['name']} ({row['label']})" for row in down)
            + "."
        )
    elif waiting:
        status = "attention"
        detail = (
            f"{len(waiting)} monitor{'s' if len(waiting) != 1 else ''} have not been "
            "checked yet — a new monitor is quiet until UptimeRobot's first pass."
        )
    elif paused:
        status = "attention"
        detail = (
            f"{len(paused)} monitor{'s' if len(paused) != 1 else ''} are paused, so "
            "nothing is watching that address."
        )
    elif weakest is not None and weakest < SHAKY_RATIO:
        status = "attention"
        detail = f"Everything answers, and the weakest 30-day uptime is {weakest:.2f}%."
    else:
        status = "ok"
        detail = "Every monitor answered through its last check."
        if weakest is not None:
            detail = f"Every monitor answered; the weakest 30-day uptime is {weakest:.2f}%."

    return {
        "status": status,
        "at": moment,
        "stale": False,
        "page": page_url(),
        "monitors": rows,
        "total": len(rows),
        "up": len(rows) - len(down) - len(paused) - len(waiting),
        "down": len(down),
        "paused": len(paused),
        "waiting": len(waiting),
        "ratio_30d": weakest,
        "detail": detail,
    }


def check(*, fetch=None, now: int | None = None) -> dict:
    """One refresh: ask UptimeRobot, or say why it could not be asked.

    ``fetch`` is injected by the tests so every branch above is exercised with
    no network, exactly as ``site_guard.check`` does it.
    """
    moment = int(time.time() if now is None else now)
    if not configured():
        return {
            "status": "unconfigured",
            "at": moment,
            "stale": False,
            "page": page_url(),
            "monitors": [],
            "total": 0,
            "up": 0,
            "down": 0,
            "paused": 0,
            "waiting": 0,
            "ratio_30d": None,
            "detail": (
                f"Not connected: set {ENV_KEY} to the read-only key from "
                "UptimeRobot → My Settings → API Settings, and this panel fills "
                "itself in. Creating a public status page there and setting "
                f"{ENV_PAGE} also adds a link for people who are not signed in."
            ),
        }
    do_fetch = fetch or _fetch
    payload = {
        "api_key": api_key(),
        "format": "json",
        "custom_uptime_ratios": "-".join(str(w) for w in RATIO_WINDOWS),
        "logs": "0",
        "response_times": "0",
    }
    try:
        answer = do_fetch(payload)
    except Exception as exc:  # the loop reports and retries; the page never waits
        return {
            "status": "unreachable",
            "at": moment,
            "stale": False,
            "page": page_url(),
            "monitors": [],
            "total": 0,
            "up": 0,
            "down": 0,
            "paused": 0,
            "waiting": 0,
            "ratio_30d": None,
            "detail": f"UptimeRobot could not be reached: {type(exc).__name__}: {exc}",
        }
    if not isinstance(answer, dict) or str(answer.get("stat") or "").lower() != "ok":
        reason = ""
        if isinstance(answer, dict):
            error = answer.get("error")
            if isinstance(error, dict):
                reason = str(error.get("message") or "").strip()
        return {
            "status": "refused",
            "at": moment,
            "stale": False,
            "page": page_url(),
            "monitors": [],
            "total": 0,
            "up": 0,
            "down": 0,
            "paused": 0,
            "waiting": 0,
            "ratio_30d": None,
            "detail": (
                "UptimeRobot refused the request"
                + (f": {reason}" if reason else ".")
                + " The usual cause is a key without read access, or one that was "
                "rotated in the dashboard and not on the host."
            ),
        }
    return parse(answer, now=moment)


#: The last snapshot, the status before it — so a *change* can be told from a
#: standing condition — and whether that change has been said yet. One dict
#: rather than three globals, because the tests reset it in one place.
_state: dict = {"last": None, "previous": None, "announced": False}


def record(snapshot: dict) -> dict | None:
    """Remember this snapshot and return the status it replaced.

    The replaced status is what makes "it just went down" sayable: a monitor
    that is down and was down is a standing condition, and one that is down and
    was up is news.

    A refresh that could not *ask* keeps the rows it already had, marked stale.
    The status is the failure's — nothing here claims UptimeRobot said "up" when
    it said nothing — but the panel is not emptied, because an empty list reads
    as "you have no monitors", which is a different and much more alarming
    claim than "here is the last reading, and I could not ask again".
    """
    previous = _state["last"]
    current = snapshot
    if (
        snapshot.get("status") in ("unreachable", "refused")
        and previous
        and previous.get("monitors")
    ):
        current = dict(snapshot)
        # ``at`` travels with the rows. Keeping the readings and stamping them
        # with the time of the *attempt* that failed aged somebody else's data:
        # the panel said "read just now" about a reading it was, in the same
        # breath, calling "the last reading, taken 20 minutes ago".
        for key in (
            "at",
            "monitors",
            "total",
            "up",
            "down",
            "paused",
            "waiting",
            "ratio_30d",
        ):
            current[key] = previous[key]
        current["stale"] = True
        # The same worder the age line uses, not raw seconds: this sentence sits
        # directly under that line, and "taken 1200 seconds ago" beside "read 20
        # minutes ago" is two spellings of one fact — which is how the panel
        # said "read 1789848173 seconds ago" before this was fixed there.
        current["detail"] = (
            f"{snapshot['detail']} Showing the last reading, taken "
            + age_text(max(0, int(time.time()) - int(previous["at"] or 0))).removeprefix("read ")
            + "."
        )
    _state["previous"] = previous
    _state["last"] = current
    # A fresh reading has news in it until :func:`headline` says so. Without
    # this, asking twice about the same transition — a manual refresh beside
    # the loop's own pass — would send the same card twice.
    _state["announced"] = False
    return (previous or {}).get("status")


def age_text(seconds: int | None) -> str:
    """How stale a reading is, in words.

    The age cannot be stored with the snapshot, because it is a fact about *now*
    rather than about the answer — an ``at`` timestamp carried to the page and
    rendered as an age is how a panel says "read 1789848173 seconds ago". So it
    is computed where it is read. Words rather than a number: this is the line
    beside a heading, not a measurement anybody is going to do arithmetic on.
    """
    if seconds is None:
        return "not read yet in this process"
    # Singular for one, in every unit: "read 1 minutes ago" is the kind of line
    # that makes a reader trust everything else on the page a little less, and it
    # was on screen the first time this was driven in a browser.
    if seconds < 90:
        count = int(seconds)
        return f"read {count} second{'' if count == 1 else 's'} ago"
    if seconds < 5400:
        minutes = max(1, int(seconds) // 60)
        return f"read {minutes} minute{'' if minutes == 1 else 's'} ago"
    hours = max(1, int(seconds) // 3600)
    return f"read {hours} hour{'' if hours == 1 else 's'} ago"


def _has_reading(current: dict) -> bool:
    """Is there a reading whose age can be told?

    Two ways to have one, and the difference matters. Rows exist when a reading
    answered *or* when one is being kept through a failed refresh — in both cases
    ``at`` is when those rows were read, because :func:`record` moves it with
    them. A status in :data:`READ_STATUSES` with no rows is also a reading, of an
    account with no monitors in it. What is left is an attempt that produced
    nothing, and its ``at`` is the time of the attempt, not of any reading.
    """
    return bool(current.get("monitors")) or current.get("status") in READ_STATUSES


def snapshot() -> dict:
    """The last snapshot, or an honest stand-in before the first refresh.

    A copy, with the age of the reading worked out at call time. The caller gets
    the same rows every surface reads, plus two facts that belong to the moment
    of asking rather than to the answer: ``age_seconds`` and its wording.

    The wording is about **the reading on screen**, not about the last attempt.
    With nothing to age — no key, a refusal, a timeout on the very first pass —
    it says so instead of counting up from a clock the API never touched.
    """
    current = _state["last"]
    if current is None:
        if not configured():
            # No key is a definite answer and needs no network to give it.
            current = check()
        else:
            current = _unread_placeholder()
    out = dict(current)
    age = None
    if current.get("at"):
        age = max(0, int(time.time()) - int(current["at"]))
    if _has_reading(current):
        out["age_seconds"] = age
        out["age_text"] = age_text(age)
    else:
        out["age_seconds"] = None
        out["age_text"] = NO_READING_TEXT.get(
            str(current.get("status") or ""), age_text(None)
        )
    return out


def _unread_placeholder() -> dict:
    """What is known before this process has asked anything."""
    return {
        "status": "unknown",
        "at": None,
        "stale": False,
        "page": page_url(),
        "monitors": [],
        "total": 0,
        "up": 0,
        "down": 0,
        "paused": 0,
        "waiting": 0,
        "ratio_30d": None,
        "detail": "Not checked yet in this process — the first pass runs shortly after boot.",
    }


def reset() -> None:
    """Forget everything remembered. For tests, and after a redeploy."""
    _state["last"] = None
    _state["previous"] = None
    _state["announced"] = False


def health_json() -> dict:
    """The aggregate for ``/health``: counts and one ratio, and no names.

    ``/health`` is unauthenticated. A monitor's name is usually the host it
    watches — which is not a secret, but the rule this endpoint already follows
    for ``config`` and ``site_guard`` is that it publishes *counts*, and a
    second endpoint exists for the rows.
    """
    current = snapshot()
    out = {
        "status": current["status"],
        "monitors": current["total"],
        "up": current["up"],
        "down": current["down"],
    }
    if current["ratio_30d"] is not None:
        out["uptime_30d"] = round(current["ratio_30d"], 3)
    # Only when there is a reading to age. On an unconfigured deployment this
    # used to publish `checked_seconds_ago: 0` — an unauthenticated endpoint
    # saying a check happened seconds ago when nothing had been asked at all.
    if current["at"] and _has_reading(current):
        out["checked_seconds_ago"] = max(0, int(time.time()) - int(current["at"]))
    if current["page"]:
        out["page"] = current["page"]
    return out


def headline(snapshot_value: dict) -> str | None:
    """One sentence of news about a *change*, or ``None`` when nothing changed.

    Two transitions are worth a message: everything answering and then something
    not, and the reverse. Everything else is silence — including the very first
    reading, which is a *baseline* rather than news, and a monitor that has been
    down for a week, which is a standing condition. A card every five minutes
    about the same outage is how an alert channel gets muted, and losing the
    audience is what costs the one card that mattered its reader.

    It reads :func:`record`'s memory rather than being told, so a caller cannot
    forget to ask "and what was it before?" — and it *spends* that memory, so
    the transition is announced exactly once no matter how many callers ask
    about the same reading. A standing condition is silence either way.
    """
    if _state.get("announced"):
        return None
    _state["announced"] = True
    before = _state.get("previous")
    if not before:
        return None
    was = before.get("status")
    now = snapshot_value.get("status")
    if now == "down" and was != "down":
        names = ", ".join(
            f"{row['name']} ({row['label']})"
            for row in snapshot_value.get("monitors") or []
            if row.get("down")
        )
        return f"UptimeRobot is reporting {names or 'a monitor'} down."
    if now == "ok" and was == "down":
        return "UptimeRobot reports everything answering again."
    return None
