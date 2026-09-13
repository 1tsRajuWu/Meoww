"""Discord Components V2 layouts — the richest message UI Discord currently allows."""
from __future__ import annotations

import discord

ACCENT = 0x5865F2
PINK = 0xEB459E
SUCCESS = 0x23A55A
DANGER = 0xF23F43
GOLD = 0xF1C40F
MUSIC = 0x1DB954
NIGHT = 0x111827


def v2_ready() -> bool:
    return hasattr(discord.ui, "LayoutView") and hasattr(discord.ui, "Container")


def avatar_url(user) -> str | None:
    if user is None:
        return None
    try:
        return user.display_avatar.url
    except Exception:
        return None


def guild_icon(guild) -> str | None:
    if guild is None or guild.icon is None:
        return None
    return guild.icon.url


def guild_banner(guild) -> str | None:
    if guild is None or getattr(guild, "banner", None) is None:
        return None
    return guild.banner.url


def _separator(*, visible: bool = True, large: bool = False):
    kwargs = {"visible": visible}
    spacing_cls = getattr(discord, "SeparatorSpacing", None) or getattr(discord.ui, "SeparatorSpacing", None)
    if spacing_cls is not None:
        kwargs["spacing"] = getattr(spacing_cls, "large" if large else "small")
    return discord.ui.Separator(**kwargs)


def _thumbnail(url: str | None):
    if not url:
        return None
    return discord.ui.Thumbnail(url)


def _gallery(*urls: str | None):
    items = [u for u in urls if u]
    if not items:
        return None
    return discord.ui.MediaGallery(*[discord.MediaGalleryItem(u) for u in items[:10]])


class Panel(discord.ui.LayoutView):
    """A premium Components V2 card with optional thumbnail, gallery, and action rows."""

    def __init__(
        self,
        *,
        title: str,
        body: str,
        accent: int = ACCENT,
        thumbnail: str | None = None,
        gallery: list[str] | None = None,
        footer: str | None = None,
        timeout: float | None = 180,
    ):
        super().__init__(timeout=timeout)
        self._accent = accent
        children: list = []

        heading = f"## {title}\n{body}" if title else body
        thumb = _thumbnail(thumbnail)
        if thumb is not None:
            children.append(discord.ui.Section(discord.ui.TextDisplay(heading), accessory=thumb))
        else:
            children.append(discord.ui.TextDisplay(heading))

        media = _gallery(*(gallery or []))
        if media is not None:
            children.append(_separator(visible=False, large=False))
            children.append(media)

        if footer:
            children.append(_separator(visible=True, large=False))
            children.append(discord.ui.TextDisplay(f"-# {footer}"))

        self.container = discord.ui.Container(*children, accent_color=accent)
        self.add_item(self.container)

    def add_row(self, *items: discord.ui.Item):
        row = discord.ui.ActionRow()
        for item in items:
            row.add_item(item)
        adder = getattr(self.container, "add_item", None)
        if callable(adder):
            adder(row)
        else:
            self.add_item(row)
        return row


async def send_panel(target, view: discord.ui.LayoutView, *, ephemeral: bool = False):
    kwargs = {"view": view}
    if isinstance(target, discord.Interaction):
        if ephemeral:
            kwargs["ephemeral"] = True
        if target.response.is_done():
            return await target.followup.send(**kwargs)
        return await target.response.send_message(**kwargs)
    return await target.send(view=view)


async def edit_to_panel(message: discord.Message, view: discord.ui.LayoutView):
    try:
        return await message.edit(content=None, embed=None, embeds=[], view=view)
    except TypeError:
        return await message.edit(content=None, embed=None, view=view)
