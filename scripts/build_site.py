"""Build the public site as static files, for GitHub Pages.

Why this exists
---------------
The pages the bot serves (``/``, ``/pricing``, ``/privacy``, ``/terms``) are the
ones worth reading *before* you trust a bot with your server, and they were
reachable only from the single Render process that also runs the bot. That host
is a free tier that sleeps, so the front door was shut whenever nobody had
knocked recently. GitHub Pages has no such problem.

Two things make this harder than copying files:

* GitHub Pages serves a *project* site from ``/<repo>/``, so every root-relative
  URL the templates emit — ``/static/meow.css``, ``/pricing``, the ``url()`` of
  the cat cursor inside the stylesheet — points outside the site and 404s. Every
  one of them is rewritten to a relative path here. The cursor is the one that
  would fail *silently*: a missing cursor image falls back to the system arrow
  with nothing in any console to say so.
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

#: Where the real, live app is. Every link that needs a running process points
#: here. It is the Render service's own hostname, which is stable across
#: deploys; a custom domain can replace it later by setting ``LIVE_APP_URL``.
DEFAULT_LIVE_APP = "https://meow-discord-bot.onrender.com"

#: ``route`` -> the file it becomes. Order is the order they are built.
PAGES = (
    ("/", "index.html"),
    ("/pricing", "pricing.html"),
    ("/privacy", "privacy.html"),
    ("/terms", "terms.html"),
)

#: The application's client id. This is *public* — it is in every invite link
#: anybody can read — and it has to be present at build time, because the
#: templates deliberately **omit** the invite button when the id is unknown
#: (a button that goes nowhere is worse than no button). Building without it
#: therefore ships a landing page whose main call to action is missing, which is
#: exactly the state the live site was in. Overridable with ``--client-id`` or
#: ``DISCORD_CLIENT_ID``.
PUBLIC_CLIENT_ID = "1548380269776605324"

#: The stylesheet is rewritten too, because of ``url()``.
CSS_ASSETS = ("meow.css",)

#: ``href|src|action="…"`` — every attribute that can carry a URL.
_ATTR = re.compile(r'\b(href|src|action|poster)\s*=\s*"([^"]*)"', re.IGNORECASE)
#: ``url(…)`` inside CSS, quoted or not.
_CSS_URL = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.IGNORECASE)


def _split_suffix(url: str) -> tuple[str, str]:
    """``/pricing#faq`` -> ``('/pricing', '#faq')``; the query/fragment is kept."""
    for index, char in enumerate(url):
        if char in "#?":
            return url[:index], url[index:]
    return url, ""


def rewrite_url(url: str, live_app: str, *, from_stylesheet: bool = False) -> str:
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

    # 3. Everything else needs the live process: send the visitor there rather
    #    than to a dead relative path. This is the whole class of pages a static
    #    host cannot serve — the dashboard, sign-in, the market, the health line.
    return live_app.rstrip("/") + raw


def rewrite_html(text: str, live_app: str) -> str:
    """Every URL a rendered page carries, rewritten in one pass.

    Inline ``<style>`` blocks get the stylesheet's rule, not the page's — the
    ``url()`` inside one is still resolved against the document's base, so a
    page-level ``static/`` prefix is right there. Only a standalone ``.css``
    file needs the bare form, which is why ``rewrite_css`` exists separately.
    """
    text = _ATTR.sub(
        lambda match: f'{match.group(1)}="{rewrite_url(match.group(2), live_app)}"',
        text,
    )
    return _CSS_URL.sub(
        lambda match: f"url('{rewrite_url(match.group(2), live_app)}')",
        text,
    )


def rewrite_css(text: str, live_app: str) -> str:
    """Asset URLs inside a stylesheet, which are relative to that file."""
    return _CSS_URL.sub(
        lambda match: f"url('{rewrite_url(match.group(2), live_app, from_stylesheet=True)}')",
        text,
    )


def _clean_targets(text: str, live_app: str) -> list[str]:
    """Root-relative URLs that survived rewriting — a defect if non-empty.

    Kept explicit and used as an assertion rather than trusted to the regexes:
    an absolute ``/static/`` path that slips through is a link that 404s on
    Pages, which is exactly the failure this whole file exists to prevent.
    """
    leftovers: list[str] = []
    for match in _ATTR.finditer(text):
        value = match.group(2)
        if value.startswith("/") and not value.startswith("//"):
            leftovers.append(value)
    for match in _CSS_URL.finditer(text):
        value = match.group(2)
        if value.startswith("/") and not value.startswith("//"):
            leftovers.append(value)
    return leftovers


def _not_found_page(live_app: str) -> str:
    """A 404 that belongs to the site instead of GitHub's bare default."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="robots" content="noindex">
    <title>Page not found</title>
    <link rel="icon" type="image/svg+xml" href="static/favicon.svg">
    <link rel="stylesheet" href="static/meow.css">
</head>
<body class="page-404">
    <div class="aurora" aria-hidden="true"><span class="grid"></span></div>
    <main class="page error-page">
        <img class="brand-logo brand-logo-lg" src="static/logo.svg" alt="" aria-hidden="true">
        <h1>That page wandered off</h1>
        <p class="lede">
            There is nothing at this address. The bot itself is probably fine —
            it is just not here.
        </p>
        <div class="hero-cta">
            <a class="btn btn-primary btn-lg" href="./">Back to the front page</a>
            <a class="btn btn-lg" href="{live_app.rstrip('/')}/dashboard">Open the dashboard</a>
        </div>
    </main>
</body>
</html>
"""


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


def build(out_dir: str, live_app: str, site_url: str = "") -> dict:
    """Render the site into ``out_dir``. Returns a small report."""
    import keep_alive  # noqa: PLC0415 — imported late so ``--help`` stays instant
    from utils import discord_auth  # noqa: PLC0415

    client = keep_alive.app.test_client()
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
        body = rewrite_html(body, live_app)
        leftovers = _clean_targets(body, live_app)
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
        leftovers = _clean_targets(css, live_app)
        if leftovers:
            report["leftovers"].append((name, sorted(set(leftovers))))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(css)

    # GitHub Pages runs Jekyll unless told not to, and Jekyll silently drops
    # files and folders whose names begin with an underscore.
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    with open(os.path.join(out_dir, "404.html"), "w", encoding="utf-8") as handle:
        handle.write(_not_found_page(live_app))
    with open(os.path.join(out_dir, "robots.txt"), "w", encoding="utf-8") as handle:
        handle.write("User-agent: *\nAllow: /\n")
    if site_url:
        with open(os.path.join(out_dir, "sitemap.xml"), "w", encoding="utf-8") as handle:
            handle.write(_sitemap(PAGES, live_app).replace("{base}", site_url.rstrip("/")))
        with open(os.path.join(out_dir, "robots.txt"), "a", encoding="utf-8") as handle:
            handle.write(f"Sitemap: {site_url.rstrip('/')}/sitemap.xml\n")
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
    args = parser.parse_args()

    # Must happen before ``keep_alive`` is imported: it reads the environment at
    # import time, and the home page asks it whether an invite link exists.
    os.environ["DISCORD_CLIENT_ID"] = args.client_id.strip()
    # Tells the landing page to describe *this* copy honestly: there is no
    # process behind a file, so "starting up" would be a promise that never
    # resolves.
    os.environ["MEOW_STATIC_SITE"] = "1"

    report = build(args.out, args.live_url, args.site_url)
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
