"""Serve the marketing site on a laptop, with the maintenance screen stepped around.

Usage (run from the repo root): ``python scripts/preview_site.py`` and open the
first address it prints — ``--port`` or ``PORT`` moves it.

Local review harness only. This file is one thing: it exports
``MEOW_SITE_PREVIEW`` **before** importing ``keep_alive``, which is the flag the
"Be Right Back" hook reads and deliberately does nothing on — so the new pages
can be reviewed on a laptop while every real host, and every ordinary build,
keeps serving the maintenance screen. Nothing here is imported by the bot and
nothing here deploys anything.

It serves the *real* Flask app — the same routes, the same templates and the
same context the published copy is built from — so what a reviewer sees here is
what `/api/figures`, the sign-in doors and the 404 page will do in production.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The one line this file exists for, and it must run before the import that
# reads it: `keep_alive` imports `config_audit`, and the hook asks the flag on
# every request.
os.environ["MEOW_SITE_PREVIEW"] = "1"

from keep_alive import app  # noqa: E402 — after the flag, on purpose

#: What a reviewer came to look at. `/login` is on the list because it is one of
#: the six pages that inherit the new frame without being rebuilt, so it is the
#: fastest way to check the frame against `pages.css`.
ROUTES = (
    "/", "/systems", "/start", "/pricing", "/premium",
    "/privacy", "/terms", "/ai", "/login",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", "8100") or "8100"),
        help="the port to serve on (default: 8100, or PORT)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="the address to bind")
    args = parser.parse_args()

    print(f"[PREVIEW] serving {app.name} with MEOW_SITE_PREVIEW=1 (this process only)")
    for route in ROUTES:
        print(f"[PREVIEW]   http://{args.host}:{args.port}{route}")
    print("[PREVIEW] a 404 path is worth one look too — /nothing-here")
    print("[PREVIEW] Ctrl-C to stop. Nothing is deployed by this script.")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
