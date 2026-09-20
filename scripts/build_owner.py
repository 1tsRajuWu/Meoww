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
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_LIVE_APP = "https://meow-discord-bot.onrender.com"
DEFAULT_OUT = os.path.join(ROOT, "_owner")

STATIC_ASSETS = [
    "meow.css",
    "theme.js",
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


def _worker_file(backend_host: str = "meow-discord-bot.onrender.com") -> str:
    return f"""/**
 * Cloudflare Pages Advanced Mode Worker for meow-owner.pages.dev
 * Dedicated Owner Console reverse proxy.
 */
const BACKEND_HOST = "{backend_host}";

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
    with open(os.path.join(out_dir, "_worker.js"), "w", encoding="utf-8") as f:
        f.write(_worker_file(backend_host))

    with open(os.path.join(out_dir, "_routes.json"), "w", encoding="utf-8") as f:
        f.write(_routes_file())

    with open(os.path.join(out_dir, "_headers"), "w", encoding="utf-8") as f:
        f.write(_headers_file())

    print(f"Built meow-owner site into {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the owner console site.")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output directory")
    parser.add_argument("--live-app", default=DEFAULT_LIVE_APP, help="Live app URL")
    args = parser.parse_args()
    build(args.out, args.live_app)


if __name__ == "__main__":
    main()
