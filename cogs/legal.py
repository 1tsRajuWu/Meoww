"""The Terms of Service and Privacy Policy, rendered from the markdown files.

The previous version cut the file at Discord's 4,096-character description
limit — so once a policy grew past that, its last sections were never shown and
nothing said so. The policies in this repository are already longer than that.
Now the intro becomes the description and each `##` section becomes a field,
paginated to Discord's limits, and the footer names the file it came from. A
test asserts that every heading in the file appears on the card.
"""
import os

import discord
from discord import app_commands
from discord.ext import commands

from utils.style import send_card

#: Discord's embed limits, named so the pagination is readable.
MAX_DESCRIPTION = 4096
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_FIELDS = 25
#: Discord's ceiling on all of an embed's text together.
MAX_TOTAL_TEXT = 6000

#: Where the unedited documents live, for anyone who wants the source text.
REPOSITORY = "https://github.com/1tsRajOwO/meow-discord-bot"


def read_document(filename: str) -> str:
    """The document's text, or an empty string when the file is missing."""
    path = os.path.join(os.path.dirname(__file__), "..", filename)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _chunks(text: str, size: int) -> list[str]:
    """Split ``text`` into pieces of at most ``size``, preferring paragraphs."""
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > size:
        cut = remaining.rfind("\n\n", 0, size)
        if cut < size // 2:
            cut = remaining.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def split_document(text: str) -> tuple[str, list[tuple[str, str]]]:
    """``(intro, [(heading, body), ...])`` for a markdown document."""
    intro: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in text.strip().splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            intro.append(line)
    return "\n".join(intro).strip(), [(head, "\n".join(body).strip()) for head, body in sections]


def document_embed(title: str, text: str, filename: str = "") -> discord.Embed:
    """One embed holding the whole document, section by section."""
    intro, sections = split_document(text)
    embed = discord.Embed(
        title=title, description=intro[:MAX_DESCRIPTION] or None, color=0x5865F2
    )

    used = len(embed.description or "")
    truncated = False
    for heading, body in sections:
        for index, part in enumerate(_chunks(body, MAX_FIELD_VALUE)):
            name = heading if index == 0 else f"{heading} (cont.)"
            if len(embed.fields) >= MAX_FIELDS or used + len(part) > MAX_TOTAL_TEXT:
                truncated = True
                break
            embed.add_field(name=name[:MAX_FIELD_NAME], value=part, inline=False)
            used += len(part)
        if truncated:
            break

    if truncated or sections and len(embed.fields) < len(sections):
        embed.add_field(
            name="Full documents",
            value=f"The remainder is in the repository: {REPOSITORY}",
            inline=False,
        )
    if filename:
        embed.set_footer(text=f"{filename} • {REPOSITORY}")
    return embed


class Legal(commands.Cog):
    """Terms of Service and Privacy Policy.

    Two documents, one command: at Discord's hundred-command ceiling every slot
    spent on a document nobody types twice is a feature that cannot exist, and
    ``/legal`` shows both without pretending they are separate features.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    legal_group = app_commands.Group(
        name="legal",
        description="The bot's Privacy Policy and Terms of Service.",
    )

    @legal_group.command(name="privacy", description="View the Bot's Privacy Policy.")
    async def privacy(self, interaction: discord.Interaction):
        text = read_document("PRIVACY.md") or "Document not found."
        embed = document_embed("Privacy Policy", text, "PRIVACY.md")
        await send_card(interaction, embed=embed, ephemeral=True)

    @legal_group.command(name="tos", description="View the Bot's Terms of Service.")
    async def tos(self, interaction: discord.Interaction):
        text = read_document("TERMS.md") or "Document not found."
        embed = document_embed("Terms of Service", text, "TERMS.md")
        await send_card(interaction, embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Legal(bot))
