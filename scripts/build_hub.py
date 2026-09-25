"""Build the hub — the public map of the bot — as static files.

Why a second site
-----------------
The marketing site answers "should I install this". The hub answers a different
question, asked by a different person: "what is this thing doing, and where".
Those are not the same page and trying to be both is how the front door became a
wall of text. So the hub is its own site, on its own hostname, with its own
stylesheet — and it is deliberately **read-only**, because a static host is a
host anybody can read:

* It has no database, no session and no credential of any kind.
* Its one network call is a GET to a path that is already public, and the worker
  it ships refuses every other method and every other path.
* Every page that would need an action behind it hands off to the console on the
  bot's own process, which asks who you are first.

Why it renders through the real Flask app
-----------------------------------------
Same reasoning as ``scripts/build_site.py``: the page a visitor reads on the CDN
and the page the app serves are the same template with the same context, so the
two cannot drift into disagreeing about the product. The build is a *publish* of
the app's own output, not a second implementation of it.

What this file has to get right, and why each one is a test
-----------------------------------------------------------
* **Every root-relative URL is rewritten.** A static host serves the hub from the
  root of its own hostname today and could be put behind a subpath tomorrow; a
  surviving ``/static/hub.css`` is a 404 either way, and the build fails on one.
* **No inline script and no `style` attribute.** The hub is published under
  ``script-src 'self'`` with no ``'unsafe-inline'``, which is only possible
  because the pages carry neither. A template that gained one would render a
  page that is subtly broken in production and fine in development — so the build
  refuses it instead.
* **The output directory is never the repository.** This is not hypothetical:
  the marketing project was once left with no build command, so Cloudflare
  published the repository root and ``bot.py`` was readable at the site's own
  address. A static host is told "everything here is published", so a build that
  points one at the source is refused here rather than warned about.

Usage::

    python scripts/build_hub.py                # -> _hub/
    python scripts/build_hub.py --out /tmp/x
    LIVE_APP_URL=https://example.test python scripts/build_hub.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import build_site  # noqa: E402 — reuses the URL rewriters
from utils import ai_guard, brand  # noqa: E402 — needs ROOT on the path first

#: Where the real, live app is. Every link that needs a running process points
#: here: the console, the legal pages, the health line.
DEFAULT_LIVE_APP = build_site.DEFAULT_LIVE_APP

#: The hub's public address, used only to write sitemap.xml. The hub has no
#: hostname of its own any more — its pages are proxied by the public site's
#: worker — so the address a sitemap would name is the site's, under `/hub`.
DEFAULT_HUB_URL = "https://dacat.pages.dev/hub"

#: ``route`` -> the file it becomes. The routes come from ``utils.hub`` so the
#: rail, the Flask app, the command palette and this builder all read one list.
from utils import hub  # noqa: E402 — imported late so ``--help`` stays instant

PAGES = tuple(
    (path, "index.html" if page == "overview" else f"{page}.html")
    for page, _label, path, _icon, _group, _blurb in hub.HUB_PAGES
)

#: The app path that serves the palette's index, and where the static build puts
#: it. Folded under ``static/`` so it inherits the immutable cache rule beside
#: the stylesheet rather than being re-fetched by every palette open.
INDEX_ROUTE = "/hub/index.json"
INDEX_FILE = "static/hub-index.json"

#: URLs in the *page* that the build is allowed to leave root-relative, because
#: the hub's own worker answers them on its own host. This is the whole
#: difference between the hub's worker and the marketing site's: that one proxies
#: the console, and this one proxies one read-only endpoint and nothing else.
PROXIED = hub.HUB_PROXY_ROUTES

#: What a strict ``script-src 'self'`` / ``style-src 'self'`` actually blocks,
#: and only that: an inline ``<script>`` (one with no ``src``), a ``<style>``
#: block, and a ``style`` attribute. A plain ``<script src=…>`` is fine and is
#: how the hub loads its one file, so matching ``<script`` alone would fail the
#: build on the correct markup.
_INLINE_BANS = (
    ("inline <script>", re.compile(r"<script\b(?![^>]*\bsrc\s*=)", re.IGNORECASE)),
    ("<style> block", re.compile(r"<style\b", re.IGNORECASE)),
    ("style attribute", re.compile(r"[\s\"']style\s*=\s*[\"']", re.IGNORECASE)),
)


def rewrite_url(url: str, live_app: str, *, from_stylesheet: bool = False) -> str:
    """One URL, made correct for a static hub served from a subpath.

    Order matters, and it is the order of specificity: the hub's own assets, then
    the hub's own pages, then the one proxied endpoint, and finally everything
    else — which by elimination is a page only the live process can answer, so it
    is sent there rather than to a dead relative path.
    """
    raw = (url or "").strip()
    if not raw:
        return url
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://", "//", "mailto:", "data:", "javascript:")):
        return url
    if raw.startswith("#") or not raw.startswith("/"):
        # Already relative, or a fragment: both are correct wherever they land.
        return url

    path, suffix = build_site._split_suffix(raw)

    # 1. The hub's own assets: /hub/static/x -> static/x.
    if path.startswith("/hub/static/"):
        return "static/" + path[len("/hub/static/"):] + suffix

    # 2. The app's shared assets. The stylesheet is the caller for these when it
    #    is a standalone file, which is why `from_stylesheet` decides the prefix
    #    — the same trap the marketing build documents at length.
    if path.startswith("/static/"):
        tail = path[len("/static/"):]
        return (tail if from_stylesheet else "static/" + tail) + suffix

    # 3. The palette's index, which is a file here.
    if path == INDEX_ROUTE:
        return INDEX_FILE + suffix

    # 4. The hub's own pages.
    for route, filename in PAGES:
        if path == route:
            return ("./" if filename == "index.html" else filename) + suffix

    # 5. The one endpoint the hub's own worker proxies, so a visitor stays on the
    #    hub's domain and the reading is same-origin.
    if path in PROXIED:
        return raw

    # 6. Everything else needs the live process.
    return live_app.rstrip("/") + raw


def _rewrite_html(text: str, live_app: str) -> str:
    """Every URL a rendered page carries, in one pass.

    ``data-index`` is included alongside the standard attributes because the
    command palette's index is fetched by script rather than requested by the
    browser, so a missed rewrite there is a palette that silently never opens —
    which is exactly the class of failure this build exists to prevent.
    """
    patterns = (
        build_site._ATTR,
        # Attributes a *script* reads rather than the browser requesting, so a
        # missed rewrite is not a 404 anyone sees in a network tab: it is a
        # palette that never opens and a hub that never refreshes.
        re.compile(r'\b(data-index|data-status-url)\s*=\s*"([^"]*)"', re.IGNORECASE),
    )
    for pattern in patterns:
        text = pattern.sub(
            lambda match: f'{match.group(1)}="{rewrite_url(match.group(2), live_app)}"',
            text,
        )
    return build_site._CSS_URL.sub(
        lambda match: f"url('{rewrite_url(match.group(2), live_app)}')", text
    )


def _leftovers(text: str) -> list[str]:
    """Root-relative URLs that survived — a defect if non-empty.

    Asserted rather than trusted to the rewriter, because the failure mode is a
    link that 404s on a host we do not control and cannot see.
    """
    found: list[str] = []
    for match in build_site._ATTR.finditer(text):
        value = match.group(2)
        if value.startswith("/") and not value.startswith("//"):
            path, _ = build_site._split_suffix(value)
            if path in PROXIED:
                continue
            found.append(value)
    for match in build_site._CSS_URL.finditer(text):
        value = match.group(2)
        if value.startswith("/") and not value.startswith("//"):
            found.append(value)
    return found


def _inline_violations(text: str) -> list[str]:
    """Things the hub's own policy blocks, refused before they can ship.

    Checked against the *rendered* page rather than against the templates, so a
    macro, an include and a data attribute that interpolates markup are all
    caught the same way. This is the one failure in this build that is invisible
    where it happens and real in production: a blocked inline script is a
    console error and a page that mostly works.
    """
    hits: list[str] = []
    for label, pattern in _INLINE_BANS:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 40)
            hits.append(f"{label}: …{text[start:match.end() + 40].replace(chr(10), ' ')}…")
    return hits


#: The hub's own security headers, generated from the one policy both hosts
#: share. `_headers` is Cloudflare Pages' format: a path pattern, then indented
#: `Name: value` lines.
def _headers_file() -> str:
    lines = ["/*"]
    for name, value in hub.SECURITY_HEADERS:
        lines.append(f"  {name}: {value}")
    # NOT `no-store`: that is right for the console and wrong for a public page
    # whose whole job is to be readable promptly by a stranger. Short and
    # revalidated, so a deploy lands within minutes while a repeat visit is
    # instant.
    lines.append("  Cache-Control: public, max-age=300, must-revalidate")
    lines.append("")
    lines.append("/static/*")
    lines.append("  Cache-Control: public, max-age=31536000, immutable")
    lines.append("")
    return "\n".join(lines)


def _redirects_file() -> str:
    """Cloudflare Pages handles extensionless clean URLs natively.

    Writing an explicit ``/systems`` → ``systems.html`` rule would be a 308 loop
    against that same rule, which is how the marketing site lost an afternoon.
    """
    return (
        "# Cloudflare Pages serves clean extensionless URLs natively"
        " (e.g. /systems -> systems.html).\n"
        "# Explicit 200 rewrites to .html files are omitted to avoid circular 308 loops.\n"
    )


def _worker_file(backend_host: str) -> str:
    """Cloudflare Pages Advanced Mode Worker: read-only by construction.

    Two gates, and both are load-bearing:

    * **Only GET and HEAD.** A public CDN host must not be able to become a write
      path to the bot, so a POST is answered here rather than proxied — and the
      worker is where that is enforceable, because it is the only code this host
      runs.
    * **One path, from an allow-list.** ``/api/status`` is a merge of two
      endpoints that are already public. Everything else is served from the
      static assets, so a new API route on the backend is not reachable from here
      by accident — which is the difference between an allow-list and a proxy.

    Cookie and Authorization headers are dropped on the way out: this endpoint
    answers the same thing to everybody, and forwarding a credential from a
    public host is a habit with no upside.
    """
    allowed = ", ".join(json.dumps(path) for path in PROXIED)
    return f"""/** Meow Hub — a read-only front door.
 *
 * Generated by scripts/build_hub.py. Its whole job is to answer one small
 * public reading from the bot's own host, so that a *static* page can still
 * show live numbers without a credential on a public CDN.
 *
 * Read-only is enforced here rather than documented: GET and HEAD only, one
 * path from an allow-list, and no inbound credentials forwarded.
 */
const BACKEND_HOST = {json.dumps(backend_host)};
const ALLOWED = [{allowed}];

export default {{
  async fetch(request, env) {{
    const url = new URL(request.url);
    const method = request.method.toUpperCase();

    if (!ALLOWED.includes(url.pathname)) {{
      return env.ASSETS.fetch(request);
    }}

    if (method !== "GET" && method !== "HEAD") {{
      return new Response("This host is read-only.\\n", {{
        status: 405,
        headers: {{
          "Content-Type": "text/plain; charset=utf-8",
          "Allow": "GET, HEAD",
          "Cache-Control": "no-store"
        }}
      }});
    }}

    const backendUrl = new URL(request.url);
    backendUrl.hostname = BACKEND_HOST;
    backendUrl.protocol = "https:";
    backendUrl.port = "";

    const outboundHeaders = new Headers();
    outboundHeaders.set("Accept", "application/json");
    outboundHeaders.set("Host", BACKEND_HOST);
    outboundHeaders.set("X-Forwarded-Host", url.host);
    outboundHeaders.set("X-Forwarded-Proto", url.protocol.replace(":", ""));

    let upstream;
    try {{
      upstream = await fetch(backendUrl.toString(), {{
        method,
        headers: outboundHeaders,
        redirect: "manual"
      }});
    }} catch (error) {{
      // A sleeping free-tier host is the common case, so the wording says what
      // happened rather than implying the hub is broken.
      return new Response(
        JSON.stringify({{ ok: false, reason: "the bot's host did not answer" }}),
        {{
          status: 503,
          headers: {{
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store"
          }}
        }}
      );
    }}

    const headers = new Headers();
    headers.set("Content-Type", upstream.headers.get("Content-Type") || "application/json; charset=utf-8");
    // Thirty seconds, matching the page's own poll: a burst of visitors during a
    // deploy is one request to the bot, not one per tab.
    headers.set("Cache-Control", "public, max-age=30");
    headers.set("X-Content-Type-Options", "nosniff");
    return new Response(upstream.body, {{ status: upstream.status, headers }});
  }}
}};
"""


def _routes_file() -> str:
    """Which paths reach the worker. Everything else is a static asset."""
    return json.dumps({
        "version": 1,
        "include": [f"{path}*" if path.endswith("/") else path for path in PROXIED],
        "exclude": ["/static/*"],
    }, indent=2) + "\n"


def _sitemap(hub_url: str, built: list[tuple[str, str]]) -> str:
    """Only the pages that exist here — a sitemap naming 404s is worse than none."""
    import datetime

    today = datetime.date.today().isoformat()
    base = hub_url.rstrip("/")
    urls = "\n".join(
        f"  <url><loc>{base}/{'' if filename == 'index.html' else filename[:-5]}</loc>"
        f"<lastmod>{today}</lastmod></url>"
        for _route, filename in built
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )


def _not_found(hub_url: str, live_app: str, asset_version: str) -> str:
    """A 404 that belongs to the hub instead of the host's own.

    The title is read from `utils.brand` rather than typed, because it was typed:
    every other page of this hub says *“· {name} Hub”* through the base template
    while this one — the page a visitor only ever sees after a mistake — kept the
    name the product had before the rebrand, and no law reads a string this file
    writes by hand.
    """
    hub_name = f"{brand.NAME} Hub"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="robots" content="noindex">
    <title>Not here · {hub_name}</title>
    <link rel="icon" type="image/svg+xml" href="static/favicon.svg?v={asset_version}">
    <link rel="stylesheet" href="static/hub.css?v={asset_version}">
</head>
<body>
<div class="aurora" aria-hidden="true"><span class="mesh"></span></div>
<div class="app">
    <div class="main">
        <main class="container" id="content">
            <section class="hero">
                <div>
                    <p class="eyebrow">404</p>
                    <h2>This hub has no page there</h2>
                    <p class="lede">
                        The address is wrong, or the page moved. The hub's own pages
                        are listed on the left of every page it does have, and the
                        bot itself is probably fine — it is just not here.
                    </p>
                    <div class="hero-facts">
                        <a class="btn btn-primary" href="./">Back to the overview</a>
                        <a class="btn" href="{live_app.rstrip('/')}/dashboard">Open the console</a>
                    </div>
                </div>
            </section>
        </main>
    </div>
</div>
</body>
</html>
"""


def build(out_dir: str, live_app: str, hub_url: str = "") -> dict:
    """Render the hub into ``out_dir``. Returns a small report."""
    import keep_alive  # noqa: PLC0415 — imported late so ``--help`` stays instant

    #: The output directory must never be the repository itself. See the module
    #: docstring: this is the line between a website and a leak, and it has
    #: already been crossed once on the sibling project.
    if os.path.realpath(out_dir) == os.path.realpath(ROOT):
        raise SystemExit(
            "--out must not be the repository root. A static host handed the "
            "repository root publishes the source tree along with the pages — "
            "bot.py was readable at the site's own address the last time this "
            "happened. Use the default (_hub) or any directory outside the "
            "checkout."
        )

    client = keep_alive.app.test_client()
    os.makedirs(out_dir, exist_ok=True)
    report: dict = {
        "pages": [], "leftovers": [], "inline": [], "invite": bool(hub.marketing_url()),
    }

    for route, filename in PAGES:
        response = client.get(route)
        if response.status_code != 200:
            raise SystemExit(
                f"{route} answered {response.status_code} while building {filename} — "
                "the hub would ship a broken page."
            )
        body = response.get_data(as_text=True)

        violations = _inline_violations(body)
        if violations:
            report["inline"].append((filename, violations[:2]))

        body = _rewrite_html(body, live_app)
        leftovers = _leftovers(body)
        if leftovers:
            report["leftovers"].append((filename, sorted(set(leftovers))))

        with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as handle:
            handle.write(body)
        report["pages"].append((route, filename, len(body)))

    static_dst = os.path.join(out_dir, "static")
    os.makedirs(static_dst, exist_ok=True)

    # The palette's index, built from the same functions the live route uses, so
    # the static hub can search exactly what the served one shows.
    with open(os.path.join(static_dst, "hub-index.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {"entries": hub.search_index(hub.systems_payload(), hub.commands_payload())},
            handle, separators=(",", ":"),
        )

    # The hub's own assets, then only the shared ones its pages actually use.
    # Copying all of `static/` would ship an 80 KB stylesheet belonging to another
    # surface to every visitor of a page that never references it.
    shutil.copytree(os.path.join(ROOT, "hub", "static"), static_dst, dirs_exist_ok=True)
    for name in hub.SHARED_ASSETS:
        source = os.path.join(ROOT, "static", name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(static_dst, name))
            report.setdefault("assets", []).append(name)
        else:
            raise SystemExit(
                f"the hub references static/{name} and it is not in the repository"
            )

    # The stylesheet's `url()` values resolve against the stylesheet, so the
    # cursor — which lives in a `url()` — needs the stylesheet's own form.
    css_path = os.path.join(static_dst, "hub.css")
    with open(css_path, "r", encoding="utf-8") as handle:
        css = handle.read()
    css = build_site.rewrite_css(css, live_app)
    leftovers = _leftovers(css)
    if leftovers:
        report["leftovers"].append(("hub.css", sorted(set(leftovers))))
    with open(css_path, "w", encoding="utf-8") as handle:
        handle.write(css)

    backend_host = urllib.parse.urlsplit(live_app).netloc or "1tsrajowo-meow.hf.space"
    files = {
        # GitHub Pages runs Jekyll unless told not to, and Jekyll silently drops
        # names beginning with an underscore — which is every file below.
        ".nojekyll": "",
        "_headers": _headers_file(),
        "_redirects": _redirects_file(),
        "_worker.js": _worker_file(backend_host),
        "_routes.json": _routes_file(),
        "404.html": _not_found(hub_url, live_app, hub.asset_version()),
        # The directives and the training prohibition, from ``utils/ai_guard`` —
        # the same two functions the app serves at /robots.txt and /ai.txt and
        # the marketing build writes. The hub is the *documentation* surface, so
        # it is the one a data collector would most like to ingest; leaving it
        # with the literal `Allow: /` it used to carry would have made this the
        # host where the whole posture was missing.
        "robots.txt": ai_guard.robots_txt(
            sitemap=f"{hub_url.rstrip('/')}/sitemap.xml" if hub_url else ""
        ),
        "ai.txt": ai_guard.ai_txt(),
    }
    for name, content in files.items():
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as handle:
            handle.write(content)

    if hub_url:
        with open(os.path.join(out_dir, "sitemap.xml"), "w", encoding="utf-8") as handle:
            handle.write(_sitemap(hub_url, PAGES))

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=os.path.join(ROOT, "_hub"))
    parser.add_argument(
        "--live-url",
        default=os.environ.get("LIVE_APP_URL", DEFAULT_LIVE_APP),
        help="where the console and the API live (default: the Render service)",
    )
    parser.add_argument(
        "--hub-url",
        default=os.environ.get("HUB_URL", DEFAULT_HUB_URL),
        help="the hub's own public address, used only for sitemap.xml",
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("DISCORD_CLIENT_ID", build_site.PUBLIC_CLIENT_ID),
        help="Discord application id, used to build the invite link",
    )
    args = parser.parse_args()

    # Must happen before ``keep_alive`` is imported: it reads the environment at
    # import time, and the pages ask it whether an invite link exists.
    os.environ["DISCORD_CLIENT_ID"] = args.client_id.strip()
    # Tells the hub to describe *this* copy honestly. There is no process behind
    # a file, so it cannot read its own environment and call that the
    # deployment's — a build runner's `os.environ` is somebody else's machine.
    os.environ["MEOW_STATIC_SITE"] = "1"

    report = build(args.out, args.live_url, args.hub_url)
    for route, filename, size in report["pages"]:
        print(f"  {route:<16} -> {filename:<16} ({size:,} bytes)")

    if report["inline"]:
        print("\nInline script or a style attribute in a rendered page. The hub is")
        print("published under `script-src 'self'` with no `'unsafe-inline'`, so")
        print("these would be blocked in production and fine in development:")
        for name, hits in report["inline"]:
            for hit in hits:
                print(f"  {name}: …{hit}…")
        return 1
    if report["leftovers"]:
        print("\nRoot-relative URLs that survived the rewrite (these would 404):")
        for name, urls in report["leftovers"]:
            print(f"  {name}: {', '.join(urls)}")
        return 1
    if not report["invite"]:
        print("\nNo public site configured, so the hub offers no invite link.")
    print(f"\nBuilt into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
