"""Who may read these pages, and who may not teach a model on them.

The framework this module implements is blunt about why an AI-facing site needs
more than a policy: **``robots.txt`` is a request, and the collectors with the
most to gain are the ones least likely to honour it.** A training crawler that
ignores the file costs this deployment bandwidth, reads pages that exist for
people, and — the part that actually matters — takes text without any agreement
ever being formed. So the posture here has three layers, and each one exists
because the one before it can be ignored:

* **The directive.** :func:`robots_txt` and :func:`ai_txt` say it plainly, per
  crawler rather than in a wildcard, because a named token is what an operator
  can point at later. ``ai.txt`` carries the training prohibition; the same
  prohibition is a clause in ``TERMS.md``, which is what makes ignoring it a
  breach rather than a preference.
* **The refusal.** :func:`headers` puts the reservation on every response, and
  :func:`crawler` recognises the agents that have already declared themselves:
  a named AI crawler asking for a page here is answered **403** and told which
  document it is choosing to ignore, which is both the cheapest possible answer
  and the one that reads well in a log.
* **The trap.** :func:`trap_anchor` is a link no human can reach and
  :data:`HONEYTOKENS` the paths behind it: disallowed in ``robots.txt``,
  absent from the sitemap, never navigable, and served with a body that is
  deliberately *not* worth collecting. Nothing legitimate ever fetches one, so a
  fetch is not a heuristic — it is a positive identification, recorded with the
  address, the agent and the country of origin, which is the evidence an
  operator needs to block the network and the sentence a takedown or a complaint
  is built on.

Two rules the code keeps, and they are the ones worth checking rather than
trusting:

1. **Nothing here reads a decision out of a request.** The agent is matched
   against a fixed table of published tokens; a path is a trap when it is one of
   the declared paths. There is no regex the client can steer, no header that
   turns the guard off, and no path where a *human* is refused — the refusal set
   is the tokens below and nothing else. A visitor with no User-Agent at all is
   served normally, because "no UA" includes every command-line user of the
   public API and that is not the same population as a training crawler.
2. **A failure to record is not a failure to answer.** Every database call is
   contained: if the ledger cannot be written (read-only disk, a restore racing
   this table), the refusal still happens, the trap still answers, and the guard
   says so in memory instead. Losing the evidence is bad; failing open is worse.

The evidence is kept for :data:`RECORD_KEEP_DAYS` and pruned on write, the same
shape as every other table here: it is a record, not a hoard.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
import time
from collections import deque

from utils import db

#: Every agent this repository refuses, by the token each one publishes in its
#: ``User-Agent``. Named individually rather than matched by a pattern: a token
#: list can be read, argued with and added to, and the two mistakes that matter
#: are opposite — refusing a browser (a person is locked out of a page they were
#: invited to read) and missing a crawler. Only long, distinctive, published
#: tokens are here, which is why ``Googlebot``, ``bingbot`` and ``PetalBot`` are
#: absent: they are search, and refusing search would take this site out of the
#: one index that brings people to it.
AI_CRAWLERS: tuple[tuple[str, str], ...] = (
    ("GPTBot", "OpenAI's training crawler"),
    ("OAI-SearchBot", "OpenAI's answer index"),
    ("ChatGPT-User", "a fetch made on behalf of a ChatGPT reader"),
    ("Google-Extended", "Gemini's training opt-in, separate from Googlebot"),
    ("Applebot-Extended", "Apple Intelligence's training opt-in"),
    ("ClaudeBot", "Anthropic's training crawler"),
    ("Claude-Web", "Anthropic's earlier reader"),
    ("anthropic-ai", "Anthropic's earlier crawler"),
    ("PerplexityBot", "Perplexity's index"),
    ("Perplexity-User", "a fetch made for a Perplexity reader"),
    ("Bytespider", "ByteDance's crawler"),
    ("CCBot", "Common Crawl, which most models are trained from"),
    ("Meta-ExternalAgent", "Meta's crawler"),
    ("meta-externalagent", "Meta's crawler (lowercase spelling)"),
    ("FacebookBot", "Meta's older crawler"),
    ("Amazonbot", "Amazon's crawler, used for answer features"),
    ("Applebot", "Apple's index crawler, where training is not opted out"),
    ("cohere-ai", "Cohere's crawler"),
    ("Diffbot", "Diffbot, which sells extracted page data"),
    ("ImagesiftBot", "an image dataset collector"),
    ("YouBot", "You.com's crawler"),
    ("Timpibot", "a data-broker crawler"),
    ("Omgilibot", "a data-broker crawler"),
    ("Webzio-Extended", "Webz.io, which resells page data"),
    ("omgili", "a data-broker crawler"),
    ("Scrapy", "a scraping framework, not a search crawler"),
    ("puppeteer", "a headless browser, not a visitor"),
    ("Playwright", "a headless browser, not a visitor"),
    ("PhantomJS", "a headless browser, not a visitor"),
    ("HeadlessChrome", "a headless browser, not a visitor"),
)

#: One compiled pattern for the whole table, boundary-anchored on purpose: a
#: substring match would refuse ``GPTBotSimulator`` (somebody's own test) and,
#: worse, a page's own title echoed back inside a UA string. Case-insensitive
#: because half of these tokens are published in more than one case.
_CRAWLER_PATTERN = re.compile(
    r"(?:^|[^a-z0-9])(" + "|".join(re.escape(token) for token, _ in AI_CRAWLERS) + r")(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)

#: What a refusal says. Plain text, no HTML, no card: the reader is a program,
#: and the only thing it can usefully be told is which document it is ignoring.
REFUSAL = (
    "403 — this host does not permit automated collection, and this agent is a "
    "published AI crawler.\n\n"
    "The prohibition is a term of use, not a preference: see {terms} and {ai}.\n"
    "Training, fine-tuning and dataset use are reserved against; the machine-\n"
    "readable statement of that is {ai_txt}, and the reservation is repeated on\n"
    "every response as Content-Usage, tdm-reservation and X-Robots-Tag.\n\n"
    "If this is a mistake — a real visitor's browser, or an integration with a "
    "written agreement — say so at {support} and the token will be removed.\n"
)

#: Paths no human can reach: disallowed in ``robots.txt``, never in the sitemap,
#: never linked from anything a visitor can click, and answered with the decoy
#: below. Each one is a JSON document at a plausible address, because the trap
#: has to look worth fetching to the thing that does not read the directives.
HONEYTOKENS: tuple[tuple[str, str], ...] = (
    ("/api/_index/entities.json", "a page index that is never built"),
    ("/_corpus/training.jsonl", "a corpus export that does not exist"),
)

#: Paths a listed crawler may still read. Both directive files have to be
#: readable by the agents they govern — a crawler that cannot fetch the rule it
#: is breaking has an argument, and refusing the policy would be an odd way to
#: enforce it. Nothing else is exempt.
DIRECTIVE_PATHS = ("/robots.txt", "/ai.txt", "/.well-known/ai.txt")

#: How long an event is kept. Long enough to be evidence in a complaint that
#: takes a month to draft, short enough that this table stays a record rather
#: than an archive.
RECORD_KEEP_DAYS = 180

#: How long a repeat of the same (agent, path, address) folds into one row. A
#: crawler that asks four hundred times should be one line with a count, not
#: four hundred lines, or the evidence becomes unreadable exactly when somebody
#: needs to read it.
FOLD_SECONDS = 600.0

#: How many refusals by one agent, inside the fold window, before the operator is
#: told. One refused request is a crawler finding out; twenty is a crawler
#: choosing to carry on, and that is the moment a decision has to be made about
#: its network.
FLOOD_THRESHOLD = 20

#: At most one alert per agent per window. A watch that sends a card per event
#: is a watch nobody reads by the second day.
ALERT_COOLDOWN_SECONDS = 3600.0

#: What ``/health`` may say about this table without naming anybody. The endpoint
#: is unauthenticated by policy, so counts and dates only — never an address, an
#: agent or a path.
SUMMARY_CACHE_SECONDS = 60.0

TABLE = """CREATE TABLE IF NOT EXISTS net_guard_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    hits INTEGER NOT NULL DEFAULT 1
)"""

INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_net_guard_at ON net_guard_events(at)"
)

_ensure_lock = threading.Lock()
#: Which database has been ensured, rather than *whether* one ever was: a process
#: that swaps databases under itself (a restore, or a test pointing at a throwaway
#: file) must not keep believing this table exists in the new one.
_ensured_for: str | None = None
_write_lock = threading.Lock()
_write_failed_at = 0.0
#: The last events this process saw, newest last. Bounded, because it exists to
#: be shown and to decide about alerts, not to be the ledger — the table is that.
_recent: deque[dict] = deque(maxlen=50)
_alerted: dict[str, float] = {}
_refused_since_alert: dict[str, int] = {}
_summary: tuple[float, dict] | None = None


# --------------------------------------------------------------------------- #
# Deciding
# --------------------------------------------------------------------------- #
def crawler(agent: str) -> str | None:
    """The token this ``User-Agent`` publishes, or ``None`` for everyone else.

    Matching only ever *adds* a refusal for a declared crawler: a browser, an
    uptime monitor, ``curl`` from a laptop and a reader with no ``User-Agent``
    at all are all ``None``, which is what keeps this guard from being the thing
    that breaks a page.
    """
    if not agent:
        return None
    match = _CRAWLER_PATTERN.search(agent[:400])
    return match.group(1) if match else None


def trap(path: str) -> str | None:
    """The label of the honeytoken at ``path``, or ``None``.

    Compared exactly, after the query string is dropped: ``…/entities.json?x=1``
    is the same trap as ``…/entities.json``, and a crawler adding a cache-buster
    is not a reason to miss it.
    """
    candidate = (path or "").split("?", 1)[0].rstrip("/")
    for declared, label in HONEYTOKENS:
        if candidate == declared.rstrip("/"):
            return label
    return None


def refuses(path: str) -> bool:
    """Whether a listed crawler is refused here. Only the directives are exempt."""
    candidate = (path or "").split("?", 1)[0]
    return candidate not in DIRECTIVE_PATHS


def decoy(path: str) -> str:
    """What a trap answers with: a document that is useless on purpose.

    Not a real payload and not a poisoned one. Poisoning invites an argument
    about who deceived whom, and the point of the trap is to be *unarguable* —
    so it is honest JSON that says what it is, with the two clauses a collector
    is declining to read. It stays small because a crawler that keeps asking
    should cost this deployment nothing.
    """
    return json.dumps(
        {
            "notice": (
                "This path is not part of the site. It exists to identify "
                "automated collection that ignores robots.txt."
            ),
            "policy": {
                "terms": f"{policy_url()}/terms",
                "ai": f"{policy_url()}/ai",
                "ai_txt": f"{policy_url()}/ai.txt",
            },
            "reservation": "Training, fine-tuning and dataset use are reserved against.",
            "dataset": [],
            "generated_by": "utils/ai_guard.py",
            "request": {"path": path},
        },
        indent=2,
    )


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #
def policy_url() -> str:
    """Where the published policies live, from the setting the site guard reads."""
    from utils import hub  # noqa: PLC0415 — one reader for this address, no cycle

    return hub.marketing_url()


def refusal_body() -> str:
    """The 403's text, with the addresses filled in from the live settings."""
    base = policy_url()
    return REFUSAL.format(
        terms=f"{base}/terms",
        ai=f"{base}/ai",
        ai_txt=f"{base}/ai.txt",
        support=f"{base}/support",
    )


#: The reservation as plain constants, for the hosts that cannot resolve the
#: policy's own address at import time (``utils/hub.py`` builds its static-host
#: header list while the module is still importing). :func:`headers` is the same
#: list plus the ``tdm-policy`` URL, so there is one place these values are
#: written and two ways to read them — which is what stops the static host and
#: the app disagreeing about what this site permits.
TRAIN_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Usage", "train-ai=n"),
    ("tdm-reservation", "1"),
    ("X-Robots-Tag", "noai, noimageai"),
)


def headers(*, private: bool = False) -> dict[str, str]:
    """The reservation, repeated on every response.

    Three spellings of one sentence, because there are three ways a program
    might look for it and none of them is universal yet: ``Content-Usage`` (the
    IETF ``aipref`` draft), ``tdm-reservation`` (the W3C TDM Reservation
    Protocol, which is the one with a patent-pool-free reading), and
    ``X-Robots-Tag`` (the half of the robots standard that travels in a header,
    so it reaches a PDF, an image and an API answer as readily as a page).

    ``private`` is for the surfaces behind a sign-in: those additionally carry
    ``noindex``, because a page that is only reachable by signing in has no
    business in an index at all — and because an authenticated page is the one a
    court asks about when a collector's defence is that it read only what was
    public. ``noai``/``noimageai`` are not registered tokens; they are what
    crawler authors actually implemented, and an unknown token in this header is
    ignored by everyone else.
    """
    values = dict(TRAIN_HEADERS)
    values["tdm-policy"] = f"{policy_url()}/ai"
    if private:
        values["X-Robots-Tag"] = "noindex, nofollow, noai, noimageai"
    return values


def robots_txt(*, sitemap: str = "", private: bool = False) -> str:
    """The directives, built rather than written by hand at each host.

    ``private`` is for a host that serves nothing but a signed-in console: on
    those the wildcard section is ``Disallow: /`` as well, because there is
    nothing there for a visitor to find and an index entry for a login page is
    only ever useful to somebody assembling a list of them. The named-crawler
    section and the trap lines are written either way — on a private host they
    are redundant *and* explicit, which is the correct combination for a host
    whose whole config is one redirect.

    Every deployment of this project has its own address (the Render service,
    Cloudflare Pages, the hub's own pages, the owner console) and each one used
    to write its own ``robots.txt`` — two of them ``Allow: /`` and nothing else,
    which is how the anti-AI posture would have been true on one host and absent
    on the next with nothing to show for it. One function, called by the app and
    by all three static builds, is the only way that cannot happen.

    Order matters in this file, and the order here is deliberate: the named
    crawlers come **before** the wildcard, because a crawler reads the most
    specific rule that names it — a ``Disallow`` written after ``User-agent: *``
    would be read by nobody.
    """
    lines: list[str] = []
    lines.append("# Automated collection and AI training are not permitted on this host.")
    lines.append(f"# The policy: {policy_url()}/ai — machine-readable: {policy_url()}/ai.txt")
    lines.append("# Named crawlers are listed so that ignoring this file is a choice,")
    lines.append("# not an oversight, and so the operator can point at which one did it.")
    lines.append("")
    for token, label in AI_CRAWLERS:
        lines.append(f"# {label}")
        lines.append(f"User-agent: {token}")
        lines.append("Disallow: /")
        lines.append("")
    if private:
        lines.append("# This host serves the operator's console and nothing else.")
        lines.append("User-agent: *")
        lines.append("Disallow: /")
        lines.append("")
    else:
        lines.append("# Everyone else: people, search engines, link previews.")
        lines.append("User-agent: *")
        lines.append("Allow: /")
        lines.append("")
        lines.append("# Sign-in, and everything behind it.")
        for path in ("/dashboard", "/servers", "/owner", "/login", "/logout", "/api/"):
            lines.append(f"Disallow: {path}")
        lines.append("")
    lines.append("# Traps: no page links a human to these, and nothing legitimate reads them.")
    for path, _label in HONEYTOKENS:
        lines.append(f"Disallow: {path}")
    lines.append("")
    if sitemap:
        lines.append(f"Sitemap: {sitemap.rstrip('/')}")
        lines.append("")
    return "\n".join(lines)


def ai_txt() -> str:
    """The machine-readable half: this host's answer to "may I train on you".

    ``ai.txt`` is a convention rather than a standard, which is exactly why the
    prohibition is also a clause in ``TERMS.md`` and a header on every response —
    a convention cannot be relied on alone, and a contract can. The format is the
    one the convention settled on: ``User-agent``/``Disallow`` pairs, where
    ``Disallow`` means *do not use this for training*.
    """
    base = f"{policy_url()}"
    lines = [
        "# ai.txt — this host's AI-use policy.",
        f"# Human-readable: {base}/ai   Terms: {base}/terms   Privacy: {base}/privacy",
        "# 'Disallow' here means: do not use this content for AI training,",
        "# fine-tuning, embedding or dataset construction. It is not about crawling;",
        "# see /robots.txt for that. Both are terms of use, not preferences.",
        "",
        "User-agent: *",
        "Disallow: /",
        "",
        "# Reserved rights, in the form the W3C TDM Reservation Protocol and the",
        "# IETF aipref draft use. Both travel on every response as headers.",
        "tdm-reservation: 1",
        "content-usage: train-ai=n",
        "",
        f"# Anything not expressly permitted is reserved. Ask first: {base}/support",
        "# Doing this without permission is a breach of the Terms of Service, and",
        "# a claim for the extraction rather than a request to stop.",
    ]
    return "\n".join(lines) + "\n"


def trap_anchor() -> str:
    """The link a person cannot reach and a collector cannot miss.

    Off-screen rather than ``display: none``, because a crawler that skips
    invisible elements is a crawler that reads CSS — and the whole population
    this exists for is the one that does not. Three attributes keep it away from
    people rather than merely out of sight: ``aria-hidden`` keeps it out of the
    accessibility tree, ``tabindex="-1"`` keeps it out of the tab order, and the
    text says what it is, so the one reader who does arrive by keyboard or
    screen reader is told the truth instead of being shown a decoy. ``nofollow``
    is deliberately **not** set: a link search engines are told to skip is a link
    the non-compliant collectors still follow, and there is nothing to lose.
    """
    path, label = HONEYTOKENS[0]
    return (
        '<a class="trap-link" href="' + html.escape(path, quote=True) + '" '
        'tabindex="-1" aria-hidden="true">' + html.escape(label) + "</a>"
    )


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    """Create the table once per process, if it is not already there.

    Declared in ``utils/db.setup`` as well, like every other table here: a
    database restored from a backup taken before this feature existed must start
    with an empty ledger rather than failing every request that touches it.
    """
    global _ensured_for
    path = str(getattr(db, "DB_PATH", ""))
    if _ensured_for == path:
        return
    with _ensure_lock:
        if _ensured_for == path:
            return
        conn.execute(TABLE)
        conn.execute(INDEX)
        conn.commit()
        _ensured_for = path


def record(kind: str, *, agent: str = "", path: str = "", ip: str = "", country: str = "") -> dict:
    """Write one event down, and answer with what was written.

    A repeat inside :data:`FOLD_SECONDS` folds into the row it repeats: the
    count is the interesting number (one collection or four hundred), and a
    ledger where the same crawler is four hundred consecutive rows is a ledger
    nobody can read. The returned mapping is what the alert and ``/health``
    use — always, whether or not the write landed, so a host with a read-only
    database still refuses the request and still says what it saw.
    """
    global _write_failed_at
    now = time.time()
    event = {
        "at": now,
        "kind": kind,
        "agent": (agent or "")[:120],
        "path": (path or "")[:200],
        "ip": (ip or "")[:64],
        "country": (country or "")[:8],
        "hits": 1,
    }
    _recent.append(event)
    try:
        conn = _connect()
        _ensure(conn)
        with _write_lock:
            row = conn.execute(
                "SELECT id, hits FROM net_guard_events WHERE kind = ? AND agent = ? "
                "AND path = ? AND ip = ? AND at > ? ORDER BY id DESC LIMIT 1",
                (kind, event["agent"], event["path"], event["ip"], now - FOLD_SECONDS),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE net_guard_events SET hits = hits + 1, at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                event["hits"] = int(row["hits"]) + 1
            else:
                conn.execute(
                    "INSERT INTO net_guard_events (at, kind, agent, path, ip, country, hits) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (now, kind, event["agent"], event["path"], event["ip"], event["country"]),
                )
            conn.execute(
                "DELETE FROM net_guard_events WHERE at < ?",
                (now - RECORD_KEEP_DAYS * 86400,),
            )
            conn.commit()
            conn.close()
        _write_failed_at = 0.0
    except sqlite3.Error as exc:
        # Once a minute, not once a request: the failure is a fact about the
        # host, and repeating it per request turns a quiet problem into a log
        # storm on the machine that has the least room for one.
        if now - _write_failed_at > 60.0:
            _write_failed_at = now
            print(f"[GUARD] could not write to the event ledger: {exc}", flush=True)
    return event


def should_alert(event: dict) -> bool:
    """Whether this event is worth waking the operator for.

    Two cases, and both are deliberate: a **trap** was fetched, which no
    legitimate agent can do, or a listed crawler has been refused
    :data:`FLOOD_THRESHOLD` times inside the fold window, which is a decision to
    carry on rather than a crawl that wandered in. Refusals below that are
    counted and shown on ``/health`` and never announced — the fastest way to
    make an operator stop reading alerts is to send one for every event.
    """
    agent = event.get("agent") or "unknown"
    if event.get("kind") == "honeytoken":
        return _cooldown_passed(agent, event.get("at") or time.time())
    if event.get("kind") != "crawler":
        return False
    _refused_since_alert[agent] = _refused_since_alert.get(agent, 0) + 1
    if _refused_since_alert[agent] < FLOOD_THRESHOLD:
        return False
    if not _cooldown_passed(agent, event.get("at") or time.time()):
        return False
    _refused_since_alert[agent] = 0
    return True


def _cooldown_passed(agent: str, now: float) -> bool:
    last = _alerted.get(agent, 0.0)
    if now - last < ALERT_COOLDOWN_SECONDS:
        return False
    _alerted[agent] = now
    return True


def recent(limit: int = 10) -> list[dict]:
    """The newest events this process saw — for the alert, and for ``/health``."""
    return list(_recent)[-limit:][::-1]


def totals(days: int = 30) -> dict:
    """Counts for ``/health``, cached: an endpoint that is polled costs a query.

    Counts and a date only. This endpoint is public by policy, so it may not
    carry an address, an agent or a path — the ledger is for the operator, and
    the operator reads it through the console, not through a status page.
    """
    global _summary
    now = time.time()
    if _summary is not None and now - _summary[0] < SUMMARY_CACHE_SECONDS:
        return _summary[1]
    data = {"refused": 0, "traps": 0, "last": None, "window_days": days}
    try:
        conn = _connect()
        _ensure(conn)
        since = now - days * 86400
        for row in conn.execute(
            "SELECT kind, SUM(hits) AS n, MAX(at) AS last FROM net_guard_events "
            "WHERE at >= ? GROUP BY kind",
            (since,),
        ).fetchall():
            if row["kind"] == "honeytoken":
                data["traps"] = int(row["n"] or 0)
            else:
                data["refused"] = int(row["n"] or 0)
            if row["last"]:
                data["last"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["last"]))
        conn.close()
    except sqlite3.Error:
        data["ledger"] = "unreadable"
    _summary = (now, data)
    return data


def note(kind: str, *, agent: str = "", path: str = "", ip: str = "", country: str = "") -> dict:
    """Record an event and decide, in one call, whether it is an alert.

    The Flask hook wants exactly this and nothing else: one call that writes,
    folds, counts and answers "is this the moment the operator hears about it".
    """
    event = record(kind, agent=agent, path=path, ip=ip, country=country)
    event["alert"] = should_alert(event)
    return event
