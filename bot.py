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
    
    # Global error handler for app commands
    bot.tree.on_error = on_app_command_error
    
    try:
        # 1. Sync all commands globally (zero duplicates, available in every channel/server)
        synced_global = await bot.tree.sync()
        print(f"✅ Successfully synced {len(synced_global)} global command(s) with zero duplicates.")

        # 2. Clear any lingering guild overrides so commands never duplicate
        for guild in bot.guilds:
            try:
                bot.tree.clear_commands(guild=guild)
                await bot.tree.sync(guild=guild)
            except Exception:
                pass
    except Exception as e:
        print(f"Slash command sync failed: {e}")

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

        # keep_alive() starts a tiny web server so free hosts (like Railway)
        # can ping the bot and see it as "alive". Harmless if unused.
        keep_alive()

        try:
            await bot.start(TOKEN)
        except discord.LoginFailure:
            print("ERROR: Invalid DISCORD_TOKEN provided. Please check your .env file or host configuration.")
        except Exception as e:
            print(f"ERROR: {e}")

if __name__ == "__main__":
    asyncio.run(main())
