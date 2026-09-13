"""Register this bot's slash commands with Discord, straight from a shell.

Why this exists: the bot only ever published its command tree when someone ran
the `/sync` slash command — and `/sync` has to be registered before it can be
used, so a tree that had drifted could never repair itself. The live guild was
running 71 of its 93 commands, with `/sync` and `/dashboard` among the 22 that
were missing, so there was no way to fix it from inside Discord at all.

`bot.py` publishes the tree on every boot. This script does the same job from a
terminal, for when you want the commands live without waiting for a redeploy.

**It only ever adds or replaces commands.** Each command is sent with its own
upsert request, so a request Discord rejects cannot take anything away. That
matters more than it sounds: Discord's *bulk* overwrite rebuilds the whole
command list, and when the daily write budget ran out part-way through the
deletes had already happened — the guild was left with a single command.

Discord budgets writes **per scope**: the 200-a-day guild budget being spent
says nothing about the global scope, which is a separate 100-command allowance.
`--global` publishes there instead, which is the way out of a spent guild budget.

Usage:
    python force_sync.py                 # upsert missing/changed commands, per guild
    python force_sync.py --global        # publish to Discord's global scope instead
    python force_sync.py --dry-run       # report only, change nothing
    python force_sync.py --force         # re-send even when nothing differs
    python force_sync.py --overwrite     # ⚠️  bulk overwrite (can wipe if interrupted)
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
from utils import commands_sync, features, permissions

API = "https://discord.com/api/v10"
#: Spacing between upserts, to stay clear of per-route burst limits.
PACE_SECONDS = 0.6
#: A 429 asking for longer than this is treated as "the window is closed" rather
#: than something to block a terminal on.
MAX_WAIT = 60.0

#: When a large batch has to stop early (a spent budget), the commands members
#: reach for first should already be back. Everything else follows in tree order.
PRIORITY = (
    "help", "dashboard", "sync", "balance", "daily", "rank", "leaderboard",
    "work", "pay", "loan", "repay", "beg", "crash", "slots", "coinflip",
    "blackjack", "shop", "meme", "kms", "meow", "serverinfo", "userinfo",
)


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


def _rate_limit_wait(body) -> float:
    if isinstance(body, dict):
        try:
            return float(body.get("retry_after", 30.0))
        except (TypeError, ValueError):
            return 30.0
    return 30.0


def _limit_reason(body) -> str:
    if isinstance(body, dict) and body.get("code") == 30034:
        return (
            " Discord's daily application-command write limit for this scope is "
            "spent; the rest will go through after it resets."
        )
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
    # Same stamp as the boot path, so a manual sync publishes the declared
    # picker visibility rather than the pre-stamp payload.
    changed = permissions.apply_declared_permissions(bot.tree)
    if changed:
        print(f"  · gated for the picker: {', '.join(sorted(changed))}", flush=True)
    return [command.to_dict(bot.tree) for command in bot.tree.get_commands()]


def _registered(path: str, token: str) -> list[dict] | None:
    status, registered = _request("GET", path, token)
    if not isinstance(registered, list):
        print(f"   ! could not list commands ({status}): {registered}")
        return None
    return registered


def _ordered(names: list[str]) -> list[str]:
    """Names with the commands members reach for first moved to the front."""
    rank = {name: index for index, name in enumerate(PRIORITY)}
    return sorted(names, key=lambda name: (rank.get(name, len(PRIORITY)), name))


def _upsert_many(
    path: str, token: str, payloads: dict, names: list[str], *, patient_until: float = 0.0
):
    """Upsert each command separately. Returns ``(ok, detail)``.

    Never deletes anything, so a partial run always leaves the account better off
    than it found it. Normally it stops at the first hard limit; in patient mode
    (``patient_until`` is a unix timestamp) it waits each window out and keeps
    going, which is the only way through a spent daily budget, since Discord lets
    a slot or two free at a time.
    """
    for name in names:
        payload = payloads.get(name)
        if payload is None:
            continue
        while True:
            status, body = _request("POST", path, token, payload)
            if status in (200, 201):
                print(f"   ✅ /{name}", flush=True)
                break
            if status == 429:
                wait = _rate_limit_wait(body)
                if wait > MAX_WAIT and time.time() < patient_until:
                    print(f"   ⏳ /{name}: waiting out the window ({wait:.0f}s)", flush=True)
                    time.sleep(wait + 2.0)
                    continue
                if wait > MAX_WAIT:
                    return False, f"/{name}: rate limited for {wait:.0f}s.{_limit_reason(body)}"
                print(f"   ⏳ /{name}: waiting {wait:.0f}s", flush=True)
                time.sleep(wait + 1.0)
                continue
            detail = body if isinstance(body, str) else json.dumps(body)
            return False, f"/{name}: {status} {detail}"
        time.sleep(PACE_SECONDS)
    return True, ""


def _bulk_overwrite(path: str, token: str, payload: list[dict]):
    """⚠️ One PUT that replaces the scope's command list. Destructive if rejected."""
    status, body = _request("PUT", path, token, payload)
    if status in (200, 201):
        return True, ""
    return False, f"{status} {body if isinstance(body, str) else json.dumps(body)}"


def _publish(label: str, path: str, token: str, commands: list[dict], args) -> int:
    """Sync one scope. Returns the number of failures."""
    payloads = commands_sync.index(commands)
    print(f"\n== {label}")

    registered = _registered(path, token)
    if registered is None:
        return 1
    missing, stale = commands_sync.diff(commands, registered)
    print(f"   Discord has {len(registered)}: {commands_sync.summary(commands, registered)}")

    if not missing and not stale and not args.force:
        print("   nothing to do ✅")
        return 0

    names = _ordered(missing + stale)
    if args.overwrite:
        if args.dry_run:
            print(f"   dry run: would bulk overwrite with {len(commands)} commands")
            return 0
        print("   ⚠️  bulk overwrite requested — Discord rebuilds the whole list, so an "
              "interrupted run loses commands.")
        ok, detail = _bulk_overwrite(path, token, commands)
    else:
        if args.dry_run:
            print(f"   dry run: would upsert {len(names)} command(s): "
                  f"{', '.join(names[:12])}" + (" …" if len(names) > 12 else ""))
            return 0
        patient_until = time.time() + args.patient_seconds if args.patient else 0.0
        print(f"   upserting {len(names)} command(s)...")
        ok, detail = _upsert_many(
            path, token, payloads, names, patient_until=patient_until
        )

    failures = 0
    if not ok:
        failures = 1
        print(f"   ❌ stopped: {detail}", flush=True)

    after = _registered(path, token)
    if after is not None:
        still_missing, still_stale = commands_sync.diff(commands, after)
        left = _ordered(still_missing + still_stale)
        print(f"   now registered: {len(after)}"
              + (f", still pending: {len(left)} ({', '.join(left[:12])}"
                 + (" …" if len(left) > 12 else "") + ")" if left else ", all up to date ✅"))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Register the slash-command tree with Discord.")
    parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    parser.add_argument("--force", action="store_true", help="re-send even when nothing differs")
    parser.add_argument(
        "--global", dest="global_scope", action="store_true",
        help="publish to Discord's global scope instead of each guild",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="WARNING: bulk overwrite. If Discord rejects it part-way, commands are lost.",
    )
    parser.add_argument(
        "--patient", action="store_true",
        help="keep waiting out the daily write window instead of stopping (slow but finishes)",
    )
    parser.add_argument(
        "--patient-seconds", type=float, default=7200.0,
        help="how long --patient keeps trying (default 2 hours)",
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

    commands = asyncio.run(_build_commands(bot_module.bot))
    print(f"Local tree: {len(commands)} top-level commands.")

    if args.global_scope:
        print("Scope: global (Discord allows 100 commands here, separate from a guild's budget)")
        failures = _publish("global", f"/applications/{app_id}/commands", token, commands, args)
    else:
        status, guilds = _request("GET", "/users/@me/guilds", token)
        if status != 200 or not isinstance(guilds, list):
            print(f"Could not list the bot's guilds: {status} {guilds}")
            return 1
        if not guilds:
            print("The bot is not in any guild — invite it first.")
            return 0
        print(f"Guilds: {', '.join(str(g['name']) for g in guilds)}")
        failures = 0
        for guild in guilds:
            path = f"/applications/{app_id}/guilds/{guild['id']}/commands"
            failures += _publish(f"{guild['name']} ({guild['id']})", path, token, commands, args)

    if failures:
        print("\nSome commands are still pending — a 30034 body means that scope's daily "
              "write limit is spent. Try --global for a separate budget, --patient to wait "
              "the window out, re-run later, or restart the bot: it publishes the "
              "pending commands on boot.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
