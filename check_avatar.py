import asyncio
import discord
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

class AvatarChecker(discord.Client):
    async def on_ready(self):
        print(f"Current Avatar URL: {self.user.avatar.url if self.user.avatar else 'None'}")
        await self.close()

intents = discord.Intents.default()
client = AvatarChecker(intents=intents)
client.run(TOKEN)
