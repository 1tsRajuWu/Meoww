import sys
import asyncio
import random
import datetime
import time
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Loaded **before** this project's own modules are imported, and that order is
# load-bearing rather than stylistic: `keep_alive` and `utils.cloud_sync` read
# the environment at import time — the session key, the passkey, the Supabase
# credentials — so a `.env` read *after* them is invisible to every one of those
# reads. The documented way to configure this bot locally is to copy
# `.env.example` to `.env` and fill it in, and under the old order that file
# reached `DISCORD_TOKEN` below and nothing else. On a machine where the passkey,
# the session key and the backups were all configured, the dashboard still ran
# with a random session key, passkey sign-in switched off and no backups — and
# said nothing, because each of those modules had already decided what it could
# see.
#
# `load_dotenv` never overrides a variable that is already set, so a real host
# environment — Render, Railway, an `export` in a shell — still wins, and on those
# hosts this call is a no-op.
load_dotenv()

from keep_alive import keep_alive  # noqa: E402
from utils import (  # noqa: E402
    alert_webhook, bot_channels, brand, command_publish, command_scope, commands_sync,
    config_audit, db, features, guild_directory, modules, operator_log, pause,
    permissions,
)

# Force real-time unbuffered log streaming to Render / cloud console
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

TOKEN = config_audit.get("DISCORD_TOKEN") or None


def _env_flag(name: str) -> bool:
    """Read a 1/true/yes/on environment flag (default: off).

    Called with the setting's name rather than reading the environment here, so
    the deployment still has exactly one place that does — ``config_audit``,
    which is also the list that says what a missing one costs.
    """
    return config_audit.flag(name)


# Privileged intents stay OFF unless explicitly enabled. Requesting one the app
# has not enabled in the Developer Portal makes Discord refuse the connection
# entirely (discord.PrivilegedIntentsRequired), which would take the bot
# offline. Turn them on at:
#   Developer Portal -> your app -> Bot -> Privileged Gateway Intents
# and then set the matching env var to 1.
intents = discord.Intents.default()
if _env_flag("ENABLE_MESSAGE_CONTENT_INTENT"):
    intents.message_content = True
if _env_flag("ENABLE_MEMBERS_INTENT"):
    intents.members = True
# Presence is the one that decides whether the console can answer *who is
# online*. Without it Discord reports every member as ``offline`` — there is no
# error, no missing attribute and no other symptom — so the Live page used to
# print "1 online, 35 offline" for a server with people in it, the one being the
# bot. Off unless asked for, like the other two, and refused safely below if the
# portal has not granted it.
if _env_flag("ENABLE_PRESENCE_INTENT"):
    intents.presences = True

# Application flag *positions* Discord sets on the app itself once a privileged
# intent has been enabled in the Developer Portal. The *_LIMITED bit is the one
# an unverified app gets, which is every app under 100 servers. These are bit
# numbers, not shifted values — shifting them twice matches nothing, which would
# read as "Discord granted nothing" and silently drop even the members intent.
_MEMBER_INTENT_BITS = (14, 15)
_MESSAGE_CONTENT_BITS = (16, 17)
# GATEWAY_PRESENCE and its "limited" twin, which is the one an unverified app
# gets — every app under 100 servers.
_PRESENCE_INTENT_BITS = (12, 13)


def portal_intents(timeout: float = 10.0) -> tuple[bool, bool, bool] | None:
    """Which privileged intents Discord has actually granted this app.

    ``(members, message_content, presences)``, or ``None`` when Discord cannot be
    asked.
    """
    try:
        import json as _json
        import urllib.request

        request = urllib.request.Request(
            "https://discord.com/api/v10/applications/@me",
            headers={
                "Authorization": f"Bot {TOKEN}",
                "User-Agent": "DiscordBot (https://github.com/Rapptz/discord.py, 1.0)",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            flags = int(_json.load(response).get("flags", 0))
    except Exception as e:
        print(
            f"[INTENTS] Could not ask Discord which intents are enabled ({e}) — "
            "requesting exactly what the environment asks for.",
            flush=True,
        )
        return None
    return (
        any(flags & (1 << bit) for bit in _MEMBER_INTENT_BITS),
        any(flags & (1 << bit) for bit in _MESSAGE_CONTENT_BITS),
        any(flags & (1 << bit) for bit in _PRESENCE_INTENT_BITS),
    )


def drop_ungranted_intents(bot, granted: tuple[bool, bool, bool] | None) -> list[str]:
    """Stop asking for intents Discord would refuse, and name the ones dropped.

    Requesting a privileged intent the portal has not enabled makes Discord
    reject the *whole* identify payload, so the bot never comes up at all: a
    mismatched environment variable becomes an outage, and the only clue is one
    line in a host log nobody is watching. Asking for less than was configured
    degrades one feature and says which, loudly, instead of refusing to start.

    This never *adds* an intent — a server running without message content and
    without being told to ask for it stays that way.
    """
    if granted is None:
        return []
    members_granted, content_granted, presence_granted = granted
    dropped: list[str] = []
    if getattr(bot.intents, "members", False) and not members_granted:
        bot.intents.members = False
        dropped.append("Server Members (ENABLE_MEMBERS_INTENT)")
    if getattr(bot.intents, "message_content", False) and not content_granted:
        bot.intents.message_content = False
        dropped.append("Message Content (ENABLE_MESSAGE_CONTENT_INTENT)")
    if getattr(bot.intents, "presences", False) and not presence_granted:
        bot.intents.presences = False
        dropped.append("Presence (ENABLE_PRESENCE_INTENT)")
    return dropped


bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# The cog list, the command->module gates and the default states all come from
# utils/features.py. Add or retire a feature there, not here.
INITIAL_COGS = features.cog_paths()
MODULE_COMMAND_MAP = features.MODULE_COMMAND_MAP
ALWAYS_ALLOWED_COMMANDS = features.ALWAYS_ALLOWED_COMMANDS

# Shared with the dashboard thread so both see exactly the same state.
bot.module_states = modules.CACHE

def is_module_enabled(guild_id: int, module_name: str) -> bool:
    """Whether a module is running for a guild (cached, thread-safe).

    Delegates to utils.modules, the single reader/writer of module state, so the
    bot and the web dashboard can never disagree about a toggle.
    """
    return modules.is_enabled(guild_id, module_name)

def set_module_state(guild_id: int, module_name: str, enabled: bool):
    """Update cached module state instantly (no database write)."""
    modules.apply_cached(guild_id, module_name, enabled)

bot.is_module_enabled = is_module_enabled
bot.set_module_state = set_module_state


def is_paused() -> bool:
    """Whether the operator has paused the bot everywhere (see `utils.pause`).

    Exposed on the bot for the same reason the module readers are: other parts of
    the process — the scheduler, the uptime card, the console — ask the *bot*
    what it is doing rather than importing the state from two places.
    """
    return pause.is_paused()


bot.is_paused = is_paused

# Global module toggle interceptor for all slash commands
async def global_module_check(interaction: discord.Interaction) -> bool:
    if not interaction.command:
        return True

    # The operator's bot-wide pause, checked first because it outranks every
    # per-server switch: a pause means *no* server is answered, including one
    # where the system in question is on. The reading is a cached dictionary
    # (`utils.pause`), so this costs nothing measurable per interaction.
    #
    # The message is the whole point of doing it here rather than by stopping the
    # process. A paused bot that simply went quiet is indistinguishable from a
    # crashed one, and the member who typed `/daily` would try again, and again —
    # so the refusal says what it is, who did it and where it can be undone.
    if pause.is_paused():
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "😴 The bot is paused by its operator, so commands are not being "
                "answered anywhere right now. This is deliberate rather than a "
                "fault — check back in a little while.",
                ephemeral=True,
            )
        return False

    if not interaction.guild_id:
        # Most commands read guild-scoped state (wallet, level row, module
        # toggle). Invoked from a DM they used to raise AttributeError on
        # `interaction.guild.id` and answer "An unexpected error occurred";
        # reply with the actual reason instead.
        root = interaction.command.root_parent.name if getattr(interaction.command, "root_parent", None) else interaction.command.name
        if root not in features.DM_ALLOWED_COMMANDS and interaction.command.name not in features.DM_ALLOWED_COMMANDS:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ This command only works inside a server.", ephemeral=True
                )
            return False
        return True
    
    # Check root command name for subcommands (e.g. "ticket" for "/ticket setup")
    root_cmd = interaction.command.root_parent.name if getattr(interaction.command, "root_parent", None) else interaction.command.name
    if root_cmd in ALWAYS_ALLOWED_COMMANDS:
        return True

    gated = MODULE_COMMAND_MAP.get(root_cmd) or MODULE_COMMAND_MAP.get(interaction.command.name)
    if gated:
        for module_id in gated:
            if not bot.is_module_enabled(interaction.guild_id, module_id):
                feature = features.get(module_id)
                label = feature.name if feature else module_id.replace("_", " ").title()
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"⚠️ The **{label}** system is currently **turned off** on this server.\n"
                        f"An administrator can re-enable it instantly in the [Web Dashboard](https://dacat.pages.dev/dashboard).",
                        ephemeral=True
                    )
                return False
    return True

bot.tree.interaction_check = global_module_check

# The status list used to be a hardcoded tuple here, which meant the only way to
# change one line was to edit this file and redeploy. It lives in
# ``utils/presence`` now, stored in the database and read fresh every cycle, so a
# change takes effect on the next rotation without a restart. The reported line
# ("Knocking over your water glass") is gone from the defaults.
#
# Read each pass rather than cached at boot on purpose: that is what makes
# editing the list *now* matter now, instead of after the next deploy.

async def rotate_status():
    """Keep the bot's activity on the configured line, rotating on the interval.

    Presence is application-wide — Discord has no per-server status — so this is
    one rotation for every server at once, and an empty list means "be online with
    no activity", which is a legitimate choice rather than an error.
    """
    from utils import presence

    last: tuple[str, str] | None = None
    while True:
        try:
            entries = await presence.lines()
        except Exception as e:
            # A settings read that fails must not kill the loop: the next cycle
            # tries again, and in the meantime the bot keeps the last status.
            print(f"[PRESENCE] Could not read the status list: {e}", flush=True)
            entries = []

        if entries:
            # Never show the same line twice in a row, which is what a plain
            # choice does one time in N and is the one repeat anybody notices.
            pool = [entry for entry in entries if entry != last] or entries
            last = random.choice(pool)
            text, kind = last
            try:
                # ``presence.build`` is the single place that knows how a line maps
                # to an activity object — including the verbless ``custom`` kind,
                # which is a CustomActivity rather than a typed one.
                await bot.change_presence(
                    activity=presence.build(text, kind),
                    status=discord.Status.online,
                )
            except Exception as e:
                print(f"[PRESENCE] Could not set the status: {e}", flush=True)
        else:
            # Configured empty: clear the activity once, then stop calling the
            # gateway every cycle to clear something already clear.
            if last is not None:
                try:
                    await bot.change_presence(activity=None, status=discord.Status.online)
                except Exception as e:
                    print(f"[PRESENCE] Could not clear the status: {e}", flush=True)
            last = None

        try:
            await asyncio.sleep(await presence.interval_seconds())
        except Exception:
            await asyncio.sleep(60 * presence.DEFAULT_INTERVAL_MINUTES)

#: How long the background drain keeps trying to publish pending commands, and
#: the longest it will wait between cycles. Discord's 200-writes-a-day budget is
#: not a cliff — it refills a slot at a time (the live guild gained roughly one
#: command every few minutes) — so recovering a guild that is tens of commands
#: behind takes repeated attempts over hours, not one retry.
DRAIN_BUDGET_SECONDS = 12 * 3600
DRAIN_MAX_WAIT = 900.0

#: Seam for the drain loop's wait, so tests can run it without sleeping.
_sleep = asyncio.sleep


async def drain_pending(
    bot: commands.Bot,
    guild,
    *,
    first_delay: float,
    budget_seconds: float = DRAIN_BUDGET_SECONDS,
    sync=None,
    label: str | None = None,
) -> bool:
    """Keep publishing a scope's pending commands until it is complete.

    ``guild`` names the target for the log line and selects the default
    synchroniser; a caller draining a different scope passes ``sync``/``label``
    to reuse the same loop (see ``_sync_global``).

    Returns True once nothing is pending, False when the time budget ran out (the
    next boot continues where this left off).
    """
    sync = sync or _sync_guild
    label = label or guild.name
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    wait = first_delay
    while True:
        await _sleep(max(30.0, min(wait, DRAIN_MAX_WAIT)))
        status = await sync(bot, guild)
        if not status["pending"]:
            print(
                f"[SYNC] ✅ {label}: all {status['registered']} commands published.",
                flush=True,
            )
            return True
        wait = status["retry_after"] or 300.0
        if loop.time() >= deadline:
            print(
                f"[SYNC] ⚠️ {label}: {status['pending']} command(s) still pending "
                f"after {budget_seconds / 3600:.1f}h of retrying; a restart will carry on.",
                flush=True,
            )
            return False


def _schedule_command_drain(
    bot: commands.Bot, guild, delay: float, *, sync=None, label: str | None = None
) -> None:
    """Queue the background drain for a scope that still has commands pending."""
    async def _drain() -> None:
        try:
            await drain_pending(
                bot, guild, first_delay=delay, sync=sync, label=label
            )
        except Exception as exc:
            print(f"[SYNC] Drain failed for {label or guild.name}: {exc}", flush=True)

    asyncio.create_task(_drain())


async def _registered_command_payloads(bot: commands.Bot, guild=None) -> list[dict] | None:
    """What Discord already has — for one guild, or globally when ``guild`` is None."""
    scope = "the global scope's" if guild is None else f"{guild.name}'s"
    try:
        remote = (
            await bot.tree.fetch_commands()
            if guild is None
            else await bot.tree.fetch_commands(guild=guild)
        )
        return [command.to_dict() for command in remote]
    except Exception as exc:
        print(f"[SYNC] ⚠️ Could not read {scope} registered commands: {exc}", flush=True)
        return None


def _is_write_budget_error(exc: BaseException) -> bool:
    """Whether an exception is Discord's ``30034``: the daily write budget is spent.

    ``discord.HTTPException.code`` carries Discord's error code straight out of the
    response body, and ``str(exc)`` embeds it as "(error code: 30034)" as well, so
    both are checked.
    """
    return getattr(exc, "code", None) == 30034 or "30034" in str(exc)


def _retry_delay(
    exc: BaseException, *, floor: float = 60.0, ceiling: float = 1800.0
) -> float | None:
    """How long to wait before retrying, or None when retrying cannot help.

    Discord tells us when the write window clears, so a deploy that lands while it
    is closed waits it out rather than leaving its new commands unregistered until
    the next restart — which is how the guild ended up 22 commands behind in the
    first place.
    """
    if not _is_write_budget_error(exc):
        return None
    reported = getattr(exc, "retry_after", None)
    try:
        delay = float(reported) if reported is not None else 600.0
    except (TypeError, ValueError):
        delay = 600.0
    return max(floor, min(delay, ceiling))


async def _publish_scope(
    bot: commands.Bot,
    payloads: dict[str, dict],
    names: list[str],
    guild=None,
) -> tuple[bool, str, BaseException | None, int]:
    """Create/update only the named commands, one request each.

    Deliberately **not** ``tree.sync(...)``. That call is a bulk overwrite, and
    Discord applies it destructively: it drops the scope's command list first and
    then rebuilds it. When the daily write budget runs out part-way through
    (error ``30034``) the deletes have already landed — which is exactly how this
    guild went from 74 commands to a single one, with nothing able to put them
    back until the window reset. An upsert only ever adds or replaces, so a
    rejected request can never take a command away.

    With ``guild`` omitted the same upsert goes to Discord's **global** scope,
    which every server the bot is in can see. That lane is now only for the
    commands that must work with no server to resolve; everything else is
    registered per server, because a global command cannot be hidden in one
    server while another keeps it.

    Returns ``(ok, detail, error, published)``.
    """
    published = 0
    refused: list[str] = []
    budget_error: BaseException | None = None
    suffix = "" if guild is not None else " (global)"
    for name in names:
        payload = payloads.get(name)
        if payload is None:
            continue
        try:
            if guild is None:
                await bot.http.upsert_global_command(bot.application_id, payload)
            else:
                await bot.http.upsert_guild_command(bot.application_id, guild.id, payload)
        except Exception as exc:
            refused.append(name)
            if budget_error is None and _is_write_budget_error(exc):
                budget_error = exc
            print(f"[SYNC] ⚠️ /{name}{suffix}: {exc}", flush=True)
            if _is_write_budget_error(exc):
                # The daily window is closed for *every* command, so the rest
                # would be refused too — walking away is cheaper than a hundred
                # doomed requests. Any other refusal is about one command (a
                # full scope answers ``30032`` for a *new* name only, and the
                # updates behind it are still allowed), and stopping at the
                # first one is how 30 unchanged-by-discord commands stayed
                # unpublished for weeks while the log said only "published 0".
                break
            continue
        published += 1
        print(f"[SYNC] ✅ /{name}{suffix}", flush=True)
    if refused:
        shown = ", ".join(refused[:6]) + (" …" if len(refused) > 6 else "")
        return False, f"{len(refused)} refused ({shown})", budget_error, published
    return True, "", None, published


async def _retire_scope(bot: commands.Bot, registered, guild=None) -> int:
    """Delete the commands this bot has retired, one named command at a time.

    The publisher can only add or replace, which is deliberate, so a command
    whose cog is gone would otherwise stay in the picker forever and fail for
    whoever clicked it. The names come from ``commands_sync.RETIRED_COMMANDS`` —
    never from "what is missing locally", because that is the destructive
    overwrite this sync exists to avoid. A refused delete is only logged: the
    command is still registered, so the next boot tries again.
    """
    removed = 0
    for name, command_id in commands_sync.retired(registered):
        try:
            if guild is None:
                await bot.http.delete_global_command(bot.application_id, command_id)
            else:
                await bot.http.delete_guild_command(bot.application_id, guild.id, command_id)
        except Exception as exc:
            print(f"[SYNC] ⚠️ Could not retire /{name}: {exc}", flush=True)
            continue
        removed += 1
        print(f"[SYNC] 🗑️ Retired /{name}", flush=True)
    return removed


async def _delete_scope_commands(bot: commands.Bot, pairs, guild=None, why: str = "") -> int:
    """Delete named commands from one scope, one request each.

    ``pairs`` is always an explicit ``(name, id)`` list decided by a rule in
    ``commands_sync`` — never "whatever is registered but not local", which is
    the bulk-overwrite instinct that once cleared a guild down to one command. A
    refused delete is logged and left alone: the command is still registered, so
    the next boot tries again, and the boot itself never fails on it.
    """
    suffix = " (global)" if guild is None else f" in {guild.name}"
    removed = 0
    for name, command_id in pairs:
        try:
            if guild is None:
                await bot.http.delete_global_command(bot.application_id, command_id)
            else:
                await bot.http.delete_guild_command(bot.application_id, guild.id, command_id)
        except Exception as exc:
            print(f"[SYNC] ⚠️ Could not remove /{name}{suffix}: {exc}", flush=True)
            continue
        removed += 1
        print(f"[SYNC] 🗑️ {why or 'Removed'} /{name}{suffix}", flush=True)
    return removed


def _explain_sync_stop(scope: str, status: dict) -> None:
    """Say *why* a scope still has commands pending — the line that was missing.

    A scope that published nothing printed a count and nothing else, so a bot
    refused at Discord's 100-command ceiling looked exactly like one whose writes
    were merely slow: every boot repeated "published 0, 32 still pending" while
    members looked at a picker missing the new commands and still offering the
    retired ones. The reason was already being carried in ``status['detail']``;
    nothing printed it.
    """
    detail = status.get("detail") or "no reason was reported"
    print(f"[SYNC] {scope}: {detail}", flush=True)
    if status.get("retry_after") is not None:
        return
    if "30032" in detail or "Maximum number of application commands" in detail:
        print(
            "[SYNC] That is Discord's ceiling of 100 chat-input commands, and this "
            "scope is full. Commands this bot no longer has hold the last of it, so "
            "they are deleted first (utils/commands_sync.RETIRED_COMMANDS) — a new "
            "command cannot be created until a slot is free.",
            flush=True,
        )


def _global_names(everything) -> set[str]:
    """What belongs in Discord's global scope: only what works outside a server.

    A globally-registered command is visible in **every** server the bot is in
    and no permission bit, context or option takes it back in one of them — so
    leaving the whole tree there would defeat per-server command lists entirely.
    The rule itself lives in ``utils/command_scope``, so this file and the
    operator's ``force_sync.py`` trim the shared lane by the same one.
    """
    return command_scope.global_names(everything)


def _scope_names(bot: commands.Bot, guild, everything) -> set[str]:
    """The command names one scope should be registered with.

    A server's list is decided by **that server's own modules**: a command whose
    modules are off there is left out of its registration, which is the only
    thing that takes it out of that server's picker. Nothing here can reach
    another server's commands, because the scope is the server.
    """
    if guild is None:
        # Nowhere to publish a per-server list yet, and the next invitation must
        # find a working picker: until there is a server to carry the tree, the
        # global scope carries all of it.
        if not bot.guilds:
            return set(everything)
        return _global_names(everything)
    try:
        states = modules.get_states(guild.id)
    except Exception as exc:
        # Failing open: an unreadable state publishes everything, which can only
        # ever leave a command visible, never take one away.
        print(
            f"[SYNC] ⚠️ {guild.name}: could not read module state ({exc}) — "
            "publishing the whole tree rather than hiding a command.",
            flush=True,
        )
        return set(everything)
    return set(command_scope.available_names(everything, states))


async def _sync_scope(
    bot: commands.Bot, *, guild=None, force: bool = False, allow_removals: bool = True
) -> dict:
    """Publish a scope's missing/changed commands — one server, or global.

    Returns ``{"registered", "pending", "published", "retry_after", "summary",
    "detail"}`` so a caller can decide between "done" and "try again later".
    """
    local = [command.to_dict(bot.tree) for command in bot.tree.get_commands()]
    everything = {command["name"]: command for command in local if command.get("name")}
    # This scope's own list. For a server that is every command its modules leave
    # on; for the global scope it is the DM-safe few (see _global_names).
    wanted = _scope_names(bot, guild, everything)
    payloads = {name: payload for name, payload in everything.items() if name in wanted}
    local = [payload for payload in local if payload.get("name") in wanted]
    registered = await _registered_command_payloads(bot, guild)
    scope = "global" if guild is None else guild.name
    #: What Discord held *before* this pass, minus everything this pass deleted.
    #: ``None`` while Discord's copy is unread — a count nobody read is not zero.
    held_before = None

    if registered is None:
        # Could not read the current state, so send everything. Upserts are
        # additive, so the worst case is spending writes we did not need to.
        print(f"[SYNC] {scope}: state unknown — publishing the whole tree.", flush=True)
        names = list(payloads)
        summary = f"{len(names)} command(s), state unknown"
        total = len(names)
    else:
        # Before the early return below: a retired command has no local
        # counterpart, so it is neither missing nor stale, and this is the only
        # place that can take it out of the picker. Counted, because the count
        # Discord ends up holding is what the dashboard's scope panel reports.
        removed = await _retire_scope(bot, registered, guild)
        if allow_removals:
            # A command this scope must not have is neither missing nor stale, so
            # nothing else would ever take it out. Every name here is one the
            # feature registry declares whose modules this server switched off —
            # so this can only undo something an administrator chose.
            removed += await _delete_scope_commands(
                bot,
                commands_sync.removable(
                    registered, wanted, set(features.MODULE_COMMAND_MAP)
                ),
                guild,
                "Not for this server",
            )
        missing, stale = commands_sync.diff(local, registered)
        names = missing + stale
        summary = commands_sync.summary(local, registered)
        total = len(registered) + len(names)
        # Names are never in two of these buckets: a *stale* command is already
        # registered (it is replaced, not added), a *missing* one is not, and a
        # removed one is neither. So this is the scope's size once every write
        # in ``names`` has landed — the figure the panel shows as "Discord holds"
        # and the reason the old ``total - pending`` could not be used for it: a
        # stale command was counted once in ``registered`` and again in ``names``.
        held_before = len(registered) - removed + len(missing)
        if not names and not force:
            command_publish.record(
                guild, verified=True, pending=0, held=held_before,
                detail=summary, retry_after=None,
            )
            return {
                "registered": held_before, "pending": 0, "published": 0,
                "retry_after": None, "summary": summary, "detail": "",
            }

    ok, detail, error, published = await _publish_scope(bot, payloads, names, guild)
    pending = len(names) - published
    retry_after = _retry_delay(error) if error is not None else None
    # The count is exact in two of three cases and reported in neither of the
    # rest. A pass that finished left ``held_before``; one that wrote nothing
    # left exactly what it read; a pass refused part-way through its writes
    # leaves the figure genuinely ambiguous, and a figure that is ambiguous is
    # worse than no figure, because a reader has no way to tell it is one.
    if registered is None:
        held = None
    elif pending == 0:
        held = held_before
    elif published == 0:
        held = held_before - len(missing)
    else:
        held = None
    command_publish.record(
        guild,
        verified=registered is not None,
        pending=pending,
        held=held,
        detail=detail,
        retry_after=retry_after,
    )
    return {
        "registered": held if held is not None else total - pending,
        "pending": pending,
        "published": published,
        "retry_after": retry_after,
        "summary": summary,
        "detail": detail,
        "ok": ok,
    }


async def _prune_global_lane(bot: commands.Bot, *, complete: bool = False) -> int:
    """Take the per-server commands out of Discord's global scope.

    What it removes is decided by the same rule as everywhere else — names the
    feature registry declares, minus the few that genuinely belong globally — so
    this can never delete a command it does not recognise, and it is idempotent:
    once the global scope holds only the DM-safe commands, there is nothing left
    to match.

    Two paths, and the difference is what is being proved.

    * ``complete=True`` — every server's publish came back with nothing pending,
      so the whole lane may be trimmed at once. Cheapest, and the boot uses it
      whenever it can.
    * ``complete=False`` — the boot could not get that far, or a switch just
      flipped and this is being called from the republish. The lane is trimmed
      **per name**, each one admitted only when every server that still wants it
      already holds its own copy (``commands_sync.prunable_globals``).

    The second path is why this function takes a flag at all. It used to be all
    or nothing, gated on the whole fleet finishing, so a single command sitting
    behind a spent daily write budget — or a switch flipped after the boot — left
    every stale global copy in place: the servers that had turned a system off
    kept seeing its commands in the picker, the ones that had not saw each twice,
    and no amount of clicking the switch could move any of it, because a switch
    only ever edits the *guild* scope. A per-name proof cannot be blocked by an
    unrelated command's refusal, and it decides nothing a server has not already
    received.
    """
    registered = await _registered_command_payloads(bot, None)
    if registered is None:
        return 0
    tree = [command.to_dict(bot.tree) for command in bot.tree.get_commands()]
    everything = {payload["name"]: payload for payload in tree if payload.get("name")}
    known = set(features.MODULE_COMMAND_MAP)
    wanted = _scope_names(bot, None, everything)
    candidates = commands_sync.removable(registered, wanted, known)
    if not candidates:
        return 0

    if complete:
        pairs = candidates
        stranded: list[str] = []
    else:
        guilds = list(bot.guilds)
        if not guilds:
            return 0
        held = []
        wants = []
        for guild in guilds:
            payloads = await _registered_command_payloads(bot, guild)
            if payloads is None:
                # One unreadable scope is enough: without every server's list
                # there is no proof that the servers wanting these commands hold
                # them, and a lane nobody can prove may not be deleted into.
                print(
                    f"[SYNC] global: could not read {guild.name}'s commands — "
                    "leaving the shared lane alone rather than guessing.",
                    flush=True,
                )
                return 0
            held.append(payloads)
            wants.append(_scope_names(bot, guild, everything))
        pairs = commands_sync.prunable_globals(registered, wanted, known, held, wants)
        stranded = commands_sync.stranded_globals(
            registered, wanted, known, held, wants
        )

    if not pairs:
        if stranded:
            _explain_stranded_globals(stranded)
        return 0
    removed = await _delete_scope_commands(bot, pairs, None, "Per server now")
    print(
        f"[SYNC] global: {removed} command(s) are registered per server now, so "
        "they no longer sit in every server's picker.",
        flush=True,
    )
    # The pass that ran before this one recorded what the lane held on its way
    # in; this is what it holds on the way out, which is the figure a reader
    # checking "did the commands I switched off leave?" is owed.
    command_publish.record(
        None,
        verified=True,
        pending=0,
        held=max(len(registered) - removed, 0),
        detail=f"{removed} command(s) left Discord's global lane",
    )
    if stranded:
        _explain_stranded_globals(stranded)
    return removed


def _explain_stranded_globals(names: list[str]) -> None:
    """Say why some commands are still in the shared lane — and that it is not a refusal.

    These are the commands still visible in a server whose switch says they are
    off: they are waiting on a *different* server's publish, not on Discord. A
    line that names them turns "I switched it off and it is still there" into
    something an operator can watch resolve on the next pass, which is the whole
    difference between a queue and a fault.
    """
    shown = ", ".join('/' + name for name in names[:6])
    more = f" … (+{len(names) - 6})" if len(names) > 6 else ""
    print(
        f"[SYNC] global: {len(names)} command(s) are still in the shared lane "
        f"({shown}{more}) because a server that wants them has not published its "
        "own copy yet. They leave on the next pass that finds every such server "
        "holding one.",
        flush=True,
    )


async def _sync_guild(bot: commands.Bot, guild, *, force: bool = False) -> dict:
    """Publish one guild's missing/changed commands. See ``_sync_scope``."""
    return await _sync_scope(bot, guild=guild, force=force)


async def _sync_global(bot: commands.Bot, _guild=None, *, force: bool = False) -> dict:
    """Publish missing/changed commands to Discord's global scope."""
    return await _sync_scope(bot, force=force)


async def sync_commands(
    bot: commands.Bot, *, force: bool = False, drain: bool = True
) -> dict[str, int]:
    """Publish each server's own command list, then trim the shared scope.

    Per server is not a preference: Discord's global scope is visible in every
    server the bot is in and no permission bit, context or option narrows it per
    server, so the *only* way a command a server turned off can leave that
    server's picker is for the command not to be registered there. A server's
    list therefore comes from its own modules (``utils/command_scope``), and the
    global scope keeps only what has to work outside a server at all — which is
    also why the copies that used to sit there are removed, once every server is
    known to hold its own.

    Nothing used to do this automatically, and the one path that existed — the
    manual `/sync` slash command — has to be registered before it can be used, so
    a bot whose tree had drifted could never repair itself. That is not
    theoretical: the live guild was sitting at **71 of 93** commands with `/sync`
    among the missing 22, so no amount of running `/sync` could have helped.

    Two safety rules, both earned the hard way:

    * **Compare first.** Discord allows 200 command writes per day per guild and
      answers ``30034`` once that is spent, so only commands that are actually
      missing or changed are sent. An unchanged guild costs nothing.
    * **Never bulk-overwrite.** See ``_publish_guild``: a bulk sync that is
      rejected mid-flight has already deleted everything.

    Never raises: a failed sync must not stop the bot coming online. A guild that
    still has commands pending is drained in the background afterwards, because
    Discord's write budget refills slowly. Returns ``{guild name: commands}``,
    with the global scope under the key ``"(global)"``.
    """
    results: dict[str, int] = {}
    every_server_complete = True

    # The per-server lane, which is now the only lane. Discord's global scope
    # reaches every server and cannot be narrowed, so a command list that differs
    # from server to server is only possible by registering each server's own
    # set. Read-compare-write keeps it affordable: a server that is already right
    # costs nothing, against the 200 writes a day Discord gives each one.
    for guild in list(bot.guilds):
        try:
            status = await _sync_guild(bot, guild, force=force)
        except Exception as exc:
            # Never into on_ready: a failed publish must not stop the bot coming
            # online, and the next boot tries the same thing again.
            print(f"[SYNC] ⚠️ {guild.name}: publish failed — {exc}", flush=True)
            command_publish.record(
                guild, verified=False, pending=0, held=None,
                detail=f"the boot publish failed: {exc}",
            )
            every_server_complete = False
            results[guild.name] = 0
            continue
        results[guild.name] = status["registered"]
        if not status["pending"]:
            print(f"[SYNC] ✅ {guild.name}: {status['summary']}.", flush=True)
            continue
        every_server_complete = False
        print(
            f"[SYNC] {guild.name}: {status['summary']} — published "
            f"{status['published']}, {status['pending']} still pending.",
            flush=True,
        )
        _explain_sync_stop(guild.name, status)
        if drain and status["retry_after"] is not None:
            print(
                f"[SYNC] Draining {guild.name} in the background "
                f"(next try in {status['retry_after']:.0f}s).",
                flush=True,
            )
            _schedule_command_drain(bot, guild, status["retry_after"], label=guild.name)

    # Published last, and with removals held back: a server whose scope is spent
    # still gets its own commands before the shared lane is touched.
    global_status = await _sync_scope(bot, force=force, allow_removals=False)
    results["(global)"] = global_status["registered"]
    if not global_status["pending"]:
        print(f"[SYNC] ✅ global: {global_status['summary']}.", flush=True)
    else:
        every_server_complete = False
        print(
            f"[SYNC] global: {global_status['summary']} — published "
            f"{global_status['published']}, {global_status['pending']} still pending.",
            flush=True,
        )
        _explain_sync_stop("global", global_status)
        if drain and global_status["retry_after"] is not None:
            wait = global_status["retry_after"]
            print(
                f"[SYNC] Draining the global scope in the background "
                f"(next try in {wait:.0f}s).",
                flush=True,
            )
            _schedule_command_drain(
                bot, None, wait, sync=_sync_global, label="global"
            )

    # The migration that makes per-server lists real. Copies of per-server
    # commands still registered *globally* appear in every server's picker —
    # which is exactly what a server that turned a module off must not see. They
    # are only removed once every server already holds its own copy: a failed
    # publish followed by a successful delete would leave that server with no
    # commands at all, and that is the one outcome worth being slow to reach.
    if bot.guilds:
        try:
            await _prune_global_lane(bot, complete=every_server_complete)
        except Exception as exc:
            # Never into on_ready: the lane is left as it is and the next pass
            # tries again, which is what every other failure here does.
            print(f"[SYNC] ⚠️ global lane: prune failed — {exc}", flush=True)
    return results


# Cogs reach the safe publisher through the bot instance. They must not
# ``import bot``: this file is what the process runs, so it is ``__main__``, and
# importing it again would re-execute the whole module on top of a second bot.
bot.publish_commands = sync_commands


def _publish_commands_in_background(bot: commands.Bot) -> "asyncio.Task | None":
    """Start the boot's command publish without holding the boot up.

    ``sync_commands`` sends one request per command Discord is missing, and
    Discord paces those writes hard: measured on the live host, a server that had
    lost its whole list spent **eighteen minutes** being given its 90 commands
    back. Awaiting that inside ``on_ready`` delayed everything after it — the
    25-second backup loop (so a restart in that window lost every coin and every
    point of XP earned during it), the per-server channel binding, and the
    release note the deploy owes each server.

    The publish itself is unchanged: same compare-first rule, same one-request
    upserts, same background drain when the daily write budget runs out. Only the
    waiting is gone. Nothing here raises — a publish that cannot even be started
    is reported, because a boot has other work to do.
    """
    publish = sync_commands(bot)
    try:
        task = bot.loop.create_task(publish)
    except Exception as exc:
        # A loop that cannot take the task would otherwise leave this coroutine
        # to be collected un-awaited, which is a warning at shutdown and a
        # publish that silently never happened.
        publish.close()
        print(f"[SYNC] ⚠️ could not start the command publish: {exc}", flush=True)
        return None

    def _report(finished: "asyncio.Task") -> None:
        try:
            results = finished.result()
        except Exception as exc:  # sync_commands never raises; a task still can
            print(f"[SYNC] ⚠️ command publish failed — {exc}", flush=True)
            return
        print(f"[SYNC] command publish finished: {results}", flush=True)

    task.add_done_callback(_report)
    return task


# ---------------------------------------------------------------------------
# A module switch is a command-set change
# ---------------------------------------------------------------------------
# Turning the market off is what takes `/market` out of that server's picker, so
# a switch that has just flipped makes the server's *registration* wrong. These
# three functions are how the dashboard asks for a republish without waiting for
# one: the request returns at once, the writes happen on the bot's own loop, and
# a burst of switches costs one publish rather than one per click — the "disable
# all" button flips eleven modules in a row, and eleven publishes of ninety
# commands each is how a daily write budget disappears.

#: How long a republish waits for the switches to stop moving before it reads
#: them. Long enough for the bulk buttons, short enough that a single click feels
#: immediate.
RESYNC_SETTLE_SECONDS = 3.0

_resync_dirty: set[int] = set()
_resync_tasks: dict[int, "asyncio.Task"] = {}


async def _resync_server(bot: commands.Bot, guild) -> None:
    """Republish one server's list, once, after its switches settle.

    Loops rather than running once: a switch that flips *while* this is
    publishing marks the server dirty again, and reading the state a second time
    is the difference between a server being correct and being one click stale.

    The dirty flag is cleared **after** the wait, not before. Clicking a bulk
    button fires one request per module, and every one of them lands inside this
    wait — clearing first would book each of those clicks as a fresh change and
    spend a second full pass (and a second read of every scope) re-publishing a
    state that had not moved. Clearing after means the wait absorbs the whole
    burst and the pass publishes what the clicking left behind, while a click
    that arrives once the publishes have started still gets its own pass.
    """
    while guild.id in _resync_dirty:
        await asyncio.sleep(RESYNC_SETTLE_SECONDS)
        _resync_dirty.discard(guild.id)
        try:
            status = await _sync_guild(bot, guild)
        except Exception as exc:
            print(f"[SYNC] ⚠️ {guild.name}: republish failed — {exc}", flush=True)
            # Written down rather than only logged: a raise here is the one
            # outcome a publisher never returns a status for, and an
            # administrator watching a command refuse to disappear is owed the
            # reason on the page they are looking at rather than in a log ring
            # behind a button on another one.
            command_publish.record(
                guild, verified=False, pending=0, held=None,
                detail=f"the last republish failed: {exc}",
            )
            break
        print(f"[SYNC] {guild.name}: {status['summary']}.", flush=True)
        if status["retry_after"] is not None:
            _schedule_command_drain(
                bot, guild, status["retry_after"], label=guild.name
            )
        # A switch just flipped and this server's own list is now current. If a
        # stale **global** copy of what it dropped is still sitting in the shared
        # lane, this is the moment to take it out: the guild scope alone cannot,
        # and waiting for the next boot is how a command stayed in the picker of
        # a server that had switched its system off.
        if not status["pending"]:
            try:
                await _prune_global_lane(bot, complete=False)
            except Exception as exc:
                print(f"[SYNC] ⚠️ global lane: prune failed — {exc}", flush=True)
    _resync_tasks.pop(guild.id, None)


async def _kick_guild_resync(bot: commands.Bot, guild) -> bool:
    """Mark a server dirty and make sure a pass is queued. Returns immediately."""
    _resync_dirty.add(guild.id)
    task = _resync_tasks.get(guild.id)
    if task is not None and not task.done():
        return True
    _resync_tasks[guild.id] = asyncio.create_task(_resync_server(bot, guild))
    return True


def resync_guild_commands(guild_id: int) -> bool:
    """Thread-safe: ask for one server's commands to be republished.

    Called from the Flask thread when a module switch flips, so nothing here
    awaits a Discord write: the page answers immediately and ninety upserts
    happen on the loop that owns the gateway objects.
    """
    guild = bot.get_guild(guild_id) if guild_id else None
    if guild is None:
        return False
    loop = getattr(bot, "loop", None)
    if loop is None or not loop.is_running():
        return False
    asyncio.run_coroutine_threadsafe(_kick_guild_resync(bot, guild), loop)
    return True


bot.resync_commands = resync_guild_commands


@bot.event
async def on_ready():
    if getattr(bot, "_has_started", False):
        print("Bot reconnected to Discord gateway (ignoring duplicate on_ready).")
        return
    bot._has_started = True

    await db.setup()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Cache the bot's global avatar so the dashboard can show it without a
    # per-request lookup. The user object is fully populated here.
    try:
        import keep_alive as _ka
        _ka._BOT_AVATAR_URL = str(bot.user.display_avatar.url)
        print(f"[BOOT] Bot avatar cached: {_ka._BOT_AVATAR_URL[:60]}...", flush=True)
    except Exception as exc:
        print(f"[BOOT] Could not cache bot avatar: {exc}", flush=True)
    
    # Validate the feature registry once so a malformed manifest is loud.
    try:
        problems = features.validate()
        for problem in problems:
            print(f"[FEATURES] ❌ {problem}", flush=True)
        print(f"[FEATURES] ✅ {len(features.FEATURES)} features, "
              f"{len(features.TOGGLEABLE_IDS)} toggleable, loaded from the registry.", flush=True)
    except Exception as e:
        print(f"[FEATURES] ⚠️ Registry validation failed: {e}", flush=True)

    # One line about whether this host can play the radio, answered before anybody
    # asks for a station. The four things voice needs fail in four different ways
    # and none of them used to say anything to the member — the card said
    # "playing", the bot was in the channel, and nothing was heard. The probe is
    # asked once here and read again by /radio doctor, the owner console and
    # /api/voice-debug, so no two surfaces can disagree about it.
    try:
        from utils import voice_caps  # noqa: PLC0415

        print(voice_caps.startup_line(), flush=True)
    except Exception as exc:
        print(f"[VOICE] ⚠️ could not probe the host's voice support: {exc}", flush=True)

    # Warm the shared module-state cache for every guild.
    for guild in bot.guilds:
        try:
            states = modules.refresh(guild.id)
            enabled = sum(1 for value in states.values() if value)
            print(f"[MODULES] {enabled}/{len(states)} modules enabled for {guild.name}.", flush=True)
        except Exception as e:
            print(f"Could not load module states for {guild.name}: {e}")

    # Write down which servers these are, and drop any the bot has since left.
    # The console drew its server names and its server picker from the gateway
    # cache alone, which is empty in a fresh process — so until this ran, every
    # console page said "Unknown server". This loop is the moment it is complete.
    # See ``utils/guild_directory.py``.
    try:
        written, forgotten = guild_directory.reconcile(bot.guilds)
        print(
            f"[DIRECTORY] {len(bot.guilds)} server(s) known: "
            f"{written} written, {forgotten} forgotten.",
            flush=True,
        )
    except Exception as exc:
        print(f"[DIRECTORY] ⚠️ Could not reconcile the server directory: {exc}", flush=True)

    
    # Stamp every command with the audience declared in utils/permissions.py
    # *before* publishing, so what Discord offers in a member's picker matches
    # what the command's own checks will actually let them do. Logged because a
    # visibility change is a real change to what people see.
    try:
        changed = permissions.apply_declared_permissions(bot.tree)
        if changed:
            print(
                f"[PERMISSIONS] {len(changed)} command(s) gated for the picker: "
                f"{', '.join(sorted(changed))}",
                flush=True,
            )
        missing = permissions.undeclared(bot.tree)
        if missing:
            print(
                f"[PERMISSIONS] ⚠️ no declared audience for: {', '.join(missing)} "
                "— add them to utils/permissions.py",
                flush=True,
            )
    except Exception as e:
        print(f"[PERMISSIONS] ⚠️ Could not apply declared permissions: {e}", flush=True)

    # Register the command tree with Discord (see sync_commands: relying on the
    # manual /sync command alone is how this server ended up missing 21 of its
    # commands, /sync included) — but never by *waiting* for it. Discord paces
    # command writes hard, and a server whose list drifted takes tens of minutes
    # to give back (measured on the live host: 90 commands took 18 minutes), so
    # awaiting it here held up everything below — the 25-second backup loop, the
    # per-server channel binding, and the release note this deploy owes every
    # server. That is why an update that had already been deployed looked like it
    # never arrived, and why nothing earned meanwhile had a backup.
    _publish_commands_in_background(bot)

    # Auto-hoist the bot's role to just below @everyone so it can action anyone
    for guild in bot.guilds:
        try:
            bot_member = guild.get_member(bot.user.id)
            bot_roles = [r for r in bot_member.roles if r.managed]  # managed = bot's own role
            if bot_roles:
                top_pos = guild.roles[-1].position  # highest role position in server
                await bot_roles[0].edit(position=top_pos - 1)
                print(f"Hoisted role in {guild.name} to position {top_pos - 1}")
        except Exception as e:
            print(f"Could not hoist role in {guild.name}: {e}")

    # Sanitize economy balances (clamp any non-owner balances)
    try:
        from cogs.economy import sanitize_economy_balances
        await sanitize_economy_balances(bot)
    except Exception as e:
        print(f"Could not sanitize economy balances: {e}")

    # Start rotating status
    bot.loop.create_task(rotate_status())
    
    # Start persistent cloud database sync loop (every 25s)
    try:
        from utils.cloud_sync import start_cloud_sync_loop
        start_cloud_sync_loop(bot)
    except Exception as e:
        print(f"[CLOUD SYNC] Failed to start loop: {e}", flush=True)

    # Deliver dashboard-scheduled messages. Started here, on the bot's own loop,
    # because posting is a gateway object's job (see keep_alive's scheduler).
    try:
        from keep_alive import start_scheduler
        start_scheduler(bot)
    except Exception as e:
        print(f"[SCHEDULE] Failed to start the delivery loop: {e}", flush=True)

    # Ask the public site, on a timer, whether it is serving this checkout.
    # Started here rather than in CI, and started on the bot's own loop because
    # the only alarm that reaches anybody is a Discord card (see site_guard).
    try:
        from keep_alive import start_site_guard
        start_site_guard(bot)
    except Exception as e:
        print(f"[SITE-GUARD] Failed to start the check: {e}", flush=True)

    # Read UptimeRobot's view of this service, on a timer. The dashboard's
    # status panel and `/health`'s `uptime` block both read the snapshot this
    # keeps, so no page load ever waits on a third party's API — and a monitor
    # that changes state is announced in the operator's own channels rather
    # than only being readable by whoever thinks to look.
    try:
        from keep_alive import start_uptime_status
        start_uptime_status(bot)
    except Exception as e:
        print(f"[UPTIME] Failed to start the monitor refresh: {e}", flush=True)

    # Probe every picture and sound this bot sends, weekly. A dead media link is
    # the quietest fault in the whole product — Discord draws an empty box and
    # reports nothing — so the check exists to be the only thing that notices,
    # and the card it sends names the link to change (see utils/media_health).
    try:
        from keep_alive import start_media_health
        start_media_health(bot)
    except Exception as e:
        print(f"[MEDIA] Failed to start the media watch: {e}", flush=True)

    # Every server keeps its own house channels. A server nobody has configured
    # yet is bound by channel name here — once — so a server added today has
    # somewhere to log and announce without anyone running a command.
    try:
        from utils import bot_channels, vc_watch
        for guild in bot.guilds:
            # The voice-chat watch is off in *every* server by default and on in
            # the operator's own one — the split they asked for by name. Seeded
            # here rather than left to a hand-run command because a feature that
            # is supposed to be on in one server must not depend on remembering;
            # ``seed_home`` writes nothing when a row already exists, so a server
            # that switched it off keeps that decision across restarts.
            if bot_channels.is_own_server(guild):
                vc_watch.seed_home(guild.id)
            newly = await bot_channels.ensure_bound(guild)
            if not newly:
                continue
            print(
                f"[CHANNELS] Bound {bot_channels.describe(guild, newly)} "
                f"in {guild.name} ({guild.id}).",
                flush=True,
            )
            # The operator's two channels get a card explaining themselves, so a
            # channel that is empty by design does not read as a bot that was
            # never installed. Only on the boot that bound them — and in its own
            # guard, because this loop's other job is binding *every* server:
            # a cosmetic card that failed must not cost the next server its
            # channels, which is the opposite of what this loop is for.
            try:
                await operator_log.announce_setup(bot, guild, newly)
            except Exception as e:
                print(f"[CHANNELS] Could not explain the new channels: {e}", flush=True)
    except Exception as e:
        print(f"[CHANNELS] Channel binding error: {e}", flush=True)

    await report_duplicate_instance()

    # Auto-announce updates to each server's own update channel on every deploy
    # with changes. Outside the quiet window this prints when the note is due and
    # sends nothing; the loop started below is what actually posts it then.
    await _announce_release_notes()
    try:
        from keep_alive import start_release_window
        start_release_window(bot)
    except Exception as e:
        print(f"[UPDATE] Failed to start the quiet-window loop: {e}", flush=True)

    # The number behind "not everyone is in the tag list": without the intent the
    # cache holds only the members the bot has happened to see.
    if not getattr(bot.intents, "members", True):
        for guild in bot.guilds:
            total = guild.member_count or 0
            cached = len(guild.members)
            if total and cached < total:
                print(
                    f"[INTENTS] {guild.name}: {cached} of {total} members are cached, so that "
                    "is all the dashboard tag picker can offer.",
                    flush=True,
                )

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] 🐾 {brand.NAME} Bot is ONLINE and ready! Connected to {len(bot.guilds)} guild(s).", flush=True)

async def report_duplicate_instance() -> str | None:
    """Name a second copy of the bot that is connected, once, where it is read.

    Two copies of the same token online at once is the one fault nobody can see
    from inside Discord: the gateway delivers every interaction to *both*, so
    whichever copy answers first wins and the other's command ends in
    ``Interaction has already been acknowledged`` — while the interactions that
    reach a session that has gone quiet are answered too late to be accepted,
    which members see as "the application did not respond". Both copies' host logs
    name nothing, and the damage reads as a broken bot rather than a duplicated
    one.

    The database lease already knows another copy is alive — it is what stops the
    two of them fighting over the backup — so the boot asks it the same question
    about the gateway and reports the answer in the operator's own channels.

    Returns the other copy's name, or ``None`` when there is not one. Never
    raises: a boot has work left to do, and a check that cannot reach Supabase has
    no business stopping it.
    """
    try:
        from utils import cloud_sync

        # On a thread: the lease is an HTTPS round trip, and a boot that blocks
        # the loop for it delays the first commands of the deploy. What it answers
        # is a *second copy*: a rolling deploy's own previous container holds the
        # lease for a few seconds and is not one.
        owner = await asyncio.to_thread(cloud_sync.other_live_copy)
        if not owner:
            return None
        # Rolling deploy grace check: during zero-downtime rolling deploys (e.g. on
        # Hugging Face or container hosts), the previous container is in the process of
        # shutting down. We wait 45 seconds before broadcasting a false-alarm embed.
        print(f"[INSTANCE] Notice: lease held by '{owner}'. Waiting 45s rolling deploy grace period...", flush=True)
        await asyncio.sleep(45)
        owner = await asyncio.to_thread(cloud_sync.other_live_copy)
        if not owner:
            print("[INSTANCE] Rolling deploy peer exited cleanly. Sole primary instance active.", flush=True)
            return None

        print(
            f"[INSTANCE] ⚠️ another copy of this bot is running ({owner}) — "
            "commands will fail at random until one of them stops.",
            flush=True,
        )
        await operator_log.report_second_instance(bot, owner)
        return owner
    except Exception as e:
        print(f"[INSTANCE] could not check for another copy of the bot: {e}", flush=True)
        return None


def _age_text(created) -> str:
    """How old an account is, in the coarsest unit that is still useful.

    Coarse on purpose: the alert needs "minutes old" versus "years old", and a
    precise timestamp would be a second personal-data field for no added reading.
    """
    if created is None:
        return "age not reported"
    try:
        seconds = max(0.0, (discord.utils.utcnow() - created).total_seconds())
    except Exception:  # noqa: BLE001 — an unreadable timestamp is not worth a failure
        return "age not reported"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''} old"
    return "minutes old"


async def _inviter_of(guild: discord.Guild) -> dict | None:
    """Who added the bot, and how old that account is — read from the audit log.

    Best-effort by construction. Reading the audit log needs **View Audit Log**,
    which this deployment may not hold in a server it has just joined, and Discord
    keeps entries for a limited time — so ``None`` is a fact about permissions
    rather than a failure, and the alert says so in words instead of showing a
    blank. What it is for: a hundred invites sent from accounts minutes old is what
    a raid looks like, and this is the only place that answer exists.
    """
    me = getattr(guild, "me", None)
    try:
        entries = [
            entry
            async for entry in guild.audit_logs(
                limit=5, action=discord.AuditLogAction.bot_add
            )
        ]
    except Exception:  # noqa: BLE001 — no permission, or no audit log to read
        return None
    for entry in entries:
        target = getattr(entry, "target", None)
        if me is not None and getattr(target, "id", None) != getattr(me, "id", None):
            continue
        user = getattr(entry, "user", None)
        if user is None:
            continue
        return {
            "name": str(user),
            "id": getattr(user, "id", 0),
            "created": getattr(user, "created_at", None),
        }
    return None


def _join_fields(guild: discord.Guild, inviter: dict | None) -> list[tuple[str, str]]:
    """The card's fields — and the whole of what this alert discloses about a server.

    Listed here as one function so the *field list* is a thing somebody can read
    against ``PRIVACY.md`` section 5 rather than a set of string literals spread
    through a handler: a server's name, its id, its size, its owner, who added the
    bot and how old that account is. No message, no member roster, no channel list.
    """
    rows = [
        ("Server", guild.name),
        ("Id", str(guild.id)),
        ("Members", str(guild.member_count) if guild.member_count else "not reported"),
        (
            "Owner",
            str(guild.owner) if getattr(guild, "owner", None) else f"id {guild.owner_id}",
        ),
    ]
    if inviter:
        rows.append(("Added by", f"{inviter['name']} ({inviter['id']})"))
        rows.append(("Their account", _age_text(inviter.get("created"))))
    else:
        rows.append(
            ("Added by", "not readable — I need View Audit Log to say who invited me")
        )
    return rows


@bot.event
async def on_member_join(member: discord.Member):
    """Auto-assign the default role to a new member, if one is configured.

    Requires the Members intent (``ENABLE_MEMBERS_INTENT=1``). When that
    intent is off, Discord never sends this event, so the handler is never
    called — which is the correct failure mode (no role, no crash).
    """
    # First, and in its own guard, because it is about the *arrival* rather than
    # about what this server asked for on arrival. Out of band and silent: a join
    # wave is the thing an operator wants to see forming, and the account's age is
    # the only signal that separates an invitation from a raid.
    try:
        alert_webhook.send(
            "member-joined",
            "👤 Somebody joined a server",
            detail=(
                "Silent, and sent for the pattern rather than for the person: one "
                "arrival is a member, and a wave of them from accounts minutes old "
                "is not."
            ),
            fields=[
                ("Server", member.guild.name),
                ("Server id", str(member.guild.id)),
                ("Account", _age_text(getattr(member, "created_at", None))),
                (
                    "Members",
                    str(member.guild.member_count)
                    if member.guild.member_count
                    else "not reported",
                ),
            ],
            severity="info",
        )
    except Exception as exc:  # noqa: BLE001 — an alert may never fail a join
        print(f"[ALERT] could not report a join in {member.guild.id}: {exc}", flush=True)
    try:
        role_id = await db.get_setting(member.guild.id, "default_role_id")
        if not role_id:
            return
        role = member.guild.get_role(int(role_id))
        if role is None:
            # Role was deleted; clean up the stale setting.
            await db.set_setting(member.guild.id, "default_role_id", None)
            return
        if role >= member.guild.me.top_role:
            print(
                f"[DEFAULT ROLE] Cannot assign {role.name} (id={role.id}) to "
                f"{member} in {member.guild.name}: role is above my own.",
                flush=True,
            )
            return
        await member.add_roles(role, reason="Default role")
        print(
            f"[DEFAULT ROLE] Assigned {role.name} to {member} in "
            f"{member.guild.name}.",
            flush=True,
        )
    except discord.Forbidden:
        print(
            f"[DEFAULT ROLE] Missing permission to assign role in "
            f"{member.guild.name}.",
            flush=True,
        )
    except Exception as exc:
        print(
            f"[DEFAULT ROLE] Failed for {member} in {member.guild.name}: "
            f"{exc}",
            flush=True,
        )

@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command: app_commands.Command | app_commands.ContextMenu):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user = interaction.user
    guild_name = interaction.guild.name if interaction.guild else "DMs"
    print(f"[{now_str}] ⚡ Slash Command: /{command.qualified_name} executed by {user} ({user.id}) in {guild_name}", flush=True)
    # The same line, where the operator can actually watch it: the command log in
    # the bot's own server. Best-effort by construction — a command that just
    # worked must not fail on its way into the log.
    await operator_log.command_used(bot, interaction, command)


@bot.event
async def on_guild_join(guild: discord.Guild):
    """A server just added the bot: set it up, tell it what changed, and log it.

    Binding runs here as well as on boot, because a server added while the bot is
    running would otherwise wait for a restart before its own `#update`,
    `#bot-error` and `#logs` were found by name.
    """
    from utils import bot_channels

    # The operator's blocklist, enforced on the way in.
    #
    # A blocklist that only took effect when somebody clicked "leave" would be a
    # note to self rather than a rule: the whole point of one is that an invite
    # cannot walk around it. So the check is here, before any setup work, and the
    # leave is the first thing that happens — a server the operator has refused
    # never gets as far as a welcome message.
    try:
        refused = await db.blocked_guild(guild.id)
    except Exception as exc:  # noqa: BLE001 — a list that cannot be read must not block every join
        refused = None
        print(f"[BLOCKLIST] could not read the list for {guild.name}: {exc}", flush=True)
    if refused:
        print(
            f"[BLOCKLIST] leaving {guild.name} ({guild.id}) — on the operator's list: "
            f"{refused.get('reason') or 'no reason recorded'}",
            flush=True,
        )
        # Written down before the leave, for the same reason the record comes
        # before the log everywhere else: after ``leave()`` the guild object is
        # gone from the cache and there is nothing left to describe.
        await db.record_web_action(
            scope="bot",
            actor_id=0,
            actor_name="the blocklist",
            guild_id=guild.id,
            action="refused-join",
            target=guild.name,
            ok=True,
            detail=str(refused.get("reason") or ""),
            at=time.time(),
        )
        try:
            await guild.leave()
        except Exception as exc:  # noqa: BLE001
            print(f"[BLOCKLIST] could not leave {guild.name}: {exc}", flush=True)
        return

    # Written down before the slow work below, so a console opened while this
    # server is still being set up can already name it.
    # ``guild.member_count`` is ``None`` without the members intent, which this
    # bot keeps off — and ``None`` means "Discord did not say", which the
    # directory records as "keep what you have" rather than as a zero.
    guild_directory.remember(guild.id, guild.name, guild.member_count)

    # Out of band, and first, because this is the one moment the ordinary path
    # provably cannot be used: the bot has just arrived, so it is in none of this
    # server's channels yet and `operator_log` has nowhere to post. Silent, so it
    # does not page anybody — and the *reason* a server was added is the reading
    # that matters when a raid arrives as a hundred invites, which is why the
    # account that did it, its age, and whether it is new are all on the card.
    try:
        alert_webhook.send(
            "guild-joined",
            "➕ A server added the bot",
            detail=(
                "Sent out of band because the bot is in none of this server's channels "
                "yet. A server added by an account minutes old is the shape a raid takes."
            ),
            fields=_join_fields(guild, await _inviter_of(guild)),
            severity="info",
        )
    except Exception as exc:  # noqa: BLE001 — an alert may never fail a join
        print(f"[ALERT] could not report the join of {guild.id}: {exc}", flush=True)

    try:
        bound = await bot_channels.ensure_bound(guild)
        if bound:
            print(f"[CHANNELS] {guild.name}: bound {bot_channels.describe(guild, bound)}", flush=True)
    except Exception as exc:
        print(f"[CHANNELS] could not bind channels for {guild.name}: {exc}", flush=True)
    try:
        if _release_notes_are_silenced():
            print(f"[UPDATE] {guild.name}: automatic release notes are off", flush=True)
        else:
            outcome = await announce_release_notes_to_guild(guild)
            print(f"[UPDATE] "
                  f"{guild.name}: {outcome}", flush=True)
    except Exception as exc:
        print(f"[UPDATE] could not announce to {guild.name}: {exc}", flush=True)
    await operator_log.guild_joined(bot, guild)
    # Its own command list, published now rather than at the next restart. The
    # global scope holds only the DM-safe commands, so without this a server
    # added while the bot is running would have an almost empty picker until a
    # deploy happened.
    try:
        status = await _sync_guild(bot, guild)
        print(
            f"[SYNC] {guild.name}: published {len(bot.tree.get_commands())} command(s) "
            f"({status['summary']}).",
            flush=True,
        )
        if status["retry_after"] is not None:
            _schedule_command_drain(
                bot, guild, status["retry_after"], label=guild.name
            )
    except Exception as exc:
        print(f"[SYNC] could not publish commands for {guild.name}: {exc}", flush=True)


@bot.event
async def on_guild_remove(guild: discord.Guild):
    """A server removed the bot, was deleted, or banned it — all read the same."""
    # Forgotten as well as logged: the console offers operators the servers in
    # the directory, so a row left behind would offer one the bot is not in.
    guild_directory.forget(guild.id)
    # The other direction of the same event, and the one that is easy to miss: a
    # hundred servers removing the bot in a minute is a coordinated thing, and it
    # looks like nothing at all on a console that only lists what is still here.
    alert_webhook.send(
        "guild-left",
        "➖ A server removed the bot",
        detail="Logged for the same reason the joins are: the pattern is the signal.",
        fields=[
            ("Server", guild.name),
            ("Id", str(guild.id)),
            ("Members", str(guild.member_count) if guild.member_count else "not reported"),
        ],
        severity="warn",
    )
    await operator_log.guild_left(bot, guild)

# ``/dashboard`` used to live here as a top-level command. It was one of the
# last slots spent on an entry point that needs no slot at all: the link is
# minted for administrators by the ``/help`` panel, which they already open, and
# which costs the tree nothing. See ``cogs/help.py::dashboard_link``.

# The update channel these notes were hardcoded to before a server could say
# where it wanted them. Still honoured for the server that owns it, so nothing
# changes for the one that has been reading them all along — and the *server* that
# holds it is the second one this bot treats as its own, which is the question
# ``bot_channels.is_own_server`` answers. One copy of the id, because a gate on the
# operator's side of ``/qna`` reads it too and two copies is one drift away from a
# gate that refuses where the operator actually is.
LEGACY_UPDATE_CHANNEL_ID = bot_channels.LEGACY_UPDATE_CHANNEL_ID
#: The Dev role of this server, read from the module that owns "who is an
#: operator" rather than written down twice: the same id decides who may answer an
#: anonymous question, and two copies would be one rename away from disagreeing.
DEV_ROLE_ID = bot_channels.DEV_ROLE_ID


def _home_guild():
    """The server whose update channel predates servers saying where they want one."""
    return next(
        (g for g in bot.guilds if g.get_channel(LEGACY_UPDATE_CHANNEL_ID) is not None),
        None,
    )


async def announce_release_notes_to_guild(guild, current_hash: str | None = None) -> str:
    """Tell one server what this deploy changed, and say what happened.

    Returns ``posted``, ``up_to_date``, ``unbound``, ``unreachable`` or
    ``failed``. A boot only logs the outcome; ``/bot push`` has to answer the
    person who asked, so both go through here and neither can drift from the
    other — the digest, the ping and the "already told" record all live in this
    one place.

    Nothing is recorded as announced unless Discord accepted the message: a note
    that failed to send is still owed, and marking it sent is how an update goes
    missing forever.
    """
    from utils import bot_channels, version

    try:
        conn = db.connect()
    except Exception as e:
        print(f"[UPDATE] Could not open the database to announce: {e}", flush=True)
        return "failed"
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        current_hash = current_hash or version._changelog_hash()
        if not current_hash:
            return "failed"
        home = _home_guild()
        if home is not None and guild.id == home.id:
            # The one row that predates per-server records describes this server.
            version.migrate_to_per_guild(conn, home.id)
        last_hash = version.get_last_hash(conn, guild.id)
        if current_hash == last_hash:
            return "up_to_date"
        channel_id = await bot_channels.bound(guild.id, "update")
        if channel_id is None and home is not None and guild.id == home.id:
            channel_id = LEGACY_UPDATE_CHANNEL_ID
        if channel_id is None:
            return "unbound"
        channel = guild.get_channel(channel_id)
        if channel is None:
            # Do NOT store the hash: the note is still owed, and marking it sent
            # is how an update goes missing forever.
            print(
                f"[UPDATE] Guild {guild.id} has update_channel_id={channel_id} "
                "but that channel is not reachable — the update note will be "
                "posted on the next boot.",
                flush=True,
            )
            return "unreachable"
        summary = version.summarize_changelog(
            version._changelog_since_last(last_hash)
        )
        embed = discord.Embed(
            title=f"🆕 Bot Updated — {brand.NAME}",
            description=(
                "A new version has been deployed!\n\n"
                f"{summary}"
            )[:4096],
            color=0x57F287,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Version {current_hash} • Generated by {brand.NAME} Bot",
                         icon_url=bot.user.display_avatar.url if bot.user else None)
        # Ping the dev role only where it exists: an id belonging to another
        # server renders as an unreadable mention at best. A server that has that
        # role keeps the ping it always got; one that does not is matched by role
        # name, so its note is not left silently unaddressed.
        ping = bot_channels.notify_role(guild, DEV_ROLE_ID)
        content = f"<@&{ping.id}>" if ping is not None else None
        try:
            await channel.send(content=content, embed=embed)
            # Only record it as announced once Discord accepted it.
            version.set_last_hash(conn, current_hash, guild.id)
            print(f"[UPDATE] Posted update announcement to #{channel.name}", flush=True)
            return "posted"
        except Exception as e:
            print(
                f"[UPDATE] Failed to post to #{channel.name} ({e}) — will try "
                "again on the next boot.",
                flush=True,
            )
            return "failed"
    except Exception as e:
        print(f"[UPDATE] Update announcement error: {e}", flush=True)
        return "failed"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _release_notes_are_silenced() -> bool:
    """Whether this deploy keeps its release notes to itself.

    ``SILENT_UPDATES=1`` turns off the *automatic* half of the release notes —
    the note on every boot that changed ``CHANGELOG.md``, and the one a server
    gets the moment it adds the bot — and nothing else. ``/bot push`` still posts
    one on demand, because an operator asking is not what is being silenced.

    It exists because a bound update channel has no way to say no. Clearing the
    binding means "not chosen yet" rather than "not wanted", so the next boot
    binds the channel by name again (``bot_channels.ensure_bound``) and the notes
    resume; and a changelog entry is written on every deploy, so "forever, and
    without being asked" is exactly what a server that does not want them gets.

    This is the *entirely* silent switch. The usual answer to "these arrive at a
    bad hour" is ``UPDATE_WINDOW_IST``, which keeps the notes and moves them.
    """
    return _env_flag("SILENT_UPDATES")


async def _announce_release_notes() -> None:
    """Tell every server that asked for release notes about this deploy.

    One message per server, in that server's own channel, and each server's
    "already told" record is its own — so a server whose channel is unreachable
    retries by itself instead of dragging the others into a re-announcement it
    already showed them. A server that has bound no update channel is skipped
    entirely rather than being sent somewhere it never asked for.

    And a boot outside the quiet window sends nothing either — but it does not
    *forget*: nothing is recorded, so the note is still owed, and the loop this
    handshake starts (``keep_alive.start_release_window``) posts it when the
    window opens. That is the difference between a delay and a loss, and it is
    why this function is safe to call from both places.
    """
    from utils import version

    if _release_notes_are_silenced():
        print(
            "[UPDATE] automatic release notes are off (SILENT_UPDATES=1) — "
            "`/bot push` still posts one on demand.",
            flush=True,
        )
        return
    if not version.window_is_open():
        print(
            f"[UPDATE] Holding this deploy's release notes for "
            f"{version.describe_next()} — nothing is lost, the note is a digest "
            "of everything since each server's last one.",
            flush=True,
        )
        return
    for guild in bot.guilds:
        await announce_release_notes_to_guild(guild)


# Reached by `/bot push` through the client rather than by importing this module:
# in production ``bot.py`` *is* ``__main__``, so importing it by name would build a
# second bot. The same handshake the sync publisher uses.
bot.announce_release_notes_to_guild = announce_release_notes_to_guild
# The whole release-note path, for the quiet-window loop: it must run the same
# silence check and the same per-server records a boot runs, or the loop becomes
# a second way to announce that the first one cannot see.
bot.announce_release_notes = _announce_release_notes


#: One broken command hit by fifty people is one bug, not fifty cards. A repeat
#: of the same failure inside this window is counted onto the card that is
#: already sitting in the channel; after it, a fresh card posts.
ERROR_CARD_WINDOW_SECONDS = 600

#: (guild id, command, exception type) -> the card showing it, how often, since when.
_ERROR_CARDS: dict[tuple[int, str, str], dict] = {}


def _error_embed(
    command: str,
    error: BaseException,
    runner: str,
    where: str,
    seen: int,
    first_at: float,
    refused: str = "",
) -> discord.Embed:
    """The failure card, built the same way whether it is posted or updated.

    ``refused`` is Discord's own answer to *which field* it rejected, when there
    is one. A refused payload reports ``Invalid Form Body`` and nothing a reader
    can act on, so without it a card like this one names a command that broke and
    leaves the reader to reproduce the payload against the live API to find out
    why (see :func:`utils.operator_log.discord_field_errors`).
    """
    body = [f"**{type(error).__name__}**: {str(error)[:600]}", ""]
    if refused:
        body.append(f"**Discord refused:** {refused}")
        body.append("")
    if seen > 1:
        since = datetime.datetime.fromtimestamp(first_at, datetime.timezone.utc)
        body.append(f"Seen **{seen}** times since {since:%H:%M} UTC")
        body.append("")
    body.append(f"Last run by {runner} in {where}")
    return discord.Embed(
        title=f"⚠️ /{command} failed",
        description="\n".join(body)[:4096],
        color=0xE74C3C,
        timestamp=discord.utils.utcnow(),
    )


async def _report_error_to_channel(interaction: discord.Interaction, error: BaseException) -> None:
    """Post an unexpected command failure where that server can see it.

    ``ERROR_WEBHOOK_URL`` reports to one webhook configured for the whole
    process, which says nothing about *which* server a command broke in. A
    server that has bound an error channel gets the same failure in its own,
    naming the command and who ran it.

    Repeats of one failure are counted onto the card already in the channel: a
    command that breaks on every call would otherwise bury the channel it is
    meant to be reported in, and the hundredth card says no more than the first.
    Never raises — a command that has already failed must not fail again here.
    """
    from utils import bot_channels, style

    try:
        guild = interaction.guild
        if guild is None:
            return
        channel_id = await bot_channels.bound(guild.id, "error")
        if channel_id is None:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            print(
                f"[ERRORS] Guild {guild.id} has error_channel_id={channel_id} but "
                "that channel does not exist — the failure was not posted there.",
                flush=True,
            )
            return
        import time

        command = interaction.command.qualified_name if interaction.command else "unknown"
        # Where the command was *run*, which is not where this card is going.
        where = getattr(interaction.channel, "mention", None) or "a channel that is gone"
        signature = (guild.id, command, type(error).__name__)
        refused = operator_log.discord_field_errors(error)
        now = time.time()

        # Forget cards whose window has closed, so a long-running bot cannot hold
        # one entry for every command that has ever failed anywhere.
        for key, card in list(_ERROR_CARDS.items()):
            if now - card["at"] > ERROR_CARD_WINDOW_SECONDS:
                _ERROR_CARDS.pop(key, None)

        card = _ERROR_CARDS.get(signature)
        if card is not None:
            card["count"] += 1
            card["at"] = now
            embed = _error_embed(
                command, error, interaction.user.mention, where, card["count"], card["first"],
                refused=refused,
            )
            try:
                await style.edit_card(card["message"], embed=embed)
            except Exception as e:
                # The card is gone (deleted, or too old to edit). Forget it so the
                # next failure posts a fresh one instead of counting into a void.
                _ERROR_CARDS.pop(signature, None)
                print(f"[ERRORS] Could not update the card for /{command}: {e}", flush=True)
            return

        message = await style.send_card(
            channel,
            embed=_error_embed(command, error, interaction.user.mention, where, 1, now,
                               refused=refused),
        )
        _ERROR_CARDS[signature] = {"count": 1, "at": now, "first": now, "message": message}
    except Exception as e:
        print(f"[ERRORS] Could not report a command failure to its channel: {e}", flush=True)


def _report_error(error: BaseException, interaction: discord.Interaction | None = None) -> None:
    """Forward an unhandled command error to ERROR_WEBHOOK_URL, if configured.

    Errors used to be swallowed into stdout, which is how a silently broken
    module stayed broken for weeks. This is opt-in and never raises.
    """
    url = config_audit.get("ERROR_WEBHOOK_URL")
    if not url:
        return
    try:
        import json as _json
        import threading
        import urllib.request

        where = "unknown"
        if interaction is not None:
            command = interaction.command.qualified_name if interaction.command else "unknown"
            guild = interaction.guild.name if interaction.guild else "DMs"
            where = f"/{command} by {interaction.user} in {guild}"
        refused = operator_log.discord_field_errors(error)
        payload = _json.dumps({
            "content": f"⚠️ **{type(error).__name__}** — {where}\n```{str(error)[:1500]}```"
            + (f"\nDiscord refused: {refused}" if refused else "")
        }).encode("utf-8")

        def _send():
            try:
                req = urllib.request.Request(
                    url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
                )
                urllib.request.urlopen(req, timeout=8).close()
            except Exception:
                pass

        threading.Thread(target=_send, daemon=True).start()
    except Exception:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ You don't have permission to do that.", ephemeral=True)
    elif isinstance(error, app_commands.CommandOnCooldown):
        if not interaction.response.is_done():
            secs = int(error.retry_after)
            if secs >= 3600:
                wait = f"{secs // 3600}h {(secs % 3600) // 60}m"
            elif secs >= 60:
                wait = f"{secs // 60}m {secs % 60}s"
            else:
                wait = f"{secs}s"
            await interaction.response.send_message(f"⏰ Slow down! You can use this again in **{wait}**.", ephemeral=True)
    elif isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Permission denied. You cannot execute this command.", ephemeral=True)
    else:
        # Don't leak stack traces to users, log it instead. Discord's answer to
        # *which field* it refused goes on the same line: a rejected payload's
        # message is only "Invalid Form Body", and the field that was actually
        # wrong — a button sharing a row with a select, a placeholder over 100
        # characters — is the difference between a five-minute fix and an
        # afternoon spent guessing (see `operator_log.discord_field_errors`).
        refused = operator_log.discord_field_errors(error)
        named = interaction.command.name if interaction.command else "Unknown"
        print(f"Error in command {named}: {error}" + (f" — refused: {refused}" if refused else ""))
        _report_error(error, interaction)
        await _report_error_to_channel(interaction, error)
        # The same failure, in the channel an operator actually watches. This is
        # the only place it can be seen: discord.py dispatches the completion
        # event — the one that feeds the command log — only for a command that
        # finished without raising, so a broken command used to leave no trace
        # anywhere but a container log.
        try:
            await operator_log.command_failed(bot, interaction, error)
        except Exception as exc:
            print(f"[LOG] could not write the failed-command entry: {exc}", flush=True)
        message = "❌ An unexpected error occurred. Please try again."
        try:
            # A command that deferred and then failed never answered the user, so
            # their client sat on "thinking…" forever. Answer via the followup in
            # that case, the initial response otherwise.
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass

def _install_shutdown_flush() -> None:
    """Save the database when the host asks us to stop.

    Render stops a container with SIGTERM and kills it a few seconds later,
    and a signalled Python process dies without ever reaching its ``finally``
    block — so a redeploy used to discard every coin and XP earned since the
    last 25-second tick. Flush the newest state instead of waiting to be killed.
    """
    import signal

    def _handler(signum, _frame):
        print(f"[SHUTDOWN] Signal {signum} received — saving the database.", flush=True)
        try:
            from utils.cloud_sync import backup_to_cloud_sync
            backup_to_cloud_sync()
            print("[SHUTDOWN] Final backup written.", flush=True)
        except Exception as exc:
            print(f"[SHUTDOWN] Final backup failed: {exc}", flush=True)
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError) as exc:  # not the main thread, or no signals
        print(f"[SHUTDOWN] Could not install the SIGTERM handler: {exc}", flush=True)


async def main():
    # What this deployment is and is not configured to do, at the top of the log
    # rather than scattered through it. The first question after a deploy that
    # behaves oddly is whether it was configured at all, and the answer belongs
    # in one readable block: a setting that is simply absent leaves no error to
    # search for, which is why these were invisible for so long.
    for line in config_audit.boot_lines():
        print(line, flush=True)

    # 1. Restore persistent SQLite database from cloud before loading cogs or connecting
    _install_shutdown_flush()
    try:
        from utils.cloud_sync import restore_from_cloud, backup_to_cloud_sync
        await asyncio.to_thread(restore_from_cloud)
    except Exception as e:
        print(f"[CLOUD SYNC] Startup restore error: {e}", flush=True)

    async with bot:
        for cog in INITIAL_COGS:
            try:
                await bot.load_extension(cog)
                print(f"Loaded {cog}")
            except Exception as e:
                print(f"Failed to load {cog}: {e}")

        if not TOKEN:
            raise RuntimeError(
                "DISCORD_TOKEN not found. Set it in your .env file (local) "
                "or in your host's Environment Variables (Railway/Oracle)."
            )

        # keep_alive() starts the Web Admin Dashboard and uptime keep-alive
        keep_alive(bot)

        # Ask Discord what it will actually allow *before* asking for it. An
        # intent the portal has not enabled takes the whole connection down, so
        # a mismatched variable would otherwise be an outage with a one-line
        # clue. Dropping it degrades a feature and says so.
        dropped = drop_ungranted_intents(bot, await asyncio.to_thread(portal_intents))
        if dropped:
            print(
                "[INTENTS] Discord has not granted "
                + " and ".join(dropped)
                + " — starting without it rather than refusing to connect. Enable it in "
                "the Developer Portal (your app -> Bot -> Privileged Gateway Intents) "
                "and restart to get that feature back.",
                flush=True,
            )

        if not bot.intents.message_content:
            print(
                "[INTENTS] Message Content intent is OFF: message text is invisible to the bot, "
                "so AutoMod invite/link filtering cannot work and text XP uses the baseline rate. "
                "Enable it in the Developer Portal and set ENABLE_MESSAGE_CONTENT_INTENT=1.",
                flush=True,
            )

        if not bot.intents.members:
            print(
                "[INTENTS] Members intent is OFF: the bot only knows the members it has already "
                "run into, so the dashboard tag picker cannot list the whole server (it can still "
                "tag anyone by id). Enable it in the Developer Portal and set "
                "ENABLE_MEMBERS_INTENT=1.",
                flush=True,
            )

        try:
            await bot.start(TOKEN)
        except discord.PrivilegedIntentsRequired as e:
            print(
                "ERROR: A privileged intent was requested that is not enabled in the Discord "
                "Developer Portal. Open your app -> Bot -> Privileged Gateway Intents and enable "
                "it, or set ENABLE_MESSAGE_CONTENT_INTENT / ENABLE_MEMBERS_INTENT back to 0."
                f" ({e})",
                flush=True,
            )
        except discord.LoginFailure:
            print("ERROR: Invalid DISCORD_TOKEN provided. Please check your .env file or host configuration.")
        except Exception as e:
            print(f"ERROR: {e}")
        finally:
            try:
                from utils.cloud_sync import backup_to_cloud_sync
                await asyncio.to_thread(backup_to_cloud_sync)
            except Exception:
                pass

if __name__ == "__main__":
    asyncio.run(main())
