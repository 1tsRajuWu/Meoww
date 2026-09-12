import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from keep_alive import keep_alive
from utils import db

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
    """Check module state in 0ms using in-memory RAM."""
    default_state = False if module_name == "welcome" else True
    guild_mods = bot.module_states.get(guild_id)
    if guild_mods is None:
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
    # Music
    "play": "music", "radio": "music", "nowplaying": "music", "pause": "music", "resume": "music",
    "skip": "music", "stop": "music", "queue": "music",
    # Leveling
    "rank": "leveling", "leaderboard": "leveling", "levelsettings": "leveling", "setlevel": "leveling",
    "resetxp": "leveling", "addlevelrole": "leveling", "removelevelrole": "leveling", "levelroles": "leveling",
    # Economy
    "balance": "economy", "daily": "economy", "pay": "economy", "work": "economy", "crime": "economy",
    "rob": "economy", "deposit": "economy", "withdraw": "economy", "gamble": "economy", "coinflip": "economy",
    "blackjack": "economy", "slots": "economy", "roulette": "economy", "shop": "economy", "buy": "economy",
    "inventory": "economy", "richlist": "economy", "eco-admin": "economy",
    # Moderation
    "ban": "moderation", "kick": "moderation", "mute": "moderation", "unmute": "moderation",
    "warn": "moderation", "warnings": "moderation", "delwarn": "moderation", "clearwarns": "moderation",
    "purge": "moderation", "lock": "moderation", "unlock": "moderation", "slowmode": "moderation",
    "modlogs": "moderation",
    # Automod
    "automod": "automod",
    # Giveaway
    "giveaway": "giveaway",
    # Colors
    "color": "colors",
    # TempVoice
    "tempvoice": "tempvoice",
    # Starboard
    "starboard": "starboard",
    # Tickets
    "ticket": "tickets",
    # Social & Fun
    "action": "social", "uwu": "social",
    "meme": "fun", "8ball": "fun", "flip": "fun", "roll": "fun", "quote": "fun", "addquote": "fun",
    # Games
    "tictactoe": "games", "rps": "games", "hangman": "games", "trivia": "games", "wordle": "games",
    # Welcome
    "welcome": "welcome"
}

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

    # Set up global module toggle check for all slash commands
    @bot.tree.interaction_check
    async def global_module_check(interaction: discord.Interaction) -> bool:
        if not interaction.guild_id:
            return True
        cmd_name = interaction.command.name if interaction.command else None
        if not cmd_name or cmd_name in ["help", "dashboard", "sync", "ping"]:
            return True
        
        module = MODULE_COMMAND_MAP.get(cmd_name)
        if module and not bot.is_module_enabled(interaction.guild_id, module):
            await interaction.response.send_message(
                f"⚠️ The **{module.title()}** system is currently **turned off** on this server.\n"
                f"An administrator can re-enable it instantly in the [Web Dashboard](https://meow-discord-bot.onrender.com).",
                ephemeral=True
            )
            return False
        return True

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
    print("Bot is online and ready.")

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
