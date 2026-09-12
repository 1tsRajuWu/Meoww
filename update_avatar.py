import asyncio
import discord
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

class AvatarUpdater(discord.Client):
    async def on_ready(self):
        print("Logged in, updating avatar...")
        try:
            with open("/Users/raj/.gemini/antigravity/brain/f5e48eb8-b3f6-48d5-82f9-1d71c0adda91/.user_uploaded/media_1789235048835.jpg", "rb") as f:
                avatar_bytes = f.read()
            await self.user.edit(avatar=avatar_bytes)
            print("Avatar updated successfully!")
        except Exception as e:
            print(f"Error updating avatar: {e}")
        
        await self.close()

intents = discord.Intents.default()
client = AvatarUpdater(intents=intents)
client.run(TOKEN)
