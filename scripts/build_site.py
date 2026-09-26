"""Build the public site as static files, for a static host.

Why this exists
---------------
The pages the bot serves (``/``, ``/pricing``, ``/privacy``, ``/terms``) are the
ones worth reading *before* you trust a bot with your server, and they were
reachable only from the single Render process that also runs the bot. That host
is a free tier that sleeps, so the front door was shut whenever nobody had
knocked recently. A static host has no such problem — Cloudflare Pages is where
this is deployed, via ``.github/workflows/site.yml``.

Two things make this harder than copying files:

* Some static hosts serve a site from a subpath — a GitHub *project* site lives
  at ``/<repo>/`` — and on those every root-relative URL the templates emit
  (``/static/meow.css``, ``/pricing``, the ``url()`` of the cat cursor inside
  the stylesheet) points outside the site and 404s. Every one of them is
  rewritten to a relative path here, which is correct at a subpath *and* at a
  root, so the output is the same whichever host takes it. The cursor is the one
  that would fail *silently*: a missing cursor image falls back to the system
  arrow with nothing in any console to say so.
* The pages that need the backend (``/dashboard``, ``/login``, ``/servers``, the
  market, ``/health``) cannot be static at all. They are rewritten to the live
  app, so a visitor who wants the real thing is sent to the process that can
  actually answer instead of to a 404.

The templates are rendered by the **real Flask app** through its test client
rather than re-implemented, so the page a visitor reads on Pages and the page
the bot serves are the same template with the same context and cannot drift into
disagreeing about what the product is.

Usage::

    python scripts/build_site.py                 # -> _site/
    python scripts/build_site.py --out /tmp/x
    LIVE_APP_URL=https://example.test python scripts/build_site.py
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utils import ai_guard  # noqa: E402 — needs ROOT on the path first

#: Where the real, live app is. Every link that needs a running process points
#: here. It is the Render service's own hostname, which is stable across
#: deploys; a custom domain can replace it later by setting ``LIVE_APP_URL``.
DEFAULT_LIVE_APP = "https://1tsrajowo-meow.hf.space"

#: ``route`` -> the file it becomes. Order is the order they are built.
#:
#: The first three are the site proper — one question each — and the sitemap is
#: built from this tuple, so a page that is not listed here is a page search
#: engines are not told about. They are also the reason `_routes_file()` does not
#: need to change when one is added: the worker's `include` list is the *dynamic*
#: paths, and everything else is served from the upload.
PAGES = (
    ("/", "index.html"),
    ("/systems", "systems.html"),
    # The radio's own page, and the reason it is one: its failures are quiet (a
    # connected bot, a card that says "playing", and silence), so the things a
    # reader would otherwise discover by being disappointed are written down here
    # instead. On the site rather than only in Discord because a policy is not the
    # only thing worth reading before you trust a bot.
    ("/radio", "radio.html"),
    ("/start", "start.html"),
    ("/pricing", "pricing.html"),
    ("/privacy", "privacy.html"),
    ("/terms", "terms.html"),
    # The third document, and the one a collector is pointed at. It is built
    # rather than proxied for the same reason the other two are: the page a
    # stranger reads before trusting a bot should not be behind a free-tier host
    # that sleeps, and this one is also the address in `ai.txt`.
    ("/ai", "ai.html"),
)

#: The application's client id. This is *public* — it is in every invite link
#: anybody can read — and it has to be present at build time, because the
#: templates deliberately **omit** the invite button when the id is unknown
#: (a button that goes nowhere is worse than no button). Building without it
#: therefore ships a landing page whose main call to action is missing, which is
#: exactly the state the live site was in. Overridable with ``--client-id`` or
#: ``DISCORD_CLIENT_ID``.
PUBLIC_CLIENT_ID = "1548380269776605324"

#: The stylesheets that are rewritten too, because of ``url()``.
#:
#: Both are listed, and the second one is the reason this is a tuple rather than a
#: name: `meoww.css` declares the two vendored families with `url("fonts/…")`,
#: which is relative to the stylesheet — so a sheet that is copied without being
#: rewritten asks for `static/fonts/…` from inside `static/`, which resolves to
#: `static/static/fonts/…` and 404s. A missing face is the quietest failure on
#: this site: the page renders, in somebody else's typeface, and nothing in any
#: log says so.
CSS_ASSETS = ("meow.css", "meoww.css")

#: ``href|src|action="…"`` — every attribute that can carry a URL.
_ATTR = re.compile(r'\b(href|src|action|poster)\s*=\s*"([^"]*)"', re.IGNORECASE)
#: ``url(…)`` inside CSS, quoted or not.
_CSS_URL = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.IGNORECASE)


#: Dynamic paths reverse-proxied through Cloudflare Pages to the Render backend.
PROXIED_ROUTES = (
    "/login",
    "/logout",
    "/dashboard",
    "/owner",
    "/servers",
    "/api",
    "/market",
    "/health",
    # The hub — seven reader-facing pages about what the bot is and what it is
    # doing — is proxied rather than built here, and that is a decision with a
    # history worth keeping. It used to be a fifth Cloudflare project of its own
    # (``meowdev``), which is precisely how it sat an entire rebrand behind: a
    # host with no path in the release flow is a host the next rebrand forgets.
    # The pages themselves are the *application's* — ``/hub`` and ``/hub/<page>``
    # are Flask views rendering from ``utils/hub.py`` — so proxying them puts
    # them on the address that is built, uploaded and checked four times a
    # release, with no second copy of a page to keep in step.
    "/hub",
)


def _split_suffix(url: str) -> tuple[str, str]:
    """``/pricing#faq`` -> ``('/pricing', '#faq')``; the query/fragment is kept."""
    for index, char in enumerate(url):
        if char in "#?":
            return url[:index], url[index:]
    return url, ""


def rewrite_url(
    url: str,
    live_app: str,
    *,
    from_stylesheet: bool = False,
    proxy_mode: bool = False,
) -> str:
    """One URL, made correct for a static site served from a subpath.

    Pure and total: anything already absolute, external, a fragment or a data
    URI is returned unchanged, so this can be applied to every URL in a document
    without an allow-list of the ones worth touching.

    ``from_stylesheet`` matters more than it looks. A stylesheet's ``url()`` is
    resolved against *the stylesheet*, and the stylesheet lives in ``static/``.
    So a page needs ``static/x`` while ``meow.css`` needs a bare ``x`` — and
    getting that wrong costs the cat cursor silently, because a browser that
    cannot fetch a cursor image draws the system arrow and logs nothing.
    """
    raw = (url or "").strip()
    if not raw:
        return url
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://", "//", "mailto:", "data:", "javascript:")):
        return url
    if raw.startswith("#"):
        return url
    if not raw.startswith("/"):
        # Already relative — a static page links its own neighbours this way.
        return url

    path, suffix = _split_suffix(raw)

    # 1. Assets: /static/x -> static/x from a page, x from inside static/.
    if path.startswith("/static/"):
        tail = path[len("/static/"):]
        return (tail if from_stylesheet else "static/" + tail) + suffix

    # 2. Pages that exist as files here.
    for route, filename in PAGES:
        if path == route:
            target = "./" if filename == "index.html" else filename
            return target + suffix

    # In proxy mode, dynamic routes stay on the same origin (handled by _worker.js)
    if proxy_mode and (path in PROXIED_ROUTES or any(path.startswith(r + "/") for r in PROXIED_ROUTES)):
        return raw

    # 3. Everything else needs the live process: send the visitor there rather
    #    than to a dead relative path. This is the whole class of pages a static
    #    host cannot serve — the dashboard, sign-in, the market, the health line.
    return live_app.rstrip("/") + raw


def rewrite_html(text: str, live_app: str, *, proxy_mode: bool = False) -> str:
    """Every URL a rendered page carries, rewritten in one pass.

    Inline ``<style>`` blocks get the stylesheet's rule, not the page's — the
    ``url()`` inside one is still resolved against the document's base, so a
    page-level ``static/`` prefix is right there. Only a standalone ``.css``
    file needs the bare form, which is why ``rewrite_css`` exists separately.
    """
    text = _ATTR.sub(
        lambda match: f'{match.group(1)}="{rewrite_url(match.group(2), live_app, proxy_mode=proxy_mode)}"',
        text,
    )
    return _CSS_URL.sub(
        lambda match: f"url('{rewrite_url(match.group(2), live_app, proxy_mode=proxy_mode)}')",
        text,
    )


def rewrite_css(text: str, live_app: str) -> str:
    """Asset URLs inside a stylesheet, which are relative to that file."""
    return _CSS_URL.sub(
        lambda match: f"url('{rewrite_url(match.group(2), live_app, from_stylesheet=True)}')",
        text,
    )


def _clean_targets(text: str, live_app: str, *, proxy_mode: bool = False) -> list[str]:
    """Root-relative URLs that survived rewriting — a defect if non-empty.

    Kept explicit and used as an assertion rather than trusted to the regexes:
    an absolute ``/static/`` path that slips through is a link that 404s on
    Pages, which is exactly the failure this whole file exists to prevent.
    """
    leftovers: list[str] = []
    for match in _ATTR.finditer(text):
        value = match.group(2)
        if value.startswith("/") and not value.startswith("//"):
            path, _ = _split_suffix(value)
            if proxy_mode and (path in PROXIED_ROUTES or any(path.startswith(r + "/") for r in PROXIED_ROUTES)):
                continue
            leftovers.append(value)
    for match in _CSS_URL.finditer(text):
        value = match.group(2)
        if value.startswith("/") and not value.startswith("//"):
            leftovers.append(value)
    return leftovers


#: The path this build asks the app for in order to get the site's 404 page.
#:
#: Nothing routes it, so the app answers with `_error_page(404)` — the same
#: document a visitor gets from Render — and that response *is* `404.html` here.
#: This replaced a page written out in this file by hand: it named `page-404`,
#: `brand-logo`, `hero-cta`, `btn-primary` and three more classes the current
#: sheet does not define, and drew `static/logo.svg`, which the redesign deleted
#: — so a mistyped address on the published site got a broken-image glyph, a
#: heading in the browser's default font, and nothing to click. It is also the
#: third place in this project that learned the same lesson: two documents for
#: one page is one document too many.
NOT_FOUND_PROBE = "/this-address-is-not-a-page"


#: The policy the static copy runs under.
#:
#: Every header below is normally set by `keep_alive.set_security_headers` — and
#: none of them exist on a static host, because there is no Flask there to set
#: them. Publishing these pages to a CDN without this file would therefore ship
#: the *least* protected copy of them: no CSP, no frame denial, no HSTS, no
#: referrer policy. The pages are the ones asking a stranger to install a bot, so
#: that trade is backwards.
#:
#: It is deliberately **tighter** than the Flask policy in one respect and looser
#: in none: the market page is not among the pages built here, so none of the
#: chart library's CDNs need allowing. `'unsafe-inline'` for scripts is required
#: by the pages themselves (the nav toggle and the reveal observer are inline)
#: — and it is the reason nonce-based CSP is worth doing for both hosts at once.
#: `data:` is allowed for images because the favicon may be inlined by a host.
STATIC_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def _headers_file() -> str:
    """Cloudflare Pages' `_headers`, which is this site's only possible CSP.

    Format: a path pattern, then indented `Name: value` lines. `/*` matches every
    page; the static assets get their own rule because they are the only thing
    here worth caching hard, and they carry a `?v=` cache-buster in the templates
    precisely so an immutable cache is safe.
    """
    reservation = "".join(f"  {name}: {value}\n" for name, value in ai_guard.headers().items())
    return (
        "/*\n"
        f"  Content-Security-Policy: {STATIC_CSP}\n"
        "  X-Content-Type-Options: nosniff\n"
        "  X-Frame-Options: DENY\n"
        "  Referrer-Policy: no-referrer\n"
        "  Permissions-Policy: camera=(), microphone=(), geolocation=()\n"
        "  Strict-Transport-Security: max-age=31536000; includeSubDomains\n"
        # The anti-AI reservation, on the CDN copy as well as the app's own. A
        # static host has no Flask to set a header, and this is the copy most
        # people actually read — so without these lines the public site would be
        # the one surface where the prohibition was only ever a comment in a
        # file. ``ai_guard.headers()`` is the same call the app makes.
        f"{reservation}"
        # NOT `no-store`: that is right for the dashboard and wrong for a page
        # whose whole job is to be readable by a stranger on a slow connection.
        # Short and revalidated, so a deploy is picked up within minutes while a
        # repeat visit is still instant.
        "  Cache-Control: public, max-age=300, must-revalidate\n"
        "\n"
        "/static/*\n"
        "  Cache-Control: public, max-age=31536000, immutable\n"
        "\n"
    )


def _redirects_file() -> str:
    """The redirects file for the CDN copy.

    Cloudflare Pages natively handles clean, extension-less URLs (serving
    `/pricing` from `pricing.html` and 308-redirecting direct requests for
    `/pricing.html` to `/pricing`).

    Explicit 200 rewrites from `/route` to `/route.html` must NOT be defined
    here because Cloudflare Pages evaluates the rewritten `.html` path against
    its Clean URLs rule, resulting in an infinite 308 redirect loop.
    """
    return (
        "# Cloudflare Pages natively serves clean extensionless URLs (e.g. /pricing -> pricing.html).\n"
        "# Explicit 200 rewrites to .html files are omitted to avoid circular 308 redirect loops.\n"
        "\n"
        "# The page this one used to be. A URL somebody bookmarked is not a mistake\n"
        "# to punish, so the old address answers with the new one — on the static\n"
        "# host as well as in the app, or a link shared last month would land on the\n"
        "# cleanup and 404 while the live app redirected it.\n"
        "/music /radio 301\n"
    )


def _sitemap(pages, live_app: str) -> str:
    """Only the pages that exist here — a sitemap naming 404s is worse than none."""
    import datetime

    today = datetime.date.today().isoformat()
    urls = "\n".join(
        f"  <url><loc>{{base}}/{'' if name == 'index.html' else name}</loc>"
        f"<lastmod>{today}</lastmod></url>"
        for _route, name in pages
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )


def _worker_file(
    backend_host: str = "1tsrajowo-meow.hf.space",
    console_host: str = "dacatowner.pages.dev",
) -> str:
    """Cloudflare Pages Advanced Mode Worker (_worker.js).

    Reverse-proxies dynamic bot/dashboard routes to Render while keeping
    the visitor on the Cloudflare domain — and hands the one path that belongs
    to another host over to it, rather than serving a second door to it here.
    """
    return f"""/**
 * Cloudflare Pages Advanced Mode Worker (_worker.js)
 * Reverse-proxies dynamic routes to the backend on Render
 * so visitors stay on the Cloudflare domain.
 */
const BACKEND_HOST = "{backend_host}";

/* The operator console lives on a host of its own, and this site hands it over
   rather than rendering it.

   `/owner` used to be a dynamic prefix here, which meant the fleet console —
   every server, the economy ledgers, the audit trail, and the God tier behind
   its own passkey — was served **under this site's own marketing header**, on
   the members' domain, wearing a nav bar offering "Add to Discord". Two things
   were wrong with that and both were reported from a browser: it made the
   operator's console look like a page of the public site (so "is this the
   dashboard?" had two answers), and it made the member-facing domain the one
   address where the fleet's controls are reachable at all.

   A 301 and not a proxy, because the other host is the one built for it: it
   serves the console's own bundle, its own stylesheet and its own `robots.txt`,
   and its worker answers nothing else. This is the same shape that host already
   uses for the paths that are not its own, pointed the other way. */
const CONSOLE_HOST = "{console_host}";

const DYNAMIC_PREFIXES = [
  "/login",
  "/logout",
  "/dashboard",
  /* Still this worker's business, and no longer proxied: `/owner` is answered
     with a 301 to the console's own host (see the top of `fetch`). It stays in
     this list because the list is *which paths Cloudflare runs the worker for*
     — the same list `_routes.json` is held to — and a path missing from it is
     answered from the published files instead, which is a 404 rather than the
     redirect. */
  "/owner",
  "/servers",
  "/api",
  "/market",
  "/health",
  /* The agreement gate: where every sign-in is sent before it gets anywhere,
     and the one page of the flow that is only ever rendered by Flask. This
     build writes `/terms`, `/privacy` and `/ai` as static pages, so they are
     correctly *not* here — but `/agree` has no static twin, and without this
     line it was served from the CDN, where no such file exists. A member who
     signed in with Discord was therefore redirected to a 404 at the exact
     moment they were first asked to accept the terms, and nothing else could
     ever ask them: the gate is at the door by design, on purpose, so missing
     this path made the clickwrap a page nobody could reach. */
  "/agree",
  /* The hub, which used to answer on a project of its own (meowdev) and now
     answers here. Seven pages rendered by the application from
     `utils/hub.py` — what is running, every command, the deployment, the
     changelog — and one read-only endpoint (`/api/status`, under `/api/*`
     above) that the pages refresh themselves from. */
  "/hub"
];

export default {{
  async fetch(request, env) {{
    const url = new URL(request.url);

    // The console is not this site's to serve. Answered before anything else,
    // so no prefix below can quietly re-admit it — and before the proxying
    // branch, which would otherwise proxy it exactly as it used to.
    if (url.pathname === "/owner" || url.pathname.startsWith("/owner/")) {{
      const target = new URL(request.url);
      target.hostname = CONSOLE_HOST;
      target.protocol = "https:";
      target.port = "";
      return new Response(null, {{
        status: 301,
        headers: {{
          "Location": target.toString(),
          "Cache-Control": "no-store"
        }}
      }});
    }}

    const isDynamic = DYNAMIC_PREFIXES.some(
      (p) => url.pathname === p || url.pathname.startsWith(p + "/")
    );

    if (isDynamic) {{
      const backendUrl = new URL(request.url);
      backendUrl.hostname = BACKEND_HOST;
      backendUrl.protocol = "https:";
      backendUrl.port = "";

      const reqHeaders = new Headers(request.headers);
      reqHeaders.set("Host", BACKEND_HOST);
      reqHeaders.set("X-Forwarded-Host", url.host);
      reqHeaders.set("X-Forwarded-Proto", url.protocol.replace(":", ""));

      const hasBody = !["GET", "HEAD"].includes(request.method);
      const proxyReq = new Request(backendUrl.toString(), {{
        method: request.method,
        headers: reqHeaders,
        body: hasBody ? request.body : undefined,
        redirect: "manual"
      }});

      let response;
      try {{
        response = await fetch(proxyReq);
      }} catch (err) {{
        return new Response("Backend gateway error: " + (err && err.message ? err.message : String(err)), {{
          status: 502,
          headers: {{ "Content-Type": "text/plain; charset=utf-8" }}
        }});
      }}

      const resHeaders = new Headers(response.headers);
      const location = resHeaders.get("Location");
      if (location) {{
        try {{
          const locUrl = new URL(location, backendUrl.origin);
          if (locUrl.hostname === BACKEND_HOST) {{
            resHeaders.set("Location", locUrl.pathname + locUrl.search + locUrl.hash);
          }}
        }} catch {{
          // Relative location, keep as is
        }}
      }}

      return new Response(response.body, {{
        status: response.status,
        statusText: response.statusText,
        headers: resHeaders
      }});
    }}

    return env.ASSETS.fetch(request);
  }}
}};
"""


def _routes_file() -> str:
    """Cloudflare Pages _routes.json defining which paths trigger the Worker.

    This file is the *first* of the two gates a dynamic path has to pass, and it
    is the easier one to forget: Cloudflare decides from this list whether the
    worker runs at all, so a prefix that is in the worker's own
    ``DYNAMIC_PREFIXES`` but missing here never reaches the code that would have
    proxied it — the request is answered from the published directory instead,
    and a path with no published file is a 404 with nothing in any log to say
    why. That is what ``/agree`` did: the clickwrap every sign-in is redirected
    to was proxied by the worker and unreachable anyway, because this list had
    never heard of it. ``tests/test_pages_site.py`` now compares the two lists
    rather than trusting them to match.
    """
    return """{
  "version": 1,
  "include": [
    "/login",
    "/login/*",
    "/logout",
    "/dashboard",
    "/dashboard/*",
    "/owner",
    "/owner/*",
    "/servers",
    "/servers/*",
    "/api/*",
    "/market",
    "/market/*",
    "/health",
    "/agree",
    "/hub",
    "/hub/*"
  ],
  "exclude": [
    "/static/*"
  ]
}
"""


def build(
    out_dir: str,
    live_app: str,
    site_url: str = "",
    *,
    proxy_mode: bool = False,
) -> dict:
    """Render the site into ``out_dir``. Returns a small report."""
    import urllib.parse
    import keep_alive  # noqa: PLC0415 — imported late so ``--help`` stays instant
    from utils import discord_auth  # noqa: PLC0415

    #: The output directory must never be the repository itself.
    #:
    #: This is the line between a website and a leak, and it is not a
    #: hypothetical: the Pages project was left with **no build command and no
    #: output directory**, so Cloudflare published the repository root — and the
    #: build log said so plainly ("No build command specified. Skipping build
    #: step.", then "Uploading... (185/185)", and the repository has exactly 185
    #: tracked files). `bot.py`, `utils/cloud_sync.py`, `cogs/`, `render.yaml`
    #: and `.env.example` were readable at the site's own address until it was
    #: caught. The values were safe — every secret lives in the host's
    #: environment, not in the tree — but the tree was public, and a static host
    #: is told "everything here is published", so a build that points it at the
    #: source is refused here rather than warned about. By the time the upload
    #: runs it is too late to take back.
    if os.path.realpath(out_dir) == os.path.realpath(ROOT):
        raise SystemExit(
            "--out must not be the repository root. The build writes only the "
            "pages it renders, and a static host handed the repository root "
            "publishes the source tree along with them. Use the default "
            "(_site) or any directory that is not the checkout."
        )

    client = keep_alive.app.test_client()
    # The output directory is emptied first, so the artifact is exactly what this
    # build produced. Without it a **retired page survives** in the directory and
    # is uploaded again by every deploy — found by building this tree and finding
    # `music.html` still in it, a page describing *"a live queue with real-time
    # audio effects"* that the product stopped having, sitting beside the
    # `_redirects` rule that is supposed to answer `/music` with the new page. A
    # static host is told "everything in here is published", so a file nobody
    # meant to publish is published: the directory has to be the build's own
    # output rather than a place the build writes into.
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    report: dict = {"pages": [], "leftovers": [], "invite": bool(discord_auth.client_id())}

    for route, filename in PAGES:
        response = client.get(route)
        if response.status_code != 200:
            raise SystemExit(
                f"{route} answered {response.status_code} while building {filename} — "
                "the site would ship a broken page."
            )
        body = response.get_data(as_text=True)
        body = rewrite_html(body, live_app, proxy_mode=proxy_mode)
        leftovers = _clean_targets(body, live_app, proxy_mode=proxy_mode)
        if leftovers:
            report["leftovers"].append((filename, sorted(set(leftovers))))
        target = os.path.join(out_dir, filename)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(body)
        report["pages"].append((route, filename, len(body)))

    # The stylesheet's ``url()`` values are relative to the stylesheet, and the
    # cat cursor lives in one: a missed rewrite costs the cursor silently.
    static_src = os.path.join(ROOT, "static")
    static_dst = os.path.join(out_dir, "static")
    shutil.copytree(static_src, static_dst, dirs_exist_ok=True)
    for name in CSS_ASSETS:
        path = os.path.join(static_dst, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            css = handle.read()
        css = rewrite_css(css, live_app)
        leftovers = _clean_targets(css, live_app, proxy_mode=proxy_mode)
        if leftovers:
            report["leftovers"].append((name, sorted(set(leftovers))))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(css)

    # GitHub Pages runs Jekyll unless told not to, and Jekyll silently drops
    # files and folders whose names begin with an underscore.
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    # Host-specific config, both harmless where they are not read: `_headers` and
    # `_redirects` are Cloudflare Pages' own files, and the `.nojekyll` above is
    # GitHub Pages'. Emitting all three keeps the same directory deployable to
    # either without a wrapper script per host.
    with open(os.path.join(out_dir, "_headers"), "w", encoding="utf-8") as handle:
        handle.write(_headers_file())
    with open(os.path.join(out_dir, "_redirects"), "w", encoding="utf-8") as handle:
        handle.write(_redirects_file())
    # Cloudflare Pages Advanced Mode worker and routing config for reverse-proxying
    backend_host = urllib.parse.urlsplit(live_app).netloc or "1tsrajowo-meow.hf.space"
    with open(os.path.join(out_dir, "_worker.js"), "w", encoding="utf-8") as handle:
        handle.write(_worker_file(backend_host))
    with open(os.path.join(out_dir, "_routes.json"), "w", encoding="utf-8") as handle:
        handle.write(_routes_file())
    # The 404 page comes from the app, fetched the same way the pages above are,
    # so a mistyped address reads the same on both hosts. A route that suddenly
    # answers 200 for this path would mean the site's error page had moved and
    # this file was about to publish something else entirely, so it is refused
    # rather than written.
    missing = client.get(NOT_FOUND_PROBE)
    if missing.status_code != 404:
        raise SystemExit(
            f"{NOT_FOUND_PROBE} answered {missing.status_code} — the app serves the "
            "site's 404 page and this build cannot invent one."
        )
    body = rewrite_html(missing.get_data(as_text=True), live_app, proxy_mode=proxy_mode)
    leftovers = _clean_targets(body, live_app, proxy_mode=proxy_mode)
    if leftovers:
        report["leftovers"].append(("404.html", sorted(set(leftovers))))
    with open(os.path.join(out_dir, "404.html"), "w", encoding="utf-8") as handle:
        handle.write(body)
    # The directives, from the one module that writes them — the same call the
    # app serves at /robots.txt and the hub and owner builds make, so a crawler
    # cannot get a different answer depending on which host it happened to ask.
    # The sitemap line is the only thing this host adds, and it is this host's.
    sitemap = f"{site_url.rstrip('/')}/sitemap.xml" if site_url else ""
    with open(os.path.join(out_dir, "robots.txt"), "w", encoding="utf-8") as handle:
        handle.write(ai_guard.robots_txt(sitemap=sitemap))
    # The machine-readable half of the same policy: training use reserved
    # against, in the convention crawlers look for. Served as a file here and as
    # a route by the app, from one function.
    with open(os.path.join(out_dir, "ai.txt"), "w", encoding="utf-8") as handle:
        handle.write(ai_guard.ai_txt())
    if site_url:
        with open(os.path.join(out_dir, "sitemap.xml"), "w", encoding="utf-8") as handle:
            handle.write(_sitemap(PAGES, live_app).replace("{base}", site_url.rstrip("/")))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=os.path.join(ROOT, "_site"))
    parser.add_argument(
        "--live-url",
        default=os.environ.get("LIVE_APP_URL", DEFAULT_LIVE_APP),
        help="where the dynamic pages live (default: the Render service)",
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("DISCORD_CLIENT_ID", PUBLIC_CLIENT_ID),
        help="Discord application id, used to build the invite link",
    )
    parser.add_argument(
        "--site-url",
        default=os.environ.get("SITE_URL", ""),
        help="public base URL of the built site, used for sitemap.xml",
    )
    parser.add_argument(
        "--proxy",
        action="store_true",
        default=os.environ.get("CLOUDFLARE_PROXY", "").strip().lower() in ("1", "true", "yes"),
        help="keep dynamic URLs relative so Cloudflare Pages reverse-proxies them",
    )
    args = parser.parse_args()

    # Must happen before ``keep_alive`` is imported: it reads the environment at
    # import time, and the home page asks it whether an invite link exists.
    os.environ["DISCORD_CLIENT_ID"] = args.client_id.strip()
    # Tells the landing page to describe *this* copy honestly: there is no
    # process behind a file, so "starting up" would be a promise that never
    # resolves.
    os.environ["MEOW_STATIC_SITE"] = "1"

    report = build(args.out, args.live_url, args.site_url, proxy_mode=args.proxy)
    for route, filename, size in report["pages"]:
        print(f"  {route:<10} -> {filename}  ({size:,} bytes)")
    if not report["invite"]:
        print(
            "\nNo Discord client id — the build has no 'Add to Discord' button.\n"
            "Pass --client-id or set DISCORD_CLIENT_ID."
        )
        return 1
    if report["leftovers"]:
        print("\nRoot-relative URLs that survived the rewrite (these would 404):")
        for name, urls in report["leftovers"]:
            print(f"  {name}: {', '.join(urls)}")
        return 1
    print(f"\nBuilt into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
