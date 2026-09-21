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

from utils import hub
from utils.style import send_card

#: Discord's embed limits, named so the pagination is readable.
MAX_DESCRIPTION = 4096
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_FIELDS = 25
#: Discord's ceiling on all of an embed's text together.
MAX_TOTAL_TEXT = 6000

#: Where the unedited documents live for anyone who wants the full text.
#:
#: The published legal pages, not the repository they are written in. Discord
#: truncates an embed well before these documents end, and the old answer — *"the
#: remainder is in the repository: <github url>"* — sent every reader to a 404,
#: because the repository is private. A member asking what they agreed to is not
#: the audience for a source link; they are the audience for the page.
DOCUMENTS = f"{hub.marketing_url()}/{{page}}"


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


#: The field that says where the unedited documents are, and how much room it
#: needs. Reserved *before* the body is filled, not added afterwards: the first
#: version of this added it after the fill, which is how TERMS.md — which grew
#: past the ceiling when it gained the anti-AI clause and the arbitration section
#: — produced a card of 6,053 characters against Discord's 6,000 ceiling. Discord
#: rejects that whole embed, so the failure was not a truncated policy, it was
#: *no policy at all* in the one command a member runs to read it.
#:
#: The pointer is written once, here, and measured. Its own length is the
#: reservation, so the text can change without the arithmetic going stale.
REST_OF_IT = (
    "Every document in full, and always the current version, is on "
    f"the site: [Privacy]({DOCUMENTS.format(page='privacy')}) · "
    f"[Terms]({DOCUMENTS.format(page='terms')}) · "
    f"[AI & crawling]({DOCUMENTS.format(page='ai')})"
)


def _fill(embed: discord.Embed, sections, *, fields: int, total: int) -> tuple[int, bool]:
    """Add sections until one does not fit. ``(characters used, was cut)``."""
    used = len(embed.description or "")
    for heading, body in sections:
        for index, part in enumerate(_chunks(body, MAX_FIELD_VALUE)):
            name = heading if index == 0 else f"{heading} (cont.)"
            if len(embed.fields) >= fields or used + len(part) > total:
                return used, True
            embed.add_field(name=name[:MAX_FIELD_NAME], value=part, inline=False)
            used += len(part)
    return used, False


def document_embed(title: str, text: str, filename: str = "") -> discord.Embed:
    """One embed holding the whole document, section by section.

    Two passes when the document does not fit. The first asks whether it fits at
    all; when it does not, the body is filled again with the pointer's own
    length — and one field of the field budget — taken out of the allowance, so
    the card that is actually sent is inside every one of Discord's limits rather
    than inside them plus one field.
    """
    intro, sections = split_document(text)
    embed = discord.Embed(
        title=title, description=intro[:MAX_DESCRIPTION] or None, color=0x5865F2
    )

    used, cut = _fill(embed, sections, fields=MAX_FIELDS, total=MAX_TOTAL_TEXT)
    if cut or (sections and len(embed.fields) < len(sections)):
        embed.clear_fields()
        _fill(
            embed,
            sections,
            fields=MAX_FIELDS - 1,
            total=MAX_TOTAL_TEXT - len(REST_OF_IT),
        )
        embed.add_field(name="The rest of it", value=REST_OF_IT, inline=False)
    if filename:
        embed.set_footer(text=f"{filename} • {DOCUMENTS.format(page=filename.removesuffix('.md').lower())}")
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
