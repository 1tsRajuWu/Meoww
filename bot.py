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
from utils import db, features, modules

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

    
    # Commands are persistently registered globally with Discord API.
    # Manual sync can be triggered at any time via /sync.
    print("✅ Application commands active and ready for interactions.")

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

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] 🐾 Meow Bot is ONLINE and ready! Connected to {len(bot.guilds)} guild(s).", flush=True)

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
    
    from keep_alive import generate_magic_token
    from utils.style import Panel, send_panel, ACCENT, avatar_url, guild_icon, guild_banner

    token = generate_magic_token(interaction.guild_id)
    dashboard_base = os.getenv("DASHBOARD_BASE_URL", "https://meow-discord-bot.onrender.com").rstrip("/")
    magic_link = f"{dashboard_base}/login?token={token}"
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

async def main():
    # 1. Restore persistent SQLite database from cloud before loading cogs or connecting
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
