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

What this build must **not** do is make that host a second copy of the whole
application, twice learned:

* a page it served but could not dress — the per-server dashboard arrived as an
  unstyled heading because this host only has the files listed below, and a
  missing stylesheet reports nothing, anywhere;
* a door nobody was watching — `/login` answered on this domain with its own
  passkey form, so the console was only ever as guarded as the looser of two
  doors into one room.

So the worker below serves the console's own paths and **redirects everything
else to the public site**, which is where those pages belong. The asset contract
shrinks to one directory, and "which host am I on" stops being a question this
project has to answer twice.

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

from utils import ai_guard, brand, config_audit  # noqa: E402  (after sys.path, by design)

DEFAULT_LIVE_APP = "https://1tsrajowo-meow.hf.space"
#: Where this host sends anything that is not the console. The public site is the
#: one place a member-facing page *works*: it is built with its own assets, and
#: its worker forwards the dynamic routes back to the same backend this host
#: proxies. So a bookmark to `/dashboard` here becomes a page that renders,
#: instead of a page this host serves and cannot dress.
DEFAULT_PUBLIC_SITE = "https://meow-bot.pages.dev"
DEFAULT_OUT = os.path.join(ROOT, "_owner")

#: The prefixes this host serves — the same list the worker is built from, kept
#: here so the build's own output can say what the contract is rather than only
#: the JavaScript being able to. ``tests/test_owner_site.py`` reads the worker's
#: list back out of the generated source and compares it with this one, so the
#: two cannot drift into a host that claims one thing and does another.
CONSOLE_PREFIXES = (
    "/owner",
    "/api/console",
    "/api/toggle",
    "/api/toggle-all",
    "/api/command-scope",
    "/api/image",
    # The voice capability probe the Radio panel is built from: whether this
    # deployment has an ffmpeg, an opus encoder and `davey` at all. It is named
    # by the panel that explains it, so it belongs on this host for the same
    # reason /api/console does: an operator reading "cannot play" needs to be
    # able to read *why* without leaving the console that told them.
    "/api/voice-debug",
    "/login/discord",
    "/logout",
    # The agreement, and the reason this list is checked against the *server's*
    # redirects as well as the bundle's own calls.
    #
    # `_door_target` sends a freshly signed-in session to `/agree?next=…` before
    # anything else — the clickwrap is at the door by design — and on this host
    # that path was not served: the post-passkey redirect went to `/agree` here,
    # the worker did not recognise it, and its answer to anything it does not
    # recognise is a 301 to the public site. So the operator entered the right
    # passkey, was signed in on this host, and arrived on *another host* that had
    # never heard of them, at a page offering no passkey field. Found by driving
    # the real sign-in form, not by reading the list: nothing in `console/src/`
    # names `/agree`, because it is the backend that puts it in a `Location`
    # header. See the flow test in `tests/test_owner_site.py`.
    "/agree",
)

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
def _themed_logos() -> list[str]:
    """Every ``logo-<name>.svg`` on disk, so a new one cannot be forgotten.

    No longer read by :data:`STATIC_ASSETS` — the family comes from
    ``utils.brand`` now, which is the same derivation with one author — but kept
    because the archived console's own law imports it by name, and a missing
    function is an import error that takes a whole test file's collection with it
    rather than failing one assertion.

    The file name *is* the theme id, which is what makes this derivable: the console
    asks for ``/static/logo-<theme>.svg``, so anything matching the pattern belongs
    to a theme and must ship with the static build. Sorted for a deterministic list,
    since the build writes a manifest a test compares against.
    """
    static = os.path.join(ROOT, "static")
    return sorted(
        name
        for name in os.listdir(static)
        if name.startswith("logo-") and name.endswith(".svg")
    )


#: for their own asset urls and fails if one of them is not copied.
STATIC_ASSETS = [
    "meow.css",
    # The rebuilt system. The dashboard this host signs into wears the new frame
    # now, so both of its files are copied here for the same reason every other
    # name in this list is: a page that links a stylesheet this build does not
    # copy is a page that renders unstyled on one host, and only on that host.
    "meoww.css",
    "meoww.js",
    # The frame the dashboard wears loads the rail's position read too. The
    # dashboard draws no rail of its own, so the request is wasted there — but a
    # 404 on this host is not, which is what the rest of this list is about.
    "spine.js",
    "pages.css",
    "landing.css",
    "landing.js",
    "anim.js",
    "market.css",
    "market.js",
    "theme.js",
    "motion.js",
    # One family, one author. `utils/brand.py` names every drawing the site and
    # the two static hosts ship, so a mark added to the brand cannot be forgotten
    # here — which is how the console once asked for five logos nobody had drawn.
    # Read rather than restated, because a restated list is the copy that goes
    # stale: this list named four marks drawn for a nine-theme vocabulary for two
    # releases after that vocabulary was gone.
    *brand.ASSETS,
]


def _js_string(value: str) -> str:
    """A JS string literal, for a value that came from the environment.

    ``json.dumps`` is exactly that: quoted, with quotes and backslashes escaped.
    It matters for the proxy secret — one stray quote in it would end the
    literal and leave the rest of the value as source.
    """
    return json.dumps(value)


def _worker_file(
    backend_host: str = "1tsrajowo-meow.hf.space",
    proxy_secret: str = "",
    public_site: str = "meow-bot.pages.dev",
) -> str:
    return f"""/**
 * Cloudflare Pages Advanced Mode Worker for meow-owner.pages.dev
 *
 * The operator's console, and **only** the console.
 *
 * This host used to be a reverse proxy of the entire application: it answered
 * every path the backend does, with one exception (`/static/`, which it served
 * from this project's CDN). That is a shape with two defects built into it, and
 * both were found by using it:
 *
 *   1. Every page rendered by the backend arrives here referring to
 *      `/static/...`, and this host only has the files its build copied. So a
 *      page whose stylesheet was not copied rendered as unstyled markup *on this
 *      host alone* — silently, because a missing stylesheet is not an error
 *      anywhere. That is how the per-server dashboard came back as a heading
 *      over one black rectangle.
 *   2. Every door the backend has was a door here too. `/login` answered 200 on
 *      this domain, with its own passkey form, and the operator's console was
 *      therefore only ever as guarded as the *loosest* of the two doors.
 *
 * The fix is to stop being a second copy of the application. The paths below are
 * the console; everything else is redirected to the public site, which is the
 * host those pages belong to. `/static/` is still answered here — the bundle and
 * the sign-in page's own assets live here — and it is now the *only* asset
 * contract this build has to keep.
 */
const BACKEND_HOST = "{backend_host}";
const PUBLIC_SITE_HOST = "{public_site}";

// Shared with the backend's OWNER_PROXY_SECRET. Empty means no stamp is sent,
// and the backend then falls back to the address Cloudflare connected from.
const OWNER_PROXY_SECRET = {_js_string(proxy_secret)};

// What this host serves. A prefix list rather than a route table, because the
// backend owns the routes and this file should not have to be edited every time
// one is added inside the console — but a *short* list, because the value of the
// list is what is missing from it. `tests/test_owner_site.py` derives the paths
// the bundled console actually calls out of `console/src/`, and the *sign-in
// flow* out of the backend's own redirects, and fails if one of them is not
// covered here.
const CONSOLE_PREFIXES = [
  "/owner",
  "/api/console",
  // The console's own switches: the reused dashboard endpoints it posts to
  // rather than reimplementing. `/api/toggle` and `/api/toggle-all` write, and
  // `/api/command-scope` reads what Discord holds after a write settles.
  "/api/toggle",
  "/api/toggle-all",
  "/api/command-scope",
  // Pictures the console draws, served from the backend's own origin so the CSP
  // can keep `img-src 'self'`.
  "/api/image",
  // The voice capability probe: the Radio panel's own reading of what this
  // deployment can do for voice, named by the panel that explains it.
  "/api/voice-debug",
  // Signing in and out. `/login/discord` is the owner's Discord button on the
  // console's own sign-in page; `/logout` is the sign-out the Access page offers.
  // `/login` itself is deliberately *not* here: that is the member-facing
  // passkey form, and proxying it is how this domain grew a second door.
  "/login/discord",
  "/logout",
  // The agreement, and the one entry here that the console's own sources cannot
  // name: it is the backend that puts `/agree?next=…` in a `Location` header
  // (`_door_target`), so it appears in no `.ts`/`.tsx` file and in no template.
  // Without it the sign-in flow dead-ended — the passkey was accepted, the
  // browser was sent to `/agree` *on this host*, this worker did not recognise
  // it, and its answer to anything it does not recognise is a 301 to the public
  // site: the operator landed on another host, signed out, at a page offering no
  // passkey field. The flow test in `tests/test_owner_site.py` derives this from
  // `keep_alive._door_target` so the next door is covered by existing.
  "/agree"
];

// The two paths that *are* the console without saying so. They are not prefixes
// and deliberately not in the list above: `/` as a prefix would mean "this host
// proxies everything, including the paths it exists to stop answering", which is
// one careless simplification of the check below away from happening.
//
// And they are the reason the sign-in flow dead-ended a second time. The passkey
// was accepted, the clickwrap was pressed, and the backend answered `Location:
// /owner` — so the browser came back to this host's root, which was not in the
// list, which this worker's answer is a 301 to the public site. The operator
// typed the right passkey, agreed to the terms, and landed on the marketing page
// signed out; the comment further down explaining that the root is the console
// had been sitting under a redirect that made it unreachable, so nothing could
// see the contradiction.
const ROOT_PATHS = ["/", "/index.html"];

export default {{
  async fetch(request, env) {{
    const url = new URL(request.url);

    // Static assets are served directly from Cloudflare Pages CDN
    if (url.pathname.startsWith("/static/")) {{
      return env.ASSETS.fetch(request);
    }}

    const isConsole = CONSOLE_PREFIXES.some(
      (prefix) => url.pathname === prefix || url.pathname.startsWith(prefix + "/")
    );
    const isRoot = ROOT_PATHS.includes(url.pathname);

    // The two files a host has to answer for itself. Everything else here is
    // redirected to the public site, and that is right — but a crawler asking
    // *this* host what it may read has to be answered by this host, or the
    // console's own address inherits the marketing site's directives, which say
    // nothing about a sign-in page. Served from the upload like `/static/`.
    if (url.pathname === "/robots.txt" || url.pathname === "/ai.txt") {{
      return env.ASSETS.fetch(request);
    }}

    if (!isConsole && !isRoot) {{
      // Not ours. Send it to the site it belongs to rather than to the backend
      // with this host's missing assets — a 301 and not a proxy, because the
      // other host is the one that can render it.
      const target = new URL(request.url);
      target.hostname = PUBLIC_SITE_HOST;
      target.protocol = "https:";
      target.port = "";
      return new Response(null, {{
        status: 301,
        headers: {{
          "Location": target.toString(),
          "Cache-Control": "no-store",
          "X-Meow-Owner-Host": "redirected"
        }}
      }});
    }}

    const backendUrl = new URL(request.url);
    backendUrl.hostname = BACKEND_HOST;
    backendUrl.protocol = "https:";
    backendUrl.port = "";

    // The root of this host is the console, so a bookmark to it lands on the
    // fleet rather than on a redirect — which is what the guard above this line
    // used to answer it with, because the root was not in any list it consulted.
    if (isRoot) {{
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
    if (typeof response.headers.getSetCookie === "function") {{
      const cookies = response.headers.getSetCookie();
      if (cookies && cookies.length > 0) {{
        resHeaders.delete("Set-Cookie");
        for (const cookie of cookies) {{
          resHeaders.append("Set-Cookie", cookie);
        }}
      }}
    }}
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
    """The console host's headers, including the reservation and `noindex`.

    The owner console is the one surface here where `noindex` is not a
    preference: every page on this host is either a sign-in form or the
    operator's own view of the fleet, and an index entry for either is only ever
    useful to somebody assembling a list of them. The reservation travels the
    same way it does everywhere else — from ``utils/ai_guard``, so the console
    cannot end up as the one host that permits training by omission.
    """
    reservation = "".join(
        f"  {name}: {value}\n" for name, value in ai_guard.headers(private=True).items()
    )
    return (
        "/*\n"
        "  Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'\n"
        "  X-Content-Type-Options: nosniff\n"
        "  X-Frame-Options: DENY\n"
        "  Referrer-Policy: no-referrer\n"
        "  Permissions-Policy: camera=(), microphone=(), geolocation=()\n"
        "  Strict-Transport-Security: max-age=31536000; includeSubDomains\n"
        f"{reservation}"
        "  Cache-Control: private, no-cache, no-store, must-revalidate\n"
        "\n/static/*\n"
        "  Cache-Control: public, max-age=31536000, immutable\n"
    )


def _host_of(url: str, fallback: str) -> str:
    """A bare hostname from a URL, for a value that came from an environment."""
    text = (url or "").strip()
    if not text:
        return fallback
    return text.replace("https://", "").replace("http://", "").split("/")[0] or fallback


def build(
    out_dir: str,
    live_app: str = DEFAULT_LIVE_APP,
    public_site: str = "",
) -> None:
    backend_host = _host_of(live_app, "1tsrajowo-meow.hf.space")

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
            dst = os.path.join(static_out, asset)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

    # The console bundle is deliberately **not** copied here: `/owner` renders one
    # page per entry in `OWNER_PAGES` now. It used to copy `static/console/` under
    # `os.path.exists`, which made what this host ships depend on whatever happened
    # to be sitting in `static/` on the machine that ran the build — an archived
    # bundle published here with no page asking for it. `tests/test_owner_site.py`
    # asserts the built directory carries none.

    # Write worker and configuration files
    proxy_secret = config_audit.get("OWNER_PROXY_SECRET")
    public_host = _host_of(
        public_site or config_audit.get("OWNER_PUBLIC_SITE_URL"),
        _host_of(DEFAULT_PUBLIC_SITE, "meow-bot.pages.dev"),
    )
    with open(os.path.join(out_dir, "_worker.js"), "w", encoding="utf-8") as f:
        f.write(_worker_file(backend_host, proxy_secret, public_host))

    with open(os.path.join(out_dir, "_routes.json"), "w", encoding="utf-8") as f:
        f.write(_routes_file())

    with open(os.path.join(out_dir, "_headers"), "w", encoding="utf-8") as f:
        f.write(_headers_file())

    # This host serves the console and nothing else, so its directives are the
    # private shape: `Disallow: /` for everybody, named crawlers and traps
    # written out as well. Without these two files the worker redirected
    # `/robots.txt` to the public site along with every other unknown path, which
    # meant the console's own address was governed by a file that never heard of
    # it.
    with open(os.path.join(out_dir, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(ai_guard.robots_txt(private=True))
    with open(os.path.join(out_dir, "ai.txt"), "w", encoding="utf-8") as f:
        f.write(ai_guard.ai_txt())

    print(f"Built meow-owner site into {out_dir}")
    print(
        f"This host serves the console ({', '.join(CONSOLE_PREFIXES)}...); "
        f"every other path is redirected to https://{public_host}."
    )
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
    parser.add_argument(
        "--public-site",
        default="",
        help="Where paths that are not the console are redirected (default: the built-in public site)",
    )
    args = parser.parse_args()
    build(args.out, args.live_app, args.public_site)


if __name__ == "__main__":
    main()
