import discord
from discord import app_commands
from discord.ext import commands
import os
from utils.style import send_card

class Legal(commands.Cog):
    """Terms of Service and Privacy Policy."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def read_file(self, filename: str) -> str:
        filepath = os.path.join(os.path.dirname(__file__), "..", filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return "Document not found."

    @app_commands.command(name="privacy", description="View the Bot's Privacy Policy.")
    async def privacy(self, interaction: discord.Interaction):
        content = self.read_file("PRIVACY.md")
        embed = discord.Embed(
            title="Privacy Policy",
            description=content[:4096],  # Ensure it doesn't exceed Discord's limits
            color=discord.Color.blue()
        )
        await send_card(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="tos", description="View the Bot's Terms of Service.")
    async def tos(self, interaction: discord.Interaction):
        content = self.read_file("TERMS.md")
        embed = discord.Embed(
            title="Terms of Service",
            description=content[:4096],
            color=discord.Color.blue()
        )
        await send_card(interaction, embed=embed, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Legal(bot))
