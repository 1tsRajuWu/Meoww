"""Register this bot's slash commands with Discord, straight from a shell.

Why this exists: the bot only ever published its command tree when someone ran
the `/sync` slash command — and `/sync` has to be registered before it can be
used, so a tree that had drifted could never repair itself. The live guild was
running **71 of its 93 commands**, with `/sync` and `/dashboard` among the 22 that
were missing, so there was no way to fix it from inside Discord at all.

`bot.py` now syncs on every boot (`sync_commands`). This script does the same job
from a terminal, for when you want the commands live without waiting for a
redeploy.

It reads what Discord has first and only writes when something actually differs,
because Discord allows **200 command writes per day per guild** and answers error
``30034`` once that is spent — after which nothing registers at all until the
window resets.

Neither mode can delete a command this build doesn't know about: the payload is
always the entire local tree.

Usage:
    python force_sync.py                 # bulk overwrite every guild (only if needed)
    python force_sync.py --dry-run       # report only, change nothing
    python force_sync.py --force         # write even when nothing differs
    python force_sync.py --one-by-one    # paced upserts, to isolate one bad command
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request

import bot as bot_module
from utils import commands_sync, features

API = "https://discord.com/api/v10"
#: Spacing between POSTs in --one-by-one mode.
PACE_SECONDS = 1.0
#: A 429 asking us to wait longer than this is treated as "the window is closed"
#: rather than something to sit and block a terminal on.
MAX_WAIT = 60.0


def _request(method: str, path: str, token: str, payload=None):
    """One REST call. Returns ``(status, parsed_body_or_text)``; never raises."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "MeowBot (force_sync, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except (ValueError, TypeError):
            return exc.code, raw[:300]
    except OSError as exc:
        return 0, str(exc)


def _rate_limit_wait(body) -> float | None:
    """Seconds to wait for a 429, or None when no 429 detail is available."""
    if isinstance(body, dict):
        try:
            return float(body.get("retry_after", 30.0))
        except (TypeError, ValueError):
            return 30.0
    return 30.0


def _limit_reason(body) -> str:
    """A friendly explanation for a 429 that is really the daily write cap."""
    if isinstance(body, dict) and body.get("code") == 30034:
        return " Discord's daily application-command write limit (200/guild) is spent."
    return ""


async def _build_commands(bot) -> list[dict]:
    """The full command payload, built the way discord.py would send it.

    The bot object is imported rather than re-created, so the tree includes the
    commands registered directly in ``bot.py`` (/dashboard) as well as the cogs.
    """
    for cog in features.cog_paths():
        try:
            await bot.load_extension(cog)
        except Exception as exc:  # a broken cog must not block the rest
            print(f"  ! could not load {cog}: {exc}", flush=True)
    return [command.to_dict(bot.tree) for command in bot.tree.get_commands()]


def _registered(app_id: str, guild_id: str, token: str) -> list[dict] | None:
    status, registered = _request(
        "GET", f"/applications/{app_id}/guilds/{guild_id}/commands", token
    )
    if not isinstance(registered, list):
        print(f"   ! could not list commands ({status}): {registered}")
        return None
    return registered


def _bulk_overwrite(app_id: str, guild_id: str, token: str, payload: list[dict]):
    """One PUT that replaces the guild's command list with this tree."""
    for attempt in range(1, 4):
        status, body = _request(
            "PUT", f"/applications/{app_id}/guilds/{guild_id}/commands", token, payload
        )
        if status in (200, 201):
            return True, ""
        if status == 429:
            wait = _rate_limit_wait(body) or 30.0
            if wait > MAX_WAIT:
                return False, f"rate limited for another {wait:.0f}s.{_limit_reason(body)}"
            print(f"   ⏳ rate limited, waiting {wait:.0f}s (attempt {attempt})", flush=True)
            time.sleep(wait + 1.0)
            continue
        detail = body if isinstance(body, str) else json.dumps(body)
        return False, f"{status} {detail}"
    return False, "still rate limited after retries"


def _one_by_one(app_id: str, guild_id: str, token: str, payload: list[dict]) -> int:
    """Upsert each command separately. Returns the number of failures."""
    failures = 0
    for command in payload:
        for attempt in range(1, 4):
            status, body = _request(
                "POST", f"/applications/{app_id}/guilds/{guild_id}/commands", token, command
            )
            if status in (200, 201):
                print(f"   ✅ /{command['name']}", flush=True)
                break
            if status == 429:
                wait = _rate_limit_wait(body) or 30.0
                if wait > MAX_WAIT:
                    print(f"   ❌ stopped: rate limited for another {wait:.0f}s."
                          f"{_limit_reason(body)}", flush=True)
                    return failures + 1
                print(f"   ⏳ /{command['name']} rate limited, waiting {wait:.0f}s", flush=True)
                time.sleep(wait + 1.0)
                continue
            failures += 1
            print(f"   ❌ /{command['name']}: {status} {body}", flush=True)
            break
        time.sleep(PACE_SECONDS)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Register the slash-command tree with Discord.")
    parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    parser.add_argument("--force", action="store_true", help="write even when nothing differs")
    parser.add_argument(
        "--one-by-one", action="store_true",
        help="POST each command instead of one bulk overwrite (slow, but isolates failures)",
    )
    args = parser.parse_args()

    token = (bot_module.TOKEN or "").strip()
    if not token:
        print("No DISCORD_TOKEN found (checked the environment and .env).")
        return 1

    status, application = _request("GET", "/oauth2/applications/@me", token)
    if status != 200 or not isinstance(application, dict):
        print(f"Could not read the application: {status} {application}")
        return 1
    app_id = str(application["id"])

    status, guilds = _request("GET", "/users/@me/guilds", token)
    if status != 200 or not isinstance(guilds, list):
        print(f"Could not list the bot's guilds: {status} {guilds}")
        return 1
    if not guilds:
        print("The bot is not in any guild — invite it first.")
        return 0

    commands = asyncio.run(_build_commands(bot_module.bot))
    print(f"Local tree: {len(commands)} top-level commands.")
    print(f"Guilds: {', '.join(str(g['name']) for g in guilds)}")

    failures = 0
    for guild in guilds:
        guild_id, guild_name = str(guild["id"]), str(guild["name"])
        print(f"\n== {guild_name} ({guild_id})")

        registered = _registered(app_id, guild_id, token)
        if registered is None:
            failures += 1
            continue
        missing, stale = commands_sync.diff(commands, registered)
        print(f"   Discord has {len(registered)}: {commands_sync.summary(commands, registered)}")

        if not missing and not stale and not args.force:
            print("   nothing to do ✅")
            continue

        if args.dry_run:
            mode = "one-by-one" if args.one_by_one else "bulk overwrite"
            print(f"   dry run: would send {len(commands)} payload(s) via {mode}")
            continue

        if args.one_by_one:
            print(f"   sending {len(commands)} command(s) one at a time...")
            failures += _one_by_one(app_id, guild_id, token, commands)
        else:
            print(f"   bulk overwriting with {len(commands)} commands...")
            ok, detail = _bulk_overwrite(app_id, guild_id, token, commands)
            if not ok:
                failures += 1
                print(f"   ❌ bulk overwrite failed: {detail}", flush=True)
                print("      (retry with --one-by-one to isolate the offending command)")

        after = _registered(app_id, guild_id, token)
        if after is not None:
            still_missing, still_stale = commands_sync.diff(commands, after)
            left = sorted(still_missing + still_stale)
            print(f"   now registered: {len(after)}"
                  + (f", still not matching: {', '.join(left)}" if left else ", all up to date ✅"))

    if failures:
        print(f"\n{failures} failure(s). A 30034 body means the daily "
              "application-command write limit was reached; it resets on Discord's "
              "clock, and the next bot restart (or /sync) will finish the job.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
