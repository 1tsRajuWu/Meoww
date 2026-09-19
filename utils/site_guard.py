"""Asks the public website, on a timer, whether it is serving the repository.

On 2026-09-19 this project's Pages deployment was publishing the **checkout**.
The Pages project had a Git integration and no build command and no output
directory, and a static host handed no output directory publishes the directory
it is given. Its own build log said so — *No build command specified. Skipping
build step.*, then *Uploading... (185/185)* — and the repository has exactly 185
tracked files. For hours, ``/bot.py``, ``/utils/cloud_sync.py``, ``/cogs/``,
``/render.yaml`` and ``/.env.example`` were readable by anyone at the site's own
address. No *value* leaked, because every secret is read from the host's
environment and none is committed; the source tree itself was the incident.

That was fixed, and fixing it is not what this module is for. This is for the
part that fix cannot cover: **nothing would have noticed a second time.** The
failure has no symptom in the product — every page looked correct, the Discord
bot stayed connected, and the one signal was a line in a host log nobody reads.
The deploy that caused it reported **success**.

So the check runs here, inside the bot process, for two deliberate reasons:

* **This process is always up.** CI is not. At the moment this was written the
  ``Tests`` and ``Site`` workflows had been failing for an hour and a half for a
  billing reason — *"The job was not started because recent account payments
  have failed"* — with no test ever running, so a check that lives in CI is a
  check that can be switched off by a payment failure. A guard against a
  publishing mistake must not share a failure mode with publishing.
* **It can say so where a person is.** The bot is already in the operator's
  server with a ``#bot-error`` channel bound, so an alert reaches Discord
  without any new service, token or dashboard to configure.

What it is careful about, and why each line is the shape it is:

* **Every probe is cache-busted, and this is not a detail.** The first live run
  of this module reported the exposure as still open. It was not: the live
  deployment was 14 files and clean, while seven paths still answered ``200``
  with 71 KB of real source, ``cf-cache-status: HIT``, ``age: 2073`` — CDN
  copies left over from the bad deploy, served at the bare URL after the origin
  had started refusing it. A guard that cannot tell a forgotten cache entry from
  a live misconfiguration reports a recurrence that is not there, and the first
  alert it ever sends is wrong. So the question *is this path published now* is
  asked with a unique query string and ``Cache-Control: no-cache``, which
  bypasses the cache layer and reaches the deployment. Verified that this does
  not cost the check its teeth: ``/``, ``/static/meow.css``, ``/pricing`` and
  ``/login`` all still answer ``200`` and ``/dashboard`` still answers ``302``
  with a query string attached, so a genuine leak still reads as ``200``.
* **The cached copy is still reported, as its own finding** (:attr:`Verdict.cached`).
  It is readable by anyone at that address right now, so staying silent about it
  would be wrong — but the remedy is *purge the cache*, not *fix the build*, and
  an operator told the wrong remedy acts on the wrong thing. The bare URL is
  therefore asked only about a path the authoritative probe **refused**, and
  where it is asked it is a real question: a deployment that has stopped serving
  a path while a cache still does is exactly the state a forgotten copy is in.
  A cycle is two requests per path plus one control, the second skipped wherever
  the first has already given the worse answer.
* **A control probe makes the other nine mean something.** Some hosts answer
  every unknown path with a page. On such a host a ``200`` says nothing about
  *this* file, and the whole check reads as a leak. So one path that certainly
  does not exist is asked for too; if it answers, the verdict is :attr:`Verdict.FALLBACK`
  and the readings are reported as proving nothing rather than as a breach.
* **A path that answers with a page is the only finding.** ``status < 400``
  counts as exposed, so a redirect counts too: a path that resolved somewhere
  was a path the host was willing to talk about.
* **``GET``, never ``HEAD``.** ``HEAD`` would be the cheaper request, and a host
  that answers it with ``405`` would read as *not exposed* — a false clean,
  which is the one answer this must never give. The body is never read, so the
  cost is a header exchange either way.
* **Unreachable is not clean.** If the site cannot be reached the verdict is
  ``unreachable`` and it says so, because "no leak found" and "no answer" are
  different claims and only one of them is reassuring. A partially-answered
  check is reported the same way, for the same reason: the probes that did not
  answer are probes that might have found something.
* **The verdict is one function of one probe function.** :func:`check` takes the
  fetcher as an argument, so every branch above is tested without a network, and
  :func:`note` is what decides whether a verdict is worth a message.

The probe list is short and it is **checked against the repository** in
``tests/test_site_guard.py``: every path here must exist in the checkout, so a
rename cannot leave this module watching paths nobody has. It covers a root
script, a package module, a config file, a dotfile and a test — the five shapes
a static host would expose if the output directory went wrong again.
"""
from __future__ import annotations

import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

#: The site's own address. ``SITE_URL`` is the variable the build already reads,
#: so a deployment that renamed its host has one place to say so.
SITE_ENV = "SITE_URL"
DEFAULT_SITE = "https://meow-bot.pages.dev"

#: A short deadline: this runs in a background loop, and a host that has not
#: answered in ten seconds is reported as unreachable rather than waited on.
REQUEST_TIMEOUT = 10

#: Identifies the check in the host's access log, so a page of hits on these
#: paths is recognisable as this and not as somebody sampling what is public.
USER_AGENT = "MeowSiteGuard/1.0 (+checks that the site is not serving the repo)"

#: How long to wait before saying the same thing again. An *exposure* is
#: reported every cycle — a leak is not noise at any interval — but the three
#: softer findings are, and a guard that repeats itself every cycle is a guard
#: whose messages get filtered, which costs the leak its audience too.
QUIET_REPEAT_SECONDS = 24 * 3600

#: Paths that must never answer with a page. Each is a file this repository
#: really contains; a test fails the suite if one of them stops existing.
PROBES: tuple[str, ...] = (
    # The bot's own entry point, and the largest single file in the tree.
    "bot.py",
    # The Flask app — the module that also serves the dashboard and the API.
    "keep_alive.py",
    # A package module: proves a whole directory is being served, not one file.
    "utils/cloud_sync.py",
    # A feature file, reached through a second directory.
    "cogs/economy.py",
    # Dependency list and the host's own blueprint: what a scanner looks for
    # first, and what names every setting the deployment needs.
    "requirements.txt",
    "render.yaml",
    # A dotfile. Excluded by most tooling by accident, published the same as
    # anything else by a host that was given the wrong directory.
    ".env.example",
    # The front page of the source tree, which is what turns a directory of
    # files into a readable project.
    "README.md",
    # Tests: the file that would have caught this, readable at the same address.
    "tests/test_shared_system.py",
)

#: A path no deployment has ever contained, asked for beside the real probes.
#: Its only job is to tell a *missing* path from a host that answers everything.
CONTROL_PREFIX = "site-guard-control-no-such-path"


def site_url() -> str:
    """Where the public site lives, with the trailing slash removed."""
    return (os.environ.get(SITE_ENV) or "").strip().rstrip("/") or DEFAULT_SITE


def _fetch(site: str, path: str, *, bust: bool = False) -> int | None:
    """The status ``site/path`` answers with, or ``None`` if it did not answer.

    ``None`` is deliberately not ``404``: it means the question was not.

    ``bust`` asks the origin rather than the cache layer, by putting a unique
    query string on the URL — and it is the query string that does the work, for
    the simple reason that it is a *different URL*, so nothing has a stored
    answer for it. The request also carries ``Cache-Control: no-cache``, which is
    a second, weaker signal: measured against this host it changed nothing (a
    client `no-cache` on the cached path returned the same stored copy, same
    ``age``), so it is not what makes the answer trustworthy. It is sent anyway
    because it costs nothing and some caches and configurations do honour it —
    but nothing here should be read as relying on it.
    """
    url = f"{site}/{path.lstrip('/')}"
    if bust:
        url += f"?site-guard={random.randrange(10**12)}"
    headers = {"User-Agent": USER_AGENT}
    if bust:
        headers["Cache-Control"] = "no-cache"
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return int(getattr(response, "status", 0) or 0)
    except urllib.error.HTTPError as error:
        # A 404 is the *good* answer here, and urllib raises on it — so the one
        # status that means everything is fine arrives as an exception.
        return int(error.code)
    except Exception:
        return None


@dataclass(frozen=True)
class Verdict:
    """What one pass over the probes found. Never a guess, always a count."""

    #: The five answers this can give. Declared as constants so that the alerting
    #: side compares against a name rather than a string it has to spell right.
    EXPOSED = "exposed"
    CACHED = "cached"
    FALLBACK = "fallback"
    UNREACHABLE = "unreachable"
    CLEAN = "clean"

    site: str
    at: int
    #: ``path -> status`` as the **deployment** answered, cache-busted. ``None``
    #: for a path that did not answer at all.
    answers: dict[str, int | None] = field(default_factory=dict)
    #: ``path -> status`` for paths the deployment refused but a cache still
    #: serves. Empty in every ordinary cycle; populated only for the paths that
    #: made the bare URL worth asking about.
    cached: dict[str, int] = field(default_factory=dict)
    #: What the control path answered, or ``None`` if it did not answer.
    control: int | None = None

    @property
    def exposed(self) -> tuple[str, ...]:
        """Paths the deployment itself served. Sorted, so an alert is stable."""
        return tuple(
            sorted(p for p, status in self.answers.items()
                   if status is not None and status < 400)
        )

    @property
    def unanswered(self) -> tuple[str, ...]:
        return tuple(sorted(p for p, status in self.answers.items() if status is None))

    @property
    def cached_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.cached))

    @property
    def status(self) -> str:
        """One of :attr:`EXPOSED`, :attr:`FALLBACK`, :attr:`CACHED`,
        :attr:`UNREACHABLE` or :attr:`CLEAN`.

        The order is the order of what a person needs to know first. A control
        path that answered outranks everything: it means this host serves a page
        for any path at all, so a ``200`` on `/bot.py` is not evidence about
        `/bot.py` and the honest report is that the check cannot tell. A cached
        copy outranks *unreachable* for the same reason a found thing outranks a
        missing answer — and ``CLEAN`` requires an answer for **every** probe,
        because a check that could not ask its questions has not learned that
        there is nothing to find.
        """
        if self.control is not None and self.control < 400:
            return self.FALLBACK
        if self.exposed:
            return self.EXPOSED
        if self.cached:
            return self.CACHED
        if self.unanswered:
            return self.UNREACHABLE
        return self.CLEAN

    @property
    def counts(self) -> dict[str, int]:
        return {
            "probes": len(self.answers),
            "exposed": len(self.exposed),
            "cached": len(self.cached),
            "unanswered": len(self.unanswered),
        }


def check(site: str | None = None, *, fetch=None, now: int | None = None) -> Verdict:
    """Probe every declared path once. The fetcher is injectable, so it is testable.

    The injectable fetcher is called as ``fetch(path, bust=True|False)``, and it
    is called twice for a path only when the first answer leaves a question open:
    the bare URL is asked about for a path the cache-busted probe **refused**,
    because a stored copy is the only thing that can answer after the deployment
    has stopped.
    """
    target = (site or site_url()).rstrip("/")
    do_fetch = fetch or (lambda path, bust=True: _fetch(target, path, bust=bust))
    answers: dict[str, int | None] = {}
    cached: dict[str, int] = {}
    for path in PROBES:
        live = do_fetch(path, bust=True)
        answers[path] = live
        if live is None or live < 400:
            # The deployment served it (already a leak), or said nothing at all.
            # Neither leaves a *cache* to find: a ``200`` from the bare URL when
            # the origin was silent could be a stored copy or an origin we simply
            # failed to reach, and reporting it as `cached` would assert "the
            # deployment is not the problem" without having established that.
            continue
        plain = do_fetch(path, bust=False)
        if plain is not None and plain < 400:
            cached[path] = plain
    return Verdict(
        site=target,
        at=int(time.time() if now is None else now),
        answers=answers,
        cached=cached,
        control=do_fetch(f"{CONTROL_PREFIX}-{random.randrange(10**9)}", bust=True),
    )


def describe(verdict: Verdict) -> str:
    """One line for the host log, which is read after the fact and by grep."""
    counts = verdict.counts
    if verdict.status == Verdict.EXPOSED:
        return (
            f"EXPOSED — {counts['exposed']}/{counts['probes']} repository paths "
            f"answer with a page at {verdict.site}: {', '.join(verdict.exposed)}"
        )
    if verdict.status == Verdict.FALLBACK:
        return (
            f"cannot tell — {verdict.site} answers {verdict.control} for a path "
            "that does not exist, so a page here says nothing about what was "
            "asked for"
        )
    if verdict.status == Verdict.CACHED:
        return (
            f"stale cache — {counts['cached']} repository paths still answer "
            f"from the CDN though {verdict.site} itself refuses them: "
            f"{', '.join(verdict.cached_paths)}. Purge the cache; the deployment "
            "is not the problem."
        )
    if verdict.status == Verdict.UNREACHABLE:
        return (
            f"no answer for {counts['unanswered']}/{counts['probes']} probes at "
            f"{verdict.site} — the tree was neither confirmed public nor "
            "confirmed private"
        )
    return f"clean — {counts['probes']} repository paths all answer 404 at {verdict.site}"


#: The two things this module remembers between cycles: the last verdict, so
#: ``/health`` can report it without making a request of its own, and when each
#: quiet finding was last said, so that a standing condition reads as one
#: message rather than one per cycle.
_state: dict = {"last": None, "said_at": {}}


def record(verdict: Verdict) -> None:
    _state["last"] = verdict


def last() -> Verdict | None:
    return _state["last"]


def reset() -> None:
    """Forget everything remembered. For tests, and after a redeploy."""
    _state["last"] = None
    _state["said_at"] = {}


def note(verdict: Verdict, *, now: int | None = None) -> str | None:
    """The sentence to send about this verdict, or ``None`` to stay quiet.

    Records what it decides, which is the one piece of state here: when it last
    reported each of the findings that stand rather than happen. An exposure has
    no such throttle — every cycle that finds one says so, because a leak that is
    still open is the most important thing this bot could be saying — while a
    cached copy, a host that answers everything and an unreachable site are each
    reported once a day, so a monitor waking a sleeping host reads as one message
    rather than one per cycle.
    """
    if verdict.status == Verdict.CLEAN:
        return None
    if verdict.status == Verdict.EXPOSED:
        return describe(verdict)
    moment = int(time.time() if now is None else now)
    said_at = _state["said_at"]
    if moment - said_at.get(verdict.status, 0.0) >= QUIET_REPEAT_SECONDS:
        said_at[verdict.status] = moment
        return describe(verdict)
    return None


def health_json() -> dict:
    """The last verdict for ``/health``: a status, counts, and how stale it is.

    Counts and no names, on the same reasoning as :func:`utils.config_audit.health_json`:
    ``/health`` is unauthenticated. Naming a *leaked* path would not be a secret —
    it is public the moment it is true — but the check exists precisely so the
    answer does not depend on this endpoint being right, and the alert with the
    names goes to the operator's channel.
    """
    verdict = _state["last"]
    if verdict is None:
        return {"status": "unknown", "detail": "not checked yet in this process"}
    counts = verdict.counts
    return {
        "status": verdict.status,
        "probes": counts["probes"],
        "exposed": counts["exposed"],
        "cached": counts["cached"],
        "unanswered": counts["unanswered"],
        "site": verdict.site,
        "checked_seconds_ago": max(0, int(time.time()) - verdict.at),
    }
