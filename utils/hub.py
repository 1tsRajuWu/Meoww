"""The hub's data layer: everything the hub's pages are assembled from.

The hub is a second public surface, and it is deliberately **read-only**: it
renders from the bot's own registry, its own changelog and its own public
readings, and nothing here can change a setting, send a message or open a
database. That is not a limitation to work around — a static host is a host
anybody can read, so putting a credential on it would be the mistake.

Why this module exists rather than the assembly living in ``keep_alive``:
``keep_alive.py`` is the Flask app, and it is already long. This half is pure —
given the same inputs it returns the same structures, with no request, no
session and no global state — which is what makes it testable without a client,
and what lets ``scripts/build_hub.py`` render the pages through the real app
*and* build a search index from the very same data.

The discipline it inherits, and the one to keep: **the hub never invents a
figure and never names a private thing.** Missing counts are ``None`` and their
surface draws a dash; configuration is reported as counts per severity, never as
a list of missing variable names, because "this deployment has no sign-in
secret" is not a sentence to hand a stranger. ``/health`` already made both
choices for the same reason, and this module follows it rather than re-deciding.
"""
from __future__ import annotations

import hashlib
import os
import re

from utils import ai_guard, brand, config_audit, features

# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------

#: ``(id, label, path, icon, group, blurb)``. Order is the order the rail draws
#: them, and ``path`` is the live app's own route — the static build rewrites
#: each one to the file it becomes, so a page's address is written once here and
#: both hosts agree about it.
#:
#: The split into two groups is the one distinction that matters to a reader:
#: ``bot`` is what the thing *is* (systems, commands), ``ops`` is what the
#: deployment is *doing* (its readings, its hosts, its history, its terms).
HUB_PAGES = (
    ("overview", "Overview", "/hub", "home", "bot",
     "What is running, what has been counted, and what changed."),
    ("systems", "Systems", "/hub/systems", "grid", "bot",
     "Every switchable area, and the commands each one gates."),
    ("commands", "Commands", "/hub/commands", "terminal", "bot",
     "The full catalogue, and what governs each command."),
    ("operations", "Operations", "/hub/operations", "pulse", "ops",
     "Four separate health claims, and the windows behind them."),
    ("deployment", "Deployment", "/hub/deployment", "stack", "ops",
     "Where it runs, why there, and what a missing setting costs."),
    ("changes", "What changed", "/hub/changes", "spark", "ops",
     "The project's own changelog, newest first."),
    ("about", "About", "/hub/about", "info", "ops",
     "What the hub reads, what it cannot reach, and how to install."),
)

HUB_PAGE_IDS = tuple(page for page, _l, _p, _i, _g, _b in HUB_PAGES)
HUB_PAGE_TITLES = {page: label for page, label, _p, _i, _g, _b in HUB_PAGES}
HUB_PAGE_LEDES = {page: blurb for page, _l, _p, _i, _g, blurb in HUB_PAGES}

#: Routes the hub's own static build answers on its own host. Everything else
#: that is root-relative is sent to the live app, which is what makes the
#: console links on a static host work instead of 404ing.
HUB_PROXY_ROUTES = ("/api/status",)

#: The catalogue decisions, written down because they are cheap to state and
#: expensive to reverse. Bandwidth is the number that matters for "free
#: forever": a metered allowance is free until one link does well.
HOSTS = (
    {
        "name": "Cloudflare Pages",
        "bandwidth": "unlimited",
        "verdict": "chosen — no ceiling in either direction",
    },
    {
        "name": "Netlify",
        "bandwidth": "~100 GB / month",
        "verdict": "free until it isn't",
    },
    {
        "name": "Vercel",
        "bandwidth": "~100 GB / month",
        "verdict": "free until it isn't",
    },
    {
        "name": "GitHub Pages",
        "bandwidth": "~100 GB / month",
        "verdict": "not available on this plan",
    },
)


def marketing_url() -> str:
    """The bot's public marketing site, which owns the legal pages.

    Read from the same setting the site guard checks, so the hub links to the
    address that is actually published rather than to a guess that stops being
    true the moment it moves.
    """
    from utils import site_guard  # noqa: PLC0415 — avoids an import cycle


    return (config_audit.get("MEOW_MARKETING_URL") or site_guard.site_url()
            or "https://dacat.pages.dev").rstrip("/")


def live_url() -> str:
    """Where the bot itself is reachable — the console's own host."""
    return (config_audit.get("LIVE_APP_URL")
            or config_audit.get("MEOW_LIVE_APP_URL")
            or "https://1tsrajowo-meow.hf.space").rstrip("/")


def dashboard_url() -> str:
    """Where the member dashboard lives — a host of its own, beside this one.

    The console and the dashboard are two hosts, so a link between them has to be
    **absolute**: `/dashboard` on the console's host is not a path that host
    answers — its worker 301s it to the public site, which is another hop and a
    different session (a cookie is host-scoped). The same reader the two above
    use, with the address this project publishes as the fallback, because a
    deployment that moves its dashboard sets the same setting the one-time link
    is built from.
    """
    return (config_audit.get("DASHBOARD_BASE_URL")
            or "https://dacatdashboard.pages.dev").rstrip("/")


# ---------------------------------------------------------------------------
# The changelog
# ---------------------------------------------------------------------------

#: ``## 1.2.3`` or ``## Unreleased`` — the version a bullet belongs to.
_HEADING = re.compile(r"^##\s+(.+?)\s*$")
#: ``- **Lead**: body``, or ``- content`` when there is no bold lead.
_BULLET = re.compile(r"^-\s+(.*)$")
_LEAD = re.compile(r"^\*\*(.+?)\*\*\s*:?\s*(.*)$", re.DOTALL)
#: A leading emoji, which the changelog uses as an icon for the entry.
_EMOJI = re.compile(
    r"^([\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d]+)\s*"
)

#: The changelog's own markdown, reduced to plain prose for the page. Bold and
#: backticks are not rendered as markup on purpose: this text reaches a public
#: page, and a renderer is a parser is an injection surface. The runs are simply
#: unwrapped, which loses emphasis and loses nothing else.
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_EM = re.compile(r"\*([^*\n]+)\*")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)


def plain(text: str) -> str:
    """One changelog fragment as readable plain text, safest-first.

    Quotes and quotes-adjacent punctuation are normalised only where the file
    itself uses them as decoration (``"`` and ``"``), which is what makes a line
    written in an editor read the same in a browser.
    """
    out = _MD_LINK.sub(r"\1", text or "")
    out = _MD_CODE.sub(r"\1", out)
    out = _MD_BOLD.sub(r"\1", out)
    # Single-asterisk emphasis, after the bold pass and never before it: `**x**`
    # matches this pattern too, so running it first would leave the outer pair
    # behind as two literal asterisks on a page that renders no markup at all.
    out = _MD_EM.sub(r"\1", out)
    # …and a `**` that survived both passes is still markup rather than content.
    # Two entries in this file carry an **unpaired** marker — the closing one was
    # eaten by an earlier pass — so the pair-stripping left it inside a published
    # sentence (*"Suite unchanged at 148 failed / 2,429 passed / 41 skipped**"*),
    # where a visitor reads it as a typo in a document nobody rendered. A doubled
    # asterisk is never prose, so it goes; a single one is left alone, because it
    # can be.
    out = out.replace("**", "")
    out = _MD_HEADING.sub("", out)
    out = out.replace("`", "").replace('\\"', '"')
    return re.sub(r"\s+", " ", out).strip()


def parse_changelog(text: str, *, limit: int | None = None) -> tuple[list[dict], int]:
    """The changelog as a flat, newest-first list of entries.

    Flat rather than grouped by version, because the question a reader arrives
    with is "what changed recently", not "what shipped in 1.0.0" — the file is
    written newest-first and for most of its life sits entirely under
    ``Unreleased``, where a version grouping would be one group of 230.

    Returns ``(entries, total)`` where ``total`` is how many exist, so a page can
    say honestly that it is showing a window onto a longer list rather than
    implying the file ends where the page does.
    """
    entries: list[dict] = []
    version = ""
    for raw in (text or "").splitlines():
        heading = _HEADING.match(raw)
        if heading:
            version = plain(heading.group(1))
            continue
        if raw.lstrip().startswith("#"):
            continue
        bullet = _BULLET.match(raw.strip())
        if not bullet:
            continue
        content = bullet.group(1).strip()
        lead_match = _LEAD.match(content)
        lead_raw = lead_match.group(1) if lead_match else ""
        body = lead_match.group(2) if lead_match else content
        icon_match = _EMOJI.match(lead_raw)
        icon = icon_match.group(1) if icon_match else "•"
        lead = plain(lead_raw[icon_match.end():] if icon_match else lead_raw)
        body = plain(body)
        if not lead:
            # A bullet with no bold lead is still a change; its first sentence
            # is the headline rather than the entry being dropped.
            lead, _, tail = body.partition(". ")
            body = tail or body
        entries.append({
            "version": version or "Unreleased",
            "icon": icon,
            "lead": lead,
            # Parsed whole and published only in part. The headline is written in
            # product terms; the body is engineering notes — file paths, the
            # shape of a past incident, the names of settings — and this page is
            # unauthenticated. So the page draws `lead`, `body` is kept here for
            # the guard that proves it stays off the page, and the truncation
            # that used to sit between them is gone with the thing it fed.
            "body": body,
        })
    total = len(entries)
    if limit is not None:
        entries = entries[:limit]
    return entries, total


def read_changelog(path: str, *, limit: int | None = None) -> tuple[list[dict], int]:
    """The changelog off disk. A missing file is an empty history, not a crash.

    A page that cannot find its own changelog should say it has nothing to show
    rather than refuse to render; this is the same rule every absent figure on
    this hub follows.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return parse_changelog(handle.read(), limit=limit)
    except OSError:
        return [], 0


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def _system_row(feature: features.Feature) -> dict:
    """One registry entry, as the hub's pages draw it."""
    gates = list(feature.gate_commands)
    return {
        "id": feature.id,
        "name": feature.name,
        "icon": feature.icon,
        "category": feature.category,
        "desc": feature.description,
        "commands": list(feature.commands),
        # A system with no gate of its own (the core utilities) is switched by
        # nothing; the template says "always on" rather than drawing an empty
        # list, which is the honest version of the same fact.
        "gates": [f"/{gate}" for gate in gates],
        "toggleable": feature.toggleable,
        "default_on": feature.enabled_by_default,
        "cog": feature.cog,
        "help_key": feature.help_key,
    }


def systems_payload() -> dict:
    """Every system, its category grouping, and the counts the pages quote.

    The grouping is by the registry's own ``category`` field and the order is the
    registry's order, so a page cannot present the product differently from the
    way the bot describes it to an administrator.
    """
    rows = [_system_row(feature) for feature in features.FEATURES]
    groups: list[tuple[str, list[dict]]] = []
    for row in rows:
        for category, group in groups:
            if category == row["category"]:
                group.append(row)
                break
        else:
            groups.append((row["category"], [row]))
    fixed = [row for row in rows if not row["toggleable"]]
    return {
        "systems": rows,
        "groups": groups,
        "fixed": fixed,            "counts": {
            # From the registry's own definitions rather than from the rows above,
            # so the rail, the console's cards and the landing page cannot count
            # different things under the same word. ``systems`` here happens to
            # equal ``len(rows)``; saying so through the shared function is what
            # keeps that true when a feature gains a row somewhere else.
            "systems": features.system_count(),
            "toggleable": sum(1 for row in rows if row["toggleable"]),
            "fixed": len(fixed),
            "categories": len(groups),
            "root_commands": len(features.MODULE_COMMAND_MAP),
            "commands": features.command_count(),
        },
        "problems": features.validate(),
        "core_cogs": list(features.CORE_COGS),
    }


def commands_payload() -> dict:
    """Every published command and what governs it.

    Counted from three sources that must agree — each feature's own gate list,
    the multi-system gates, and the always-allowed set — because that agreement
    is the property the whole registry exists to hold.
    """
    shared = [
        {
            "name": command,
            "systems": [
                {
                    "id": feature.id,
                    "name": feature.name,
                    "icon": feature.icon,
                }
                for feature in (features.get(fid) for fid in feature_ids)
                if feature is not None
            ],
        }
        for command, feature_ids in sorted(features.SHARED_GATES.items())
    ]
    return {
        "shared": shared,
        "always_allowed": sorted(features.ALWAYS_ALLOWED_COMMANDS),
        "dm_allowed": sorted(features.DM_ALLOWED_COMMANDS),
    }


# ---------------------------------------------------------------------------
# Configuration, as counts
# ---------------------------------------------------------------------------

#: How each severity is described to a reader, and how much a missing one costs.
#: The wording is the point: ``essential`` means a feature that cannot work at
#: all, and ``tuning`` means a default the operator may prefer.
_SEVERITY_COPY = {
    config_audit.ESSENTIAL: (
        "Essential",
        "A feature cannot work at all without it.",
        "bad",
    ),
    config_audit.SECURITY: (
        "Security",
        "Something is reachable that should not be.",
        "bad",
    ),
    config_audit.CAPABILITY: (
        "Capability",
        "A feature works, but with less than it could do.",
        "warn",
    ),
    config_audit.TUNING: (
        "Tuning",
        "A default is in use. Often fine.",
        "info",
    ),
    config_audit.HOST: (
        "Host",
        "The platform provides it; absent means the host is not set up.",
        "info",
    ),
}

_SCOPE_COPY = {
    config_audit.RUNTIME: (
        "Runtime",
        "Read by the running bot. Absent here means a feature is limited.",
    ),
    config_audit.BUILD: (
        "Build",
        "Read by whatever renders these pages. Absent here means a page "
        "quietly loses something and nothing raises.",
    ),
}


def config_payload(environ=None, *, readable: bool = True) -> dict:
    """The configuration inventory, as **counts and no names**.

    Read from ``config_audit.SETTINGS`` rather than from the result of
    :func:`config_audit.audit`, and the difference is not cosmetic: ``audit()``
    deliberately reports only the *runtime* half, because it exists to tell an
    operator what to paste into a host. The hub needs both halves, because a
    build setting that is absent is the failure this project has actually had —
    the front door shipped without its invite button and nothing anywhere raised.

    Every value here is a count. The names are thrown away on purpose: the console
    answers the named question to a signed-in administrator, and a public page
    answers it as arithmetic, which is enough for a reader to know whether the
    deployment is complete and not enough to tell anybody where it is weak.
    """
    settings = list(config_audit.SETTINGS)
    env: dict = {} if environ is None else environ
    #: ``readable=False`` is the static build's case, and it is the *whole* reason
    #: this flag exists: the build runs on a machine that is not the deployment,
    #: so its environment is not the deployment's. Reporting
    #: ``1 of 50 settings present, 18 need attention`` from a CI runner is not a
    #: stale reading — it is a wrong one, confidently printed on a page that has
    #: no way to notice. The same structures come back with the counts absent and
    #: the page draws a dash until the live read replaces them, exactly as every
    #: other figure on this hub does.
    absent = ([] if not readable
              else [s for s in settings if not config_audit.is_set(s.name, env)])

    def _count(values) -> int | None:
        """A count, or nothing at all when this machine is not the deployment."""
        return None if not readable else len(values)

    def _rows_for(attribute: str, value: str):
        return [s for s in settings if getattr(s, attribute) == value]

    def _bucket(severity: str) -> dict:
        label, what, tone = _SEVERITY_COPY[severity]
        total = len(_rows_for("kind", severity))
        missing = _count([s for s in absent if s.kind == severity])
        return {
            "label": label,
            "what": what,
            "tone": tone if missing else "ok",
            "missing": missing,
            "present": None if missing is None else total - missing,
            "total": total,
        }

    scopes = []
    for scope in (config_audit.RUNTIME, config_audit.BUILD):
        label, what = _SCOPE_COPY[scope]
        total = len(_rows_for("scope", scope))
        missing = _count([s for s in absent if s.scope == scope])
        scopes.append({
            "label": label,
            "what": what,
            # A missing build setting is the quieter failure and the one that has
            # actually shipped a broken page, so it is amber where a missing
            # runtime setting is merely informational.
            "tone": "ok" if missing == 0 else ("warn" if scope == config_audit.BUILD else "info"),
            "missing": missing,
            "present": None if missing is None else total - missing,
            "total": total,
        })

    return {
        "readable": readable,
        # The *total* survives a build: how many settings exist is a fact about
        # the code, not about a host, and it is worth stating either way.
        "total": len(settings),
        "set": None if not readable else len(settings) - len(absent),
        "missing": _count(absent),
        "attention": _count([
            s for s in absent
            if s.kind in config_audit.NOTABLE and not config_audit.is_implied(s, env)
        ]),
        "rows": [_bucket(severity) for severity in config_audit.SEVERITY
                 if _rows_for("kind", severity)],
        "scopes": scopes,
    }


# ---------------------------------------------------------------------------
# The static prose the pages carry
# ---------------------------------------------------------------------------


def topology(gateway_connected: bool) -> list[dict]:
    """The hosts this project runs on, as the deployment page draws them."""
    return [
        {
            "name": "Render",
            "role": "the bot",
            "tone": "ok" if gateway_connected else "warn",
            "what": (
                "One process runs the bot's gateway and the console's web server "
                "together, because they share the database and the live client. Its "
                "free tier sleeps when nobody knocks — which is why the pages a "
                "stranger reads first do not live here."
            ),
            "facts": [
                "discord.py gateway",
                "Flask console",
                "scheduled-message loop",
                "SQLite",
            ],
            "link": "/dashboard",
            "link_label": "The console",
        },
        {
            "name": "Cloudflare Pages",
            "role": "the public pages",
            "tone": "ok",
            "what": (
                "A CDN with no bandwidth ceiling, so a link that does well costs "
                "nothing. It serves the marketing site and this hub, and the live "
                "numbers on both come from the same public endpoint on the bot's own "
                "host — a page served from a cache still has to ask for its figures."
            ),
            "facts": ["static build", "CSP + HSTS", "no bandwidth cap", "this hub"],
            "link": "/hub/deployment",
            "link_label": "How it is built",
        },
        {
            "name": "GitHub",
            "role": "the source",
            "tone": "ok",
            "what": (
                "Where the code lives and where each host's build is triggered from. "
                "The repository is private and this hub deliberately links to nothing "
                "inside it: a public page pointing at a private path is a 404 with "
                "extra steps."
            ),
            "facts": ["private repository", "triggers both builds"],
            "link": "/hub/changes",
            "link_label": "What changed",
        },
        {
            "name": "UptimeRobot",
            "role": "the watcher",
            "tone": "info",
            "what": (
                "An outside process checking the host on a schedule, which is the only "
                "reading here that comes from neither of the other two. Its monitors "
                "are read by the background loop and cached, so this page can be left "
                "open all day without ever costing it a request."
            ),
            "facts": ["monitors", "30-day ratio", "outside both hosts"],
            "link": "/hub/operations",
            "link_label": "The readings",
        },
    ]


def pipeline() -> list[dict]:
    """How a change reaches production, including the parts that can skip."""
    return [
        {
            "icon": "✏️",
            "title": "A change is committed",
            "detail": "On main, in the private repository.",
            "badge": "local",
            "tone": "",
        },
        {
            "icon": "🧪",
            "title": "The suite runs",
            "detail": (
                "The whole suite, plus the registry and policy guards. A failure here "
                "is the only thing that stops a change from reaching either host."
            ),
            "badge": "tests",
            "tone": "info",
        },
        {
            "icon": "🏗️",
            "title": "The pages are rendered",
            "detail": (
                "The real app renders each page, every root-relative URL is rewritten, "
                "and the builder refuses to emit a link that would 404 on a static host."
            ),
            "badge": "build",
            "tone": "info",
        },
        {
            "icon": "🚀",
            "title": "Both hosts pick it up",
            "detail": (
                "Render redeploys the bot from the same commit; Cloudflare rebuilds the "
                "pages. Neither is a claim that the other landed, which is why the "
                "deploy step asks each host which commit it is serving."
            ),
            "badge": "deploy",
            "tone": "ok",
        },
        {
            "icon": "👀",
            "title": "An outside check confirms it",
            "detail": (
                "A scheduled job asks the published site for files that must never be "
                "readable there — a source file, a config file, an env file — and "
                "alerts if any of them answers at all."
            ),
            "badge": "after",
            "tone": "ok",
        },
    ]


def endpoints() -> list[dict]:
    """The readings the hub and anything else may ask for."""
    live = live_url()
    return [
        {
            "path": "/api/status",
            "answers": "Every figure on this hub, merged into one small payload.",
            "public": True,
            "absolute": f"{live}/api/status",
        },
        {
            "path": "/health",
            "answers": "Four separate claims: up, configured, publishing safely, reliable.",
            "public": True,
            "absolute": f"{live}/health",
        },
        {
            "path": "/api/figures",
            "answers": "Servers, members, and four counted windows.",
            "public": True,
            "absolute": f"{live}/api/figures",
        },
        {
            "path": "/api/uptime",
            "answers": "The monitors themselves — names and addresses.",
            "public": False,
            "absolute": f"{live}/api/uptime",
        },
        {
            "path": "/api/config",
            "answers": "Every setting, present or absent, by name.",
            "public": False,
            "absolute": f"{live}/api/config",
        },
        {
            "path": "/api/command-scope",
            "answers": "What Discord actually holds for one server, beside its switches.",
            "public": False,
            "absolute": f"{live}/api/command-scope",
        },
    ]


def trust_links() -> list[dict]:
    """The pages worth reading before installing anything, as rows."""
    site = marketing_url()
    return [
        {
            "icon": "🔒",
            "title": "Privacy policy",
            "detail": (
                "What is stored, for how long, and what is never stored — including "
                "the message content the tracker deliberately does not keep."
            ),
            "href": f"{site}/privacy",
        },
        {
            "icon": "📜",
            "title": "Terms of service",
            "detail": (
                "The rules of use, the limits of any warranty, and what happens when "
                "the bot is removed from a server."
            ),
            "href": f"{site}/terms",
        },
        {
            "icon": "🐾",
            "title": "The public site",
            "detail": (
                "The front door: what the bot is, what it does, and the invite link. "
                "Nothing on it is for sale, and it says so."
            ),
            "href": site,
        },
    ]


# ---------------------------------------------------------------------------
# The search index
# ---------------------------------------------------------------------------


def search_index(systems: dict, commands: dict) -> list[dict]:
    """Everything the hub can reach, for the palette to search.

    Built from the same structures the pages render, so the palette cannot offer
    a command the reference page has retired or miss a system it added. Commands
    are entries that copy rather than link, because there is nowhere to navigate
    a command to — the honest action for ``/market buy`` is to hand it over.
    """
    entries: list[dict] = []
    for page, _label, path, _icon, _group, blurb in HUB_PAGES:
        entries.append({
            "kind": "page",
            "label": HUB_PAGE_TITLES[page],
            "hint": blurb,
            "href": path,
        })
    for system in systems["systems"]:
        entries.append({
            "kind": "system",
            "label": f"{system['icon']} {system['name']}",
            "hint": system["category"],
            "href": f"/hub/systems#cat-{system['category'].lower().replace(' ', '-')}",
        })
    for command in sorted(features.MODULE_COMMAND_MAP):
        system = features.get(features.MODULE_COMMAND_MAP[command][0])
        entries.append({
            "kind": "command",
            "label": f"/{command}",
            "hint": system.name if system else "",
            "text": f"/{command}",
            "copy": f"/{command}",
        })
    for command in commands["shared"]:
        names = " + ".join(system["name"] for system in command["systems"])
        entries.append({
            "kind": "command",
            "label": f"/{command['name']}",
            "hint": f"needs {names}",
            "text": f"/{command['name']}",
            "copy": f"/{command['name']}",
        })
    return entries


#: Where the hub keeps the two files it owns.
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "hub", "static")


def asset_version() -> str:
    """A short hash of the hub's own assets, for the ``?v=`` cache-buster.

    Both hub assets are served immutably for a year, which is only safe when the
    URL changes when the file does. Hashing the files themselves means the
    buster cannot drift: a hand-maintained ``?v=4`` stops busting the first time
    somebody forgets to increment it, and the symptom is a stylesheet that is
    right in development, wrong in production, and identical in both to look at.

    Read every call rather than cached — two small files, on a path that renders
    a page — so a stylesheet edited in a running development server is picked up
    without a restart.
    """
    digest = hashlib.sha256()
    try:
        names = sorted(os.listdir(STATIC_DIR))
    except OSError:
        return "0"
    for name in names:
        path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as handle:
                digest.update(name.encode("utf-8"))
                digest.update(handle.read())
        except OSError:
            continue
    return digest.hexdigest()[:12]


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

#: The hub's Content-Security-Policy, and it is **stricter than the rest of the
#: app** in the way that matters: no `'unsafe-inline'` for scripts or styles at
#: all. That is only possible because the hub's pages carry no inline script and
#: no `style="…"` attribute anywhere — every number a script drives is written
#: through CSSOM, which CSP does not govern, and everything else is a class.
#:
#: It lives here, in a module both hosts read, because it is one policy: the
#: Flask app applies it to `/hub/*` and `scripts/build_hub.py` writes it into the
#: Cloudflare `_headers` file for the static copy. Two hand-written copies of a
#: security policy is how a tightening lands on one host and not the other, and
#: the looser one is always the one nobody is looking at.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

#: What the static host cannot inherit from Flask, because there is no Flask
#: there: the headers `set_security_headers` would otherwise be the only thing
#: setting them. A CDN copy without these is the *least* protected copy of a
#: public page, which is the wrong way round for the pages that ask a stranger to
#: trust a bot.
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
) + ai_guard.TRAIN_HEADERS
# The anti-AI reservation is appended rather than written again here: the same
# three values travel on every response this project sends, on every host — the
# app in ``set_security_headers``, the static copies through this tuple, and the
# owner console through its own `_headers` file. One list, three writers, and
# the values themselves live in ``utils/ai_guard.py``.

#: How many changelog entries the hub shows. The file is 230-odd entries and
#: grows with every change, so the page states how many it is showing rather than
#: implying the history ends where the page does. At 60 full paragraphs the page
#: was 198 KB of HTML, which is a page nobody on a phone will finish loading.
CHANGES_SHOWN = 40

#: Where the hub keeps its own assets, and what the static build copies out of
#: the app's shared `static/`. Listed rather than globbed so a page cannot come to
#: depend on an asset nobody is shipping, and `tests/test_hub.py` fails on a
#: reference to anything outside this set plus the hub's own two files.
#:
#: The family itself is `utils/brand.py`'s, and read rather than restated: the
#: list used to name four logo marks and four favicons drawn for a nine-theme
#: vocabulary, so a rebrand had to edit two lists in two languages and the one
#: that was forgotten shipped a broken image silently. One author, and
#: `tests/test_brand.py` holds these two to each other.
SHARED_ASSETS = (
    *brand.ASSETS,
)
