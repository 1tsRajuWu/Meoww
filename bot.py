import sys
import os
import asyncio
import datetime
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from keep_alive import keep_alive
from utils import db

# Force real-time unbuffered log streaming to Render / cloud console
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
# intents.message_content = True
# intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

INITIAL_COGS = [
    "cogs.moderation",
    "cogs.tickets",
    "cogs.server_control",
    "cogs.welcome",
    "cogs.reaction_roles",
    "cogs.automod",
    "cogs.help",
    "cogs.legal",
    "cogs.bot_config",
    "cogs.economy",
    "cogs.games",
    "cogs.fun",
    "cogs.social",
    "cogs.leveling",
    "cogs.music",
    "cogs.giveaway",
    "cogs.colors",
    "cogs.tempvoice",
    "cogs.starboard",
]

# Real-time in-memory module toggle cache: guild_id -> {module_name: bool}
bot.module_states = {}

def is_module_enabled(guild_id: int, module_name: str) -> bool:
    """Check module state in 0ms using in-memory RAM, with sync DB fallback."""
    default_state = False if module_name == "welcome" else True
    guild_mods = bot.module_states.get(guild_id)
    if guild_mods is None:
        try:
            guild_mods = db.get_guild_modules_sync(guild_id)
            bot.module_states[guild_id] = guild_mods
        except Exception:
            return default_state
    return guild_mods.get(module_name, default_state)

def set_module_state(guild_id: int, module_name: str, enabled: bool):
    """Update in-memory module state instantly."""
    if guild_id not in bot.module_states:
        bot.module_states[guild_id] = {}
    bot.module_states[guild_id][module_name] = enabled

bot.is_module_enabled = is_module_enabled
bot.set_module_state = set_module_state

MODULE_COMMAND_MAP = {
    # Music (11 commands)
    "play": "music", "radio": "music", "nowplaying": "music", "pause": "music", "resume": "music",
    "skip": "music", "stop": "music", "queue": "music", "volume": "music", "songinfo": "music", "lyrics": "music",
    # Leveling (9 commands)
    "rank": "leveling", "leaderboard": "leveling", "levelsettings": "leveling", "setlevel": "leveling",
    "givexp": "leveling", "resetlevels": "leveling", "addlevelrole": "leveling", "removelevelrole": "leveling", "levelroles": "leveling",
    # Economy & Banking (13 commands)
    "balance": "economy", "daily": "economy", "weekly": "economy", "work": "economy", "rob": "economy",
    "pay": "economy", "deposit": "economy", "withdraw": "economy", "shop": "economy", "buy": "economy",
    "inventory": "economy", "richlist": "economy", "eco-admin": "economy",
    # Casino & Games (12 commands - dual module check)
    "gamble": ("economy", "games"), "slots": ("economy", "games"), "blackjack": ("economy", "games"),
    "roulette": ("economy", "games"), "coinflip": ("economy", "games"), "dice": ("economy", "games"),
    "mines": ("economy", "games"), "crash": ("economy", "games"), "highlow": ("economy", "games"),
    "tictactoe": "games", "rps": "games", "trivia": "games",
    # Moderation & Server Control (19 commands)
    "ban": "moderation", "kick": "moderation", "mute": "moderation", "unmute": "moderation",
    "unban": "moderation", "warn": "moderation", "warnings": "moderation", "clearwarnings": "moderation",
    "purge": "moderation", "modlog": "moderation", "lockdown": "moderation", "lockdown-server": "moderation",
    "unlock": "moderation", "unlock-server": "moderation", "slowmode": "moderation", "addrole": "moderation",
    "removerole": "moderation", "announce": "moderation", "reactionrole-add": "moderation",
    # Automod (1 command)
    "automod": "automod",
    # Giveaway (1 command)
    "giveaway": "giveaway",
    # Colors (1 command)
    "color": "colors",
    # TempVoice (1 command)
    "tempvoice": "tempvoice",
    # Starboard (1 command)
    "starboard": "starboard",
    # Tickets (1 command)
    "ticket": "tickets",
    # Social & Animals (8 commands)
    "action": "social", "uwu": "social", "marry": "social", "divorce": "social",
    "ship": "social", "hunt": "social", "zoo": "social", "sellanimals": ("social", "economy"),
    # Fun & Entertainment (11 commands)
    "meme": "fun", "8ball": "fun", "meow": "fun", "meowsound": "fun", "tan": "fun", "kms": "fun",
    "quote": "fun", "addquote": "fun", "customquote": "fun", "customquotes": "fun", "delquote": "fun",
    # Welcome (1 command)
    "welcome": "welcome",
    # Core Utilities & System Info (Always accessible)
    "help": None, "dashboard": None, "sync": None, "ping": None,
    "serverinfo": None, "userinfo": None, "privacy": None, "tos": None,
    "setavatar": None, "setbanner": None, "updatedb": None
}

# Global module toggle interceptor for all slash commands
async def global_module_check(interaction: discord.Interaction) -> bool:
    if not interaction.guild_id:
        return True
    if not interaction.command:
        return True
    
    # Check root command name for subcommands (e.g. "giveaway" for "/giveaway start")
    root_cmd = interaction.command.root_parent.name if getattr(interaction.command, "root_parent", None) else interaction.command.name
    if root_cmd in ["help", "dashboard", "sync", "ping", "serverinfo", "userinfo", "privacy", "tos", "setavatar", "setbanner", "updatedb"]:
        return True
    
    modules = MODULE_COMMAND_MAP.get(root_cmd) or MODULE_COMMAND_MAP.get(interaction.command.name)
    if modules:
        if isinstance(modules, str):
            modules = (modules,)
        for mod in modules:
            if not bot.is_module_enabled(interaction.guild_id, mod):
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"⚠️ The **{mod.title()}** system is currently **turned off** on this server.\n"
                        f"An administrator can re-enable it instantly in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                        ephemeral=True
                    )
                return False
    return True

bot.tree.interaction_check = global_module_check

import asyncio
import random

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
        await bot.change_presence(activity=discord.Activity(type=atype, name=name), status=discord.Status.dnd)
        await asyncio.sleep(3600)  # Rotate every 60 minutes

@bot.event
async def on_ready():
    if getattr(bot, "_has_started", False):
        print("Bot reconnected to Discord gateway (ignoring duplicate on_ready).")
        return
    bot._has_started = True

    await db.setup()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    
    # Load module toggle states from database
    for guild in bot.guilds:
        try:
            bot.module_states[guild.id] = await db.get_guild_modules(guild.id)
            print(f"Loaded {len(bot.module_states[guild.id])} custom module states for {guild.name}.")
        except Exception as e:
            print(f"Could not load module states for {guild.name}: {e}")

    # Global error handler for app commands
    bot.tree.on_error = on_app_command_error
    
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
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] 🐾 Meow Bot is ONLINE and ready! Connected to {len(bot.guilds)} guild(s).", flush=True)

@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] ⚠️ App Command Error: {error} (Command: {interaction.command})", flush=True)

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
    token = generate_magic_token()
    dashboard_base = "https://meow-discord-bot.onrender.com"
    magic_link = f"{dashboard_base}/login?token={token}"
    
    embed = discord.Embed(
        title="🐾 Meow Control Center — Web Admin Dashboard",
        description=(
            "Manage all bot modules and features with **1:1 instant (0ms) real-time control**!\n\n"
            f"🔑 **1-Click Magic Login Link (Valid 24h):**\n[👉 Click Here to Open Web Admin Dashboard]({magic_link})\n\n"
            f"🌐 **Direct Dashboard URL:** `{dashboard_base}/dashboard`\n"
            f"⚡ **Direct Passkey:** Configured in `.env` (`ADMIN_PASSKEY`)"
        ),
        color=0x5865F2
    )
    embed.set_footer(text="🔒 Keep your magic link private — it grants full admin access.")
    
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open Dashboard ⚡", url=magic_link, style=discord.ButtonStyle.link))
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ You don't have permission to do that.", ephemeral=True)
    elif isinstance(error, app_commands.CommandOnCooldown):
        if not interaction.response.is_done():
            await interaction.response.send_message(f"⏰ Command is on cooldown. Try again in {error.retry_after:.2f}s.", ephemeral=True)
    elif isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Permission denied. You cannot execute this command.", ephemeral=True)
    else:
        # Don't leak stack traces to users, log it instead.
        print(f"Error in command {interaction.command.name if interaction.command else 'Unknown'}: {error}")
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message("❌ An unexpected error occurred. Please try again.", ephemeral=True)
            except:
                pass

async def main():
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

        try:
            await bot.start(TOKEN)
        except discord.LoginFailure:
            print("ERROR: Invalid DISCORD_TOKEN provided. Please check your .env file or host configuration.")
        except Exception as e:
            print(f"ERROR: {e}")

if __name__ == "__main__":
    asyncio.run(main())
