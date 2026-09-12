import asyncio
import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

COGS = [
    "cogs.moderation", "cogs.tickets", "cogs.server_control",
    "cogs.welcome", "cogs.reaction_roles", "cogs.automod",
    "cogs.help", "cogs.legal", "cogs.bot_config",
    "cogs.economy", "cogs.games", "cogs.fun",
]

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    for cog in COGS:
        try:
            await bot.load_extension(cog)
            print(f"  ✓ Loaded {cog}")
        except Exception as e:
            print(f"  ✗ {cog}: {e}")
    
    print("\nSyncing slash commands globally...")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} commands:")
        for cmd in synced:
            print(f"   /{cmd.name}")
    except Exception as e:
        print(f"❌ Sync failed: {e}")
    
    await bot.close()

bot.run(TOKEN)
