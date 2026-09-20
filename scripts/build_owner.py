"""Build the dedicated owner console site for Cloudflare Pages (meow-owner).

Why a dedicated owner site
--------------------------
The public site (meow-bot.pages.dev) is for community members and server admins.
The operator console (meow-owner.pages.dev) is exclusively for the bot owner to
manage the whole fleet, system toggles, ops telemetry, and changes.

Having a separate site allows:
1. Complete removal of the "Admin Passkey" field from the public /login page.
2. A single, dedicated portal for the owner at https://meow-owner.pages.dev.
3. Edge-caching of all static console bundles while proxying API calls to the backend.

Usage::

    python scripts/build_owner.py
    python scripts/build_owner.py --out _owner
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_LIVE_APP = "https://meow-discord-bot.onrender.com"
DEFAULT_OUT = os.path.join(ROOT, "_owner")

#: Every file under ``static/`` that a page *this host serves* asks for by name.
#:
#: The list is a contract with the templates rather than a wish: this site is a
#: reverse proxy, so a page rendered by the backend arrives here referring to
#: ``/static/...`` — and ``/static/`` is the one path the worker answers from the
#: CDN instead of forwarding. A file that is not in this list is therefore a
#: **404 on the owner host and nowhere else**, which is exactly how the per-server
#: dashboard came to render as an unstyled heading with one black rectangle on
#: it: `dashboard.css`, `motion.js` and `gateway.js` were missing here while
#: answering correctly on Render. `tests/test_owner_site.py` reads the templates
#: for their own asset urls and fails if one of them is not copied.
STATIC_ASSETS = [
    "meow.css",
    "dashboard.css",
    "theme.js",
    "motion.js",
    "gateway.js",
    "figures.js",
    "logo.svg",
    "logo-sakura.svg",
    "logo-forest.svg",
    "logo-ember.svg",
    "favicon.svg",
    "favicon-sakura.svg",
    "favicon-forest.svg",
    "favicon-ember.svg",
    "cursor-default.svg",
    "cursor-pointer.svg",
]


def _js_string(value: str) -> str:
    """A JS string literal, for a value that came from the environment.

    ``json.dumps`` is exactly that: quoted, with quotes and backslashes escaped.
    It matters for the proxy secret — one stray quote in it would end the
    literal and leave the rest of the value as source.
    """
    return json.dumps(value)


def _worker_file(
    backend_host: str = "meow-discord-bot.onrender.com", proxy_secret: str = ""
) -> str:
    return f"""/**
 * Cloudflare Pages Advanced Mode Worker for meow-owner.pages.dev
 * Dedicated Owner Console reverse proxy.
 */
const BACKEND_HOST = "{backend_host}";

// Shared with the backend's OWNER_PROXY_SECRET. Empty means no stamp is sent,
// and the backend then falls back to the address Cloudflare connected from.
const OWNER_PROXY_SECRET = {_js_string(proxy_secret)};

export default {{
  async fetch(request, env) {{
    const url = new URL(request.url);

    // Static assets are served directly from Cloudflare Pages CDN
    if (url.pathname.startsWith("/static/")) {{
      return env.ASSETS.fetch(request);
    }}

    // Proxy everything else to the Render backend
    const backendUrl = new URL(request.url);
    backendUrl.hostname = BACKEND_HOST;
    backendUrl.protocol = "https:";
    backendUrl.port = "";

    // Map root / or /index.html on meow-owner.pages.dev directly to backend /owner
    if (url.pathname === "/" || url.pathname === "/index.html") {{
      backendUrl.pathname = "/owner";
    }}

    // The visitor's headers, minus the ones that describe a *hop*. A client that
    // sends its own X-Forwarded-For is describing a chain it is not part of, and
    // downstream nothing can tell those from the addresses a proxy appended —
    // which is how a five-attempt sign-in limit becomes an unlimited one. They
    // are dropped here and replaced below with what this edge actually saw.
    const reqHeaders = new Headers(request.headers);
    for (const hop of [
      "X-Forwarded-For",
      "X-Forwarded-Host",
      "X-Forwarded-Proto",
      "X-Real-IP",
      "Forwarded",
      "CF-Connecting-IP",
      "X-Meow-Client-IP",
      "X-Meow-Proxy",
    ]) {{
      reqHeaders.delete(hop);
    }}
    reqHeaders.set("Host", BACKEND_HOST);
    reqHeaders.set("X-Forwarded-Host", url.host);
    reqHeaders.set("X-Forwarded-Proto", url.protocol.replace(":", ""));

    // *This* is the visitor, as Cloudflare saw them — and it travels with the
    // shared secret because the header alone would be worth nothing. The backend
    // rate-limits sign-ins per address, so any address the visitor can set is a
    // way to choose their own bucket. With the secret it is a claim this worker
    // made rather than one the caller did.
    const visitorIp = request.headers.get("CF-Connecting-IP");
    if (visitorIp) {{
      reqHeaders.set("X-Meow-Client-IP", visitorIp);
    }}
    if (OWNER_PROXY_SECRET) {{
      reqHeaders.set("X-Meow-Proxy", OWNER_PROXY_SECRET);
    }}

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
          let targetPath = locUrl.pathname;
          if (targetPath === "/owner") {{
            targetPath = "/";
          }}
          resHeaders.set("Location", targetPath + locUrl.search + locUrl.hash);
        }}
      }} catch {{
        if (location === "/owner") {{
          resHeaders.set("Location", "/");
        }}
      }}
    }}

    return new Response(response.body, {{
      status: response.status,
      statusText: response.statusText,
      headers: resHeaders
    }});
  }}
}};
"""


def _routes_file() -> str:
    return """{
  "version": 1,
  "include": [
    "/*"
  ],
  "exclude": [
    "/static/*"
  ]
}
"""


def _headers_file() -> str:
    return """/*
  Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Referrer-Policy: no-referrer
  Permissions-Policy: camera=(), microphone=(), geolocation=()
  Strict-Transport-Security: max-age=31536000; includeSubDomains
  Cache-Control: private, no-cache, no-store, must-revalidate

/static/*
  Cache-Control: public, max-age=31536000, immutable
"""


def build(out_dir: str, live_app: str = DEFAULT_LIVE_APP) -> None:
    backend_host = live_app.replace("https://", "").replace("http://", "").split("/")[0]

    if os.path.abspath(out_dir) == ROOT:
        raise ValueError("Cannot write to repository root")

    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    static_out = os.path.join(out_dir, "static")
    os.makedirs(static_out, exist_ok=True)

    # Copy shared assets
    static_src = os.path.join(ROOT, "static")
    for asset in STATIC_ASSETS:
        src = os.path.join(static_src, asset)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(static_out, asset))

    # Copy console bundle
    console_src = os.path.join(static_src, "console")
    console_out = os.path.join(static_out, "console")
    if os.path.exists(console_src):
        shutil.copytree(console_src, console_out, dirs_exist_ok=True)

    # Write worker and configuration files
    proxy_secret = (os.environ.get("OWNER_PROXY_SECRET") or "").strip()
    with open(os.path.join(out_dir, "_worker.js"), "w", encoding="utf-8") as f:
        f.write(_worker_file(backend_host, proxy_secret))

    with open(os.path.join(out_dir, "_routes.json"), "w", encoding="utf-8") as f:
        f.write(_routes_file())

    with open(os.path.join(out_dir, "_headers"), "w", encoding="utf-8") as f:
        f.write(_headers_file())

    print(f"Built meow-owner site into {out_dir}")
    if not proxy_secret:
        # Said out loud because the symptom is invisible: sign-in rate limiting
        # still *works*, it just counts everybody on this host as one visitor.
        print(
            "NOTE: OWNER_PROXY_SECRET is not set, so the worker sends no visitor "
            "address and sign-in limits on this host share one bucket. Set the "
            "same value here and in the backend's environment to separate them."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the owner console site.")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output directory")
    parser.add_argument("--live-app", default=DEFAULT_LIVE_APP, help="Live app URL")
    args = parser.parse_args()
    build(args.out, args.live_app)


if __name__ == "__main__":
    main()
