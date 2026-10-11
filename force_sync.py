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
from utils import command_scope, commands_sync, features, modules, permissions

API = "https://discord.com/api/v10"
#: Spacing between upserts, to stay clear of per-route burst limits.
PACE_SECONDS = 0.6
#: A 429 asking for longer than this is treated as "the window is closed" rather
#: than something to block a terminal on.
MAX_WAIT = 60.0

#: When a large batch has to stop early (a spent budget), the commands members
#: reach for first should already be back. Everything else follows in tree order.
PRIORITY = (
    "help", "dashboard", "bot", "balance", "daily", "rank", "leaderboard",
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
    if not isinstance(body, dict):
        return ""
    if body.get("code") == 30034:
        return (
            " Discord's daily application-command write limit for this scope is "
            "spent; the rest will go through after it resets."
        )
    if body.get("code") == 30032:
        return (
            " Discord allows 100 chat-input commands per scope and this one is "
            "full: the commands this bot has retired "
            "(utils/commands_sync.RETIRED_COMMANDS) are deleted first, which is "
            "what frees a slot for a new one."
        )
    return ""


async def _build_commands(bot) -> list[dict]:
    """The full command payload, built the way discord.py would send it.

    The bot object is imported rather than re-created, so a command registered
    directly in ``bot.py`` would be published alongside the cogs — none is left
    there now (``/dashboard`` became the ⚡ button on the ``/help`` panel), but
    the payload is built from the live tree rather than assumed.
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
            return False, f"/{name}: {status} {detail}.{_limit_reason(body)}"
        time.sleep(PACE_SECONDS)
    return True, ""


def _bulk_overwrite(path: str, token: str, payload: list[dict]):
    """⚠️ One PUT that replaces the scope's command list. Destructive if rejected."""
    status, body = _request("PUT", path, token, payload)
    if status in (200, 201):
        return True, ""
    return False, f"{status} {body if isinstance(body, str) else json.dumps(body)}"


def _retire(label: str, path: str, token: str, registered: list[dict], args) -> int:
    """Delete the commands this bot has retired, one named command at a time.

    The boot does this (``bot._retire_scope``) and this script did not, which made
    the terminal path unable to do the one thing a *full* scope needs: Discord
    allows 100 chat-input commands, twelve of them were commands the code had
    folded away, and with the ceiling reached every new command was refused with
    ``30032`` — so ``/bot`` and ``/legal`` could not be published by any path.
    Freeing the slots has to come before the upserts, never after.
    """
    doomed = commands_sync.retired(registered)
    if not doomed:
        return 0
    if args.dry_run:
        print(
            f"   dry run: would retire {len(doomed)} command(s): "
            f"{', '.join(name for name, _ in doomed)}"
        )
        return 0
    removed = 0
    for name, command_id in doomed:
        # A 429 is not a failure — a burst of deletes earns one even at the pace
        # below, and a command that was refused is simply deleted on the next run
        # (the boot retries the whole list every time). Waiting out a short one
        # here is what makes a single run finish the job.
        for attempt in range(3):
            status, body = _request("DELETE", f"{path}/{command_id}", token)
            if status == 429 and attempt < 2:
                time.sleep(min(_rate_limit_wait(body), MAX_WAIT) + 0.5)
                continue
            break
        if status in (200, 204):
            removed += 1
            print(f"   🗑️  retired /{name}", flush=True)
        else:
            print(f"   ! could not retire /{name}: {status} {body}", flush=True)
        time.sleep(PACE_SECONDS)
    print(f"   {removed} of {len(doomed)} retired command(s) deleted ({label})", flush=True)
    return removed


def _publish(label: str, path: str, token: str, commands: list[dict], args) -> int:
    """Sync one scope. Returns the number of failures."""
    payloads = commands_sync.index(commands)
    print(f"\n== {label}")

    registered = _registered(path, token)
    if registered is None:
        return 1
    # Before the diff: a retired command is neither missing nor stale (it has no
    # local counterpart), and on a scope at its ceiling the delete is what makes
    # the create possible at all.
    if _retire(label, path, token, registered, args) and not args.dry_run:
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


def _identity_commands(commands: list, app_id) -> list:
    """Drop VIP-only roots when this token is not the VIP bot's.

    Same rule ``bot.py`` publishes by, so this tool cannot drift from the bot.
    """
    if not permissions.is_vip_app_id(app_id):
        dropped = [c["name"] for c in commands if c.get("name") in permissions.VIP_ONLY_COMMANDS]
        commands = [c for c in commands if c.get("name") not in permissions.VIP_ONLY_COMMANDS]
        if dropped:
            print(f"Main identity: leaving out {', '.join(sorted(dropped))}.")
    return commands


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
    commands = _identity_commands(commands, app_id)

    if args.global_scope:
        # The shared lane is no longer where the tree lives. Every server is
        # registered with its own list, because Discord cannot hide a global
        # command in one server — so publishing the whole tree here would put
        # back exactly the commands a server turned off.
        shared = command_scope.global_names([command["name"] for command in commands])
        scoped = [command for command in commands if command["name"] in shared]
        print(
            "Scope: global — only the commands that must work outside a server "
            f"({', '.join(sorted(shared)) or 'none'}). Everything else is "
            "registered per server; run without --global to publish those."
        )
        failures = _publish(
            "global", f"/applications/{app_id}/commands", token, scoped, args
        )
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
            # Each server's own list, from its own module state — the same rule
            # ``bot.py`` publishes by, so this tool cannot drift from the bot.
            try:
                states = modules.get_states(int(guild["id"]))
            except Exception as exc:
                print(f"   ! could not read {guild['name']}'s module state: {exc}")
                states = {}
            wanted = set(command_scope.available_names(
                [command["name"] for command in commands], states
            ))
            scoped = [command for command in commands if command["name"] in wanted]
            hidden = len(commands) - len(scoped)
            print(
                f"{guild['name']} ({guild['id']}): {len(scoped)} of {len(commands)} "
                f"command(s)" + (f", {hidden} off on this server" if hidden else "")
            )
            path = f"/applications/{app_id}/guilds/{guild['id']}/commands"
            failures += _publish(f"{guild['name']} ({guild['id']})", path, token, scoped, args)

    if failures:
        print("\nSome commands are still pending — a 30034 body means that scope's daily "
              "write limit is spent. Try --global for a separate budget, --patient to wait "
              "the window out, re-run later, or restart the bot: it publishes the "
              "pending commands on boot.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
