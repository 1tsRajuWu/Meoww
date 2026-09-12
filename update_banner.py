import asyncio
import discord
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

class BannerUpdater(discord.Client):
    async def on_ready(self):
        print("Logged in, updating banner...")
        with open("/Users/raj/.gemini/antigravity/brain/f5e48eb8-b3f6-48d5-82f9-1d71c0adda91/.user_uploaded/media_1789235048835.jpg", "rb") as f:
            banner_bytes = f.read()
        
        try:
            await self.user.edit(banner=banner_bytes)
            print("Banner updated successfully!")
        except Exception as e:
            print(f"Error updating banner: {e}")
        
        await self.close()

intents = discord.Intents.default()
client = BannerUpdater(intents=intents)
client.run(TOKEN)
