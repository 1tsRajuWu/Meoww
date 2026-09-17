import sys
import os
import asyncio
import random
import datetime
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from keep_alive import keep_alive
from utils import commands_sync, db, features, modules, permissions

# Force real-time unbuffered log streaming to Render / cloud console
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")


def _env_flag(name: str) -> bool:
    """Read a 1/true/yes/on environment flag (default: off)."""
    return os.getenv(name, "0").strip().lower() in {"1", "true", "yes", "on"}


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

# Global module toggle interceptor for all slash commands
async def global_module_check(interaction: discord.Interaction) -> bool:
    if not interaction.command:
        return True

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
                        f"An administrator can re-enable it instantly in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                        ephemeral=True
                    )
                return False
    return True

bot.tree.interaction_check = global_module_check

STATUSES = [
    ("Meowing at the wall 🐈", discord.ActivityType.playing),
    ("Knocking over your water glass 🐾", discord.ActivityType.playing),
    ("Sleeping on the keyboard ⌨️", discord.ActivityType.playing),
    ("Chasing a laser pointer 🔴", discord.ActivityType.competing),
    ("Demanding wet food 🐟", discord.ActivityType.listening),
    ("Judging you silently 👁️", discord.ActivityType.watching),
    ("Zoomies at 3 AM 🏃‍♂️", discord.ActivityType.playing),
    ("Purring... 🐾", discord.ActivityType.listening),
    ("Plotting world domination 😼", discord.ActivityType.watching),
    ("Staring into the void 🕳️", discord.ActivityType.watching),
    ("Scratching the sofa 🛋️", discord.ActivityType.playing),
    ("Biting your ankles 🦶", discord.ActivityType.competing),
    ("Taking a 16-hour nap 💤", discord.ActivityType.playing),
]

async def rotate_status():
    last_idx = -1
    while True:
        available = [i for i in range(len(STATUSES)) if i != last_idx]
        idx = random.choice(available)
        last_idx = idx
        name, atype = STATUSES[idx]
        try:
            await bot.change_presence(activity=discord.Activity(type=atype, name=name), status=discord.Status.dnd)
        except Exception as e:
            print(f"Failed to update status: {e}")
        await asyncio.sleep(3600)  # Rotate every 60 minutes

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
    which every server the bot is in can see. Discord budgets the two scopes
    separately, so that is a usable second lane when a guild's is spent.

    Returns ``(ok, detail, error, published)``.
    """
    published = 0
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
            return False, f"/{name}: {exc}", exc, published
        published += 1
        print(f"[SYNC] ✅ /{name}{suffix}", flush=True)
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


async def _sync_scope(bot: commands.Bot, *, guild=None, force: bool = False) -> dict:
    """Publish a scope's missing/changed commands — one guild, or global.

    Returns ``{"registered", "pending", "published", "retry_after", "summary",
    "detail"}`` so a caller can decide between "done" and "try again later".
    """
    local = [command.to_dict(bot.tree) for command in bot.tree.get_commands()]
    payloads = {command["name"]: command for command in local if command.get("name")}
    registered = await _registered_command_payloads(bot, guild)
    scope = "global" if guild is None else guild.name

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
        # place that can take it out of the picker.
        await _retire_scope(bot, registered, guild)
        missing, stale = commands_sync.diff(local, registered)
        names = missing + stale
        summary = commands_sync.summary(local, registered)
        total = len(registered) + len(names)
        if not names and not force:
            return {
                "registered": len(registered), "pending": 0, "published": 0,
                "retry_after": None, "summary": summary, "detail": "",
            }

    ok, detail, error, published = await _publish_scope(bot, payloads, names, guild)
    pending = len(names) - published
    return {
        "registered": total - pending,
        "pending": pending,
        "published": published,
        "retry_after": _retry_delay(error) if error is not None else None,
        "summary": summary,
        "detail": detail,
        "ok": ok,
    }


async def _sync_guild(bot: commands.Bot, guild, *, force: bool = False) -> dict:
    """Publish one guild's missing/changed commands. See ``_sync_scope``."""
    return await _sync_scope(bot, guild=guild, force=force)


async def _sync_global(bot: commands.Bot, _guild=None, *, force: bool = False) -> dict:
    """Publish missing/changed commands to Discord's global scope."""
    return await _sync_scope(bot, force=force)


async def sync_commands(
    bot: commands.Bot, *, force: bool = False, drain: bool = True
) -> dict[str, int]:
    """Publish the slash-command tree to every guild the bot is in.

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

    # Discord budgets command writes **per scope**, and the global scope reaches
    # every server the bot is in, so it is published first: a guild whose daily
    # budget is spent still shows its commands. Read-compare-write means this
    # costs nothing once the global tree matches.
    global_status = await _sync_scope(bot)
    results["(global)"] = global_status["registered"]
    if not global_status["pending"]:
        print(f"[SYNC] ✅ global: {global_status['summary']}.", flush=True)
    else:
        print(
            f"[SYNC] global: {global_status['summary']} — published "
            f"{global_status['published']}, {global_status['pending']} still pending.",
            flush=True,
        )
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

    # Guild lane retired: global commands reach every server, and maintaining a
    # per-guild duplicate burns the 200-writes-a-day budget on commands that
    # already exist globally.  The old guild copies were deleted manually.
    return results


# Cogs reach the safe publisher through the bot instance. They must not
# ``import bot``: this file is what the process runs, so it is ``__main__``, and
# importing it again would re-execute the whole module on top of a second bot.
bot.publish_commands = sync_commands


@bot.event
async def on_ready():
    if getattr(bot, "_has_started", False):
        print("Bot reconnected to Discord gateway (ignoring duplicate on_ready).")
        return
    bot._has_started = True

    await db.setup()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    
    # Validate the feature registry once so a malformed manifest is loud.
    try:
        problems = features.validate()
        for problem in problems:
            print(f"[FEATURES] ❌ {problem}", flush=True)
        print(f"[FEATURES] ✅ {len(features.FEATURES)} features, "
              f"{len(features.TOGGLEABLE_IDS)} toggleable, loaded from the registry.", flush=True)
    except Exception as e:
        print(f"[FEATURES] ⚠️ Registry validation failed: {e}", flush=True)

    # Warm the shared module-state cache for every guild.
    for guild in bot.guilds:
        try:
            states = modules.refresh(guild.id)
            enabled = sum(1 for value in states.values() if value)
            print(f"[MODULES] {enabled}/{len(states)} modules enabled for {guild.name}.", flush=True)
        except Exception as e:
            print(f"Could not load module states for {guild.name}: {e}")

    
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
    # commands, /sync included).
    await sync_commands(bot)

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

    # Auto-announce updates to the update channel on every deploy with changes
    UPDATE_CHANNEL_ID = 1548948620600942632
    DEV_ROLE_ID = 1548948461301534791
    try:
        from utils import version
        conn = db.connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        last_hash = version.get_last_hash(conn)
        current_hash = version._changelog_hash()
        if current_hash and current_hash != last_hash:
            channel = bot.get_channel(UPDATE_CHANNEL_ID)
            if channel is None:
                # Do NOT store the hash: the announcement is still owed, and
                # marking it as sent is how an update goes missing forever.
                print(
                    f"[UPDATE] Channel {UPDATE_CHANNEL_ID} is not reachable — "
                    "the update note will be posted on the next boot.",
                    flush=True,
                )
            if channel:
                # Dynamically summarize the changelog
                from utils import version as version_module
                changelog_text = version_module._changelog_since_last(last_hash)
                summary = version_module.summarize_changelog(changelog_text)

                embed = discord.Embed(
                    title="🆕 Bot Updated — Meow",
                    description=(
                        "A new version has been deployed!\n\n"
                        f"{summary}"
                    )[:4096],
                    color=0x57F287,
                    timestamp=discord.utils.utcnow(),
                )
                embed.set_footer(text=f"Version {current_hash} • Generated by Meow Bot",
                                   icon_url=bot.user.display_avatar.url if bot.user else None)
                try:
                    await channel.send(
                        content=f"<@&{DEV_ROLE_ID}>",
                        embed=embed
                    )
                    # Only record it as announced once Discord accepted it.
                    version.set_last_hash(conn, current_hash)
                    print(f"[UPDATE] Posted update announcement to #{channel.name}", flush=True)
                except Exception as e:
                    print(
                        f"[UPDATE] Failed to post update ({e}) — will try again "
                        "on the next boot.",
                        flush=True,
                    )
        conn.close()
    except Exception as e:
        print(f"[UPDATE] Update announcement error: {e}", flush=True)

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
    print(f"[{now_str}] 🐾 Meow Bot is ONLINE and ready! Connected to {len(bot.guilds)} guild(s).", flush=True)

@bot.event
async def on_member_join(member: discord.Member):
    """Auto-assign the default role to a new member, if one is configured.

    Requires the Members intent (``ENABLE_MEMBERS_INTENT=1``). When that
    intent is off, Discord never sends this event, so the handler is never
    called — which is the correct failure mode (no role, no crash).
    """
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

@bot.tree.command(name="dashboard", description="Access the 1:1 Real-Time Web Admin Dashboard & Module Controller")
@app_commands.default_permissions(administrator=True)
async def dashboard_cmd(interaction: discord.Interaction):
    if not interaction.guild_id:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return
    
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only administrators can access the Web Dashboard.", ephemeral=True)
        return
    
    from keep_alive import dashboard_base_url, generate_magic_token
    from utils.style import Panel, send_panel, ACCENT, avatar_url, guild_icon, guild_banner

    # The administrator's id rides along so the dashboard can say who scheduled a
    # message — the session itself has no other memory of who signed in.
    token = generate_magic_token(interaction.guild_id, interaction.user.id)
    # The token is held in the memory of the process that made it, so the link has
    # to name that process — not a fixed host that may be a different instance.
    magic_link = f"{dashboard_base_url()}/login?token={token}"
    guild = interaction.guild
    thumb = guild_icon(guild) or avatar_url(interaction.client.user)
    gallery = [u for u in (guild_banner(guild),) if u]

    view = Panel(
        title="Meow Control Center",
        body=(
            f"Welcome back, {interaction.user.mention}.\n"
            f"Instant module control for **{guild.name if guild else 'this server'}**.\n\n"
            "- 🎛️ Toggle economy, games, tickets, automod, and more in real time\n"
            "- 🔐 This sign-in link is **single-use** and expires in **15 minutes**\n"
            "- 🌙 Keep it private — it unlocks the full admin dashboard"
        ),
        accent=ACCENT,
        thumbnail=thumb,
        gallery=gallery or None,
        footer="Meow Admin  ·  one-time secure access",
        timeout=None,
    )
    view.add_row(
        discord.ui.Button(label="Open Dashboard", url=magic_link, style=discord.ButtonStyle.link, emoji="⚡")
    )
    await send_panel(interaction, view, ephemeral=True)

def _report_error(error: BaseException, interaction: discord.Interaction | None = None) -> None:
    """Forward an unhandled command error to ERROR_WEBHOOK_URL, if configured.

    Errors used to be swallowed into stdout, which is how a silently broken
    module stayed broken for weeks. This is opt-in and never raises.
    """
    url = (os.getenv("ERROR_WEBHOOK_URL") or "").strip()
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
        payload = _json.dumps({
            "content": f"⚠️ **{type(error).__name__}** — {where}\n```{str(error)[:1500]}```"
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
        # Don't leak stack traces to users, log it instead.
        print(f"Error in command {interaction.command.name if interaction.command else 'Unknown'}: {error}")
        _report_error(error, interaction)
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
