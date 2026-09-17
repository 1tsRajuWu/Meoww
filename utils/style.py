"""Discord Components V2 layouts — the richest message UI Discord currently allows.

This module is the single source of truth for how Meow renders messages.
Everything routes through Components V2 (`LayoutView` + `Container` +
`Section` / `TextDisplay` / `Separator` / `MediaGallery` / `Thumbnail` /
`ActionRow`), which is the maximum-fidelity UI Discord currently exposes.

Key entry points
----------------
- ``Panel``        : build a rich card (title / body / fields / media / buttons).
- ``card``         : quick functional builder used by the semantic helpers.
- ``success/error/info/warn`` : one-line semantic feedback cards.
- ``send_panel`` / ``edit_to_panel`` : send or edit a prebuilt layout.
- ``send_card`` / ``edit_to_card``   : accept an ``Embed`` and render it as V2,
  falling back to a legacy embed if V2 is unavailable.
- ``embed_to_layout`` : best-effort, budget-aware ``Embed`` -> ``LayoutView``.

All builders are defensive: Discord rejects oversized Components V2 messages
(40 components / 4000 characters), so text is trimmed before it can escape.
"""
from __future__ import annotations

import asyncio
import datetime
import time

import discord

# Components V2 turns card text into ``TextDisplay`` components, and — unlike
# legacy embeds, where mentions never notified — *mentions inside TextDisplay do
# notify*. Before this module existed every card was an embed, so nothing pinged;
# after the V2 upgrade every ``member.mention`` placed in a leaderboard started
# tagging people. Cards therefore suppress mentions by default, and a caller that
# genuinely wants to notify someone opts in explicitly with ``allowed_mentions``.
NO_PINGS = discord.AllowedMentions.none()

# --------------------------------------------------------------------------- #
# Palette
# --------------------------------------------------------------------------- #
ACCENT = 0x5865F2
PINK = 0xEB459E
SUCCESS = 0x23A55A
DANGER = 0xF23F43
GOLD = 0xF1C40F
MUSIC = 0x1DB954
NIGHT = 0x111827
INFO = 0x3B82F6
WARN = 0xF59E0B
NEUTRAL = 0x95A5A6

# Discord hard limits for a Components V2 message.
MAX_V2_COMPONENTS = 40
MAX_V2_CHARS = 4000

# Component aliases so cogs can import one consistent set of names.
TextDisplay = discord.ui.TextDisplay
Separator = discord.ui.Separator
Section = discord.ui.Section
Thumbnail = discord.ui.Thumbnail
MediaGallery = discord.ui.MediaGallery
MediaGalleryItem = discord.MediaGalleryItem
Container = discord.ui.Container
ActionRow = discord.ui.ActionRow
LayoutView = discord.ui.LayoutView


def v2_ready() -> bool:
    """True when the installed discord.py exposes Components V2."""
    return hasattr(discord.ui, "LayoutView") and hasattr(discord.ui, "Container")


# --------------------------------------------------------------------------- #
# Primitive helpers
# --------------------------------------------------------------------------- #
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


def _clamp(text: str, limit: int = MAX_V2_CHARS) -> str:
    """Trim text so a layout never exceeds Discord's character budget."""
    text = str(text or "")
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def heading(text: str, *, url: str | None = None) -> str:
    """A level-2 markdown heading, optionally linked."""
    if not text:
        return ""
    return f"## [{text}]({url})" if url else f"## {text}"


def subtext(text: str) -> str:
    """Discord's small '-#' footer style."""
    return f"-# {text}" if text else ""


def separator(*, visible: bool = True, large: bool = False):
    kwargs = {"visible": visible}
    spacing_cls = getattr(discord, "SeparatorSpacing", None) or getattr(discord.ui, "SeparatorSpacing", None)
    if spacing_cls is not None:
        kwargs["spacing"] = getattr(spacing_cls, "large" if large else "small")
    return Separator(**kwargs)


def thumbnail(url: str | None):
    return Thumbnail(url) if url else None


def gallery(*urls: str | None):
    items = [u for u in urls if u]
    if not items:
        return None
    return MediaGallery(*[MediaGalleryItem(u) for u in items[:10]])


def progress_bar(current: float, total: float, *, width: int = 12) -> str:
    """A filled/empty bar for XP, cooldowns and goals."""
    try:
        total = float(total)
        ratio = 0.0 if total <= 0 else max(0.0, min(1.0, float(current) / total))
    except (TypeError, ValueError):
        ratio = 0.0
    filled = int(round(ratio * width))
    return f"`{'▰' * filled}{'▱' * (width - filled)}` {int(ratio * 100)}%"


def field_text(fields: list[tuple[str, str]] | None) -> str:
    """Render embed-style (name, value) pairs into one compact text block."""
    if not fields:
        return ""
    blocks = []
    for name, value in fields:
        name = str(name or "").strip()
        value = str(value or "").strip()
        if not name and not value:
            continue
        blocks.append(f"**{name}**\n{value}" if name else value)
    return "\n\n".join(blocks)


def _build_children(
    *,
    title: str | None = None,
    body: str | None = None,
    thumbnail_url: str | None = None,
    image_urls: list[str] | None = None,
    footer: str | None = None,
    fields: list[tuple[str, str]] | None = None,
    extra: list | None = None,
) -> list:
    """Assemble the ordered children of a card, respecting the text budget."""
    children: list = []
    head = heading(title) if title else ""
    body_text = str(body or "").strip()
    field_block = field_text(fields)
    footer_text = subtext(footer) if footer else ""

    # Every text component shares Discord's 4000-char budget for a V2 message.
    budget = MAX_V2_CHARS - len(footer_text)
    main = "\n".join(part for part in (head, body_text) if part)
    if len(main) > budget:
        joiner = 1 if (head and body_text) else 0
        body_text = _clamp(body_text, max(0, budget - len(head) - joiner))
        main = "\n".join(part for part in (head, body_text) if part)
    remaining = max(0, budget - len(main))
    if field_block and len(field_block) > remaining:
        field_block = _clamp(field_block, remaining)
    thumb = thumbnail(thumbnail_url)
    if main and thumb is not None:
        children.append(Section(TextDisplay(main), accessory=thumb))
    elif main:
        children.append(TextDisplay(main))

    if field_block:
        children.append(TextDisplay(field_block))

    media = gallery(*(image_urls or []))
    if media is not None:
        children.append(separator(visible=False))
        children.append(media)

    if extra:
        children.extend(extra)

    if footer_text:
        children.append(separator(visible=True))
        children.append(TextDisplay(footer_text))

    if not children:
        children.append(TextDisplay("​"))

    return children[:MAX_V2_COMPONENTS]


# --------------------------------------------------------------------------- #
# Panel — the main card builder
# --------------------------------------------------------------------------- #
class Panel(LayoutView):
    """A premium Components V2 card with optional media, fields and action rows."""

    def __init__(
        self,
        *,
        title: str,
        body: str,
        accent: int = ACCENT,
        thumbnail: str | None = None,
        gallery: list[str] | None = None,
        footer: str | None = None,
        fields: list[tuple[str, str]] | None = None,
        timeout: float | None = 180,
    ):
        super().__init__(timeout=timeout)
        self._accent = accent
        children = _build_children(
            title=title,
            body=body,
            thumbnail_url=thumbnail,
            image_urls=gallery,
            footer=footer,
            fields=fields,
        )
        self.container = Container(*children, accent_color=accent)
        self.add_item(self.container)

    def add_row(self, *items: discord.ui.Item):
        """Append an ActionRow (buttons/selects) inside the card."""
        row = ActionRow()
        for item in items:
            row.add_item(item)
        adder = getattr(self.container, "add_item", None)
        if callable(adder):
            adder(row)
        else:
            self.add_item(row)
        return row


def card(
    *,
    title: str | None = None,
    body: str | None = None,
    accent: int = ACCENT,
    thumbnail: str | None = None,
    gallery_urls: list[str] | None = None,
    footer: str | None = None,
    fields: list[tuple[str, str]] | None = None,
    timeout: float | None = 180,
) -> Panel:
    """Functional alias for :class:`Panel` used by the semantic helpers."""
    return Panel(
        title=title or "",
        body=body or "",
        accent=accent,
        thumbnail=thumbnail,
        gallery=gallery_urls,
        footer=footer,
        fields=fields,
        timeout=timeout,
    )


# Semantic one-liners ------------------------------------------------------- #
def success(body: str, *, title: str = "Success", **kw) -> Panel:
    return card(title=title, body=body, accent=SUCCESS, **kw)


def error(body: str, *, title: str = "Something went wrong", **kw) -> Panel:
    return card(title=title, body=body, accent=DANGER, **kw)


def info(body: str, *, title: str | None = None, **kw) -> Panel:
    return card(title=title, body=body, accent=INFO, **kw)


def warn(body: str, *, title: str = "Heads up", **kw) -> Panel:
    return card(title=title, body=body, accent=WARN, **kw)


# --------------------------------------------------------------------------- #
# Embed -> Components V2 conversion
# --------------------------------------------------------------------------- #
def embed_to_layout(embed: discord.Embed, *, timeout: float | None = 180) -> Panel | None:
    """Convert a legacy embed into a Components V2 card.

    Returns ``None`` when there is nothing safe to render, so callers can fall
    back to sending the embed unchanged.
    """
    if embed is None:
        return None

    title = embed.title or None
    url = getattr(embed, "url", None)
    header = heading(title, url=url) if title else ""

    parts: list[str] = []
    author = getattr(embed, "author", None)
    if author and author.name:
        parts.append(subtext(author.name))
    if header:
        parts.append(header)
    if embed.description:
        parts.append(str(embed.description))

    body = "\n".join(parts).strip()

    footer = None
    embed_footer = getattr(embed, "footer", None)
    if embed_footer and embed_footer.text:
        footer = embed_footer.text
    if getattr(embed, "timestamp", None):
        stamp = embed.timestamp
        if isinstance(stamp, datetime.datetime):
            footer = f"{footer} • {stamp.strftime('%Y-%m-%d %H:%M UTC')}" if footer else stamp.strftime("%Y-%m-%d %H:%M UTC")

    fields = [(f.name, f.value) for f in (embed.fields or [])]

    thumb_url = None
    embed_thumb = getattr(embed, "thumbnail", None)
    if embed_thumb and getattr(embed_thumb, "url", None):
        thumb_url = embed_thumb.url

    image_urls: list[str] = []
    embed_image = getattr(embed, "image", None)
    if embed_image and getattr(embed_image, "url", None):
        image_urls.append(embed_image.url)

    if not body and not fields and not image_urls:
        return None

    return Panel(
        title="",
        body=body,
        accent=embed.colour.value if getattr(embed, "colour", None) else ACCENT,
        thumbnail=thumb_url,
        gallery=image_urls or None,
        footer=footer,
        fields=fields,
        timeout=timeout,
    )


def embeds_to_layout(embeds: list[discord.Embed], *, timeout: float | None = 180) -> Panel | None:
    """Merge several embeds into one card (first wins for media/accent)."""
    usable = [e for e in (embeds or []) if e is not None]
    if not usable:
        return None
    merged = usable[0]
    extra_fields: list[tuple[str, str]] = []
    for embed in usable[1:]:
        if embed.title:
            extra_fields.append((embed.title, embed.description or ""))
        elif embed.description:
            extra_fields.append(("", embed.description))
    base = embed_to_layout(merged, timeout=timeout)
    if base is None:
        if not extra_fields:
            return None
        return Panel(title="", body="", fields=extra_fields, timeout=timeout)
    if extra_fields:
        base.container.add_item(TextDisplay(field_text(extra_fields)))
    return base


# --------------------------------------------------------------------------- #
# Sending helpers
# --------------------------------------------------------------------------- #
async def send_panel(target, view: LayoutView, *, ephemeral: bool = False, allowed_mentions=None):
    """Send a prebuilt Components V2 layout to an interaction or channel.

    Mentions are suppressed unless the caller passes ``allowed_mentions`` — see
    the note on ``NO_PINGS`` for why V2 cards must not ping by accident.
    """
    kwargs = {"view": view, "allowed_mentions": allowed_mentions or NO_PINGS}
    if isinstance(target, discord.Interaction):
        if ephemeral:
            kwargs["ephemeral"] = True
        if target.response.is_done():
            return await target.followup.send(**kwargs)
        return await target.response.send_message(**kwargs)
    return await target.send(view=view, allowed_mentions=kwargs["allowed_mentions"])


async def edit_to_panel(message: discord.Message, view: LayoutView, *, allowed_mentions=None):
    """Edit an existing message into a Components V2 layout."""
    try:
        return await message.edit(
            content=None, embed=None, embeds=[], view=view,
            allowed_mentions=allowed_mentions or NO_PINGS,
        )
    except TypeError:
        return await message.edit(content=None, embed=None, view=view)


def _layout_from(
    embed: discord.Embed | None,
    embeds: list[discord.Embed] | None,
    content: str | None,
    timeout: float | None,
) -> Panel | None:
    """Build a V2 layout from an embed/embeds/content, or None if not possible."""
    if not v2_ready():
        return None
    try:
        if embed is not None:
            layout = embed_to_layout(embed, timeout=timeout)
            if layout is not None and content:
                layout.container.add_item(TextDisplay(str(content)))
            return layout
        if embeds:
            layout = embeds_to_layout(list(embeds), timeout=timeout)
            if layout is not None and content:
                layout.container.add_item(TextDisplay(str(content)))
            return layout
        if content:
            return card(body=str(content), timeout=timeout)
    except Exception:
        return None
    return None


def _one_embed(embed: discord.Embed | None, embeds: list[discord.Embed] | None) -> dict:
    """The single embed keyword to send — never both.

    discord.py raises ``TypeError('Cannot mix embed and embeds keyword
    arguments.')`` when a payload carries ``embed`` *and* ``embeds`` at once,
    checking for the keys rather than for values, so ``embed=None`` beside
    ``embeds=[]`` is a payload that can never be sent at all.
    """
    if embeds is not None:
        return {"embeds": embeds}
    return {"embed": embed}


def _is_v2_message(target) -> bool:
    """True when this target is an already-sent Components V2 message.

    Such a message may not carry content or embeds, so a legacy edit is not a
    valid fallback for it: Discord refuses the payload and reports that instead
    of whatever actually went wrong.
    """
    return bool(getattr(getattr(target, "flags", None), "components_v2", False))


def _is_interaction(target) -> bool:
    """True for a real Interaction (or a duck-typed stand-in)."""
    if isinstance(target, discord.Interaction):
        return True
    return (
        hasattr(target, "response")
        and hasattr(target, "followup")
        and hasattr(target, "edit_original_response")
        and hasattr(target, "response")
        and hasattr(getattr(target, "response"), "is_done")
    )


async def _dispatch_send(target, payload: dict, ephemeral: bool):
    """Low-level send to an Interaction (response/followup) or any Messageable."""
    clean = {k: v for k, v in payload.items() if v is not None}
    clean.setdefault("allowed_mentions", NO_PINGS)
    if _is_interaction(target):
        if ephemeral:
            clean["ephemeral"] = True
        if target.response.is_done():
            return await target.followup.send(**clean)
        return await target.response.send_message(**clean)
    return await target.send(**clean)


async def _dispatch_edit(target, payload: dict):
    """Low-level edit of an Interaction's message or a Message.

    Note: explicit ``None`` values are preserved here on purpose — an edit that
    upgrades a legacy embed to V2 must actively clear ``content``/``embed``, so
    this deliberately does not filter them out the way the send path does.
    """
    clean = dict(payload)
    clean.setdefault("allowed_mentions", NO_PINGS)
    if _is_interaction(target):
        if target.response.is_done():
            return await target.edit_original_response(**clean)
        return await target.response.edit_message(**clean)
    return await target.edit(**clean)


async def send_card(
    target,
    *,
    embed: discord.Embed | None = None,
    embeds: list[discord.Embed] | None = None,
    content: str | None = None,
    ephemeral: bool = False,
    view=None,
    timeout: float | None = 180,
    **extras,
):
    """Send `content`/`embed(s)` as a Components V2 card, with a safe fallback.

    Works with an ``Interaction`` (response or followup) or any Messageable
    (channel, member, thread). If V2 is unavailable, an embed cannot be
    converted, or Discord rejects the layout, the original payload is sent
    unchanged so no call site can break.

    A legacy interactive ``view`` is passed straight through, because merging a
    legacy View into a layout would silently drop its timeout and
    ``interaction_check`` guards.
    """
    passthrough = {k: v for k, v in extras.items() if v is not None}

    if view is not None:
        return await _dispatch_send(
            target, {"content": content, "embed": embed, "embeds": embeds, "view": view, **passthrough}, ephemeral
        )

    legacy = {"content": content, "embed": embed, "embeds": embeds, **passthrough}
    layout = _layout_from(embed, embeds, content, timeout)
    if layout is None:
        return await _dispatch_send(target, legacy, ephemeral)

    try:
        return await _dispatch_send(target, {"view": layout, **passthrough}, ephemeral)
    except Exception:
        # A rejected payload must never take the command down with it.
        return await _dispatch_send(target, legacy, ephemeral)


async def edit_card(
    target,
    *,
    embed: discord.Embed | None = None,
    embeds: list[discord.Embed] | None = None,
    content: str | None = None,
    view=None,
    timeout: float | None = 180,
    **extras,
):
    """Edit an interaction's message (or a Message) into a V2 card.

    Falls back to the original embed/content so an edit can never fail the
    command. When upgrading an existing message to V2, legacy content and
    embeds are explicitly cleared first, since a V2 message cannot carry them.
    """
    passthrough = {k: v for k, v in extras.items() if v is not None}

    if view is not None:
        return await _dispatch_edit(
            target, {**_one_embed(embed, embeds), "content": content, "view": view, **passthrough}
        )

    legacy = {**_one_embed(embed, embeds), "content": content, **passthrough}
    layout = _layout_from(embed, embeds, content, timeout)
    if layout is None:
        return await _dispatch_edit(target, {k: v for k, v in legacy.items() if v is not None})

    # A V2 message carries neither content nor embeds, so an upgrade clears both
    # — with exactly one embed keyword. Sending ``embed=None`` beside
    # ``embeds=[]`` raised ``TypeError`` inside discord.py on every V2 edit,
    # which dropped every one of them into the legacy retry below.
    v2_payload = {"content": None, "embeds": [], "view": layout, **passthrough}
    try:
        return await _dispatch_edit(target, v2_payload)
    except Exception:
        if _is_v2_message(target):
            raise  # the retry cannot work here; report what really failed
        return await _dispatch_edit(target, {k: v for k, v in legacy.items() if v is not None})


async def edit_to_card(message: discord.Message, *, embed: discord.Embed | None = None, content: str | None = None):
    """Edit a Message into a V2 card, falling back to a legacy edit."""
    return await edit_card(message, embed=embed, content=content)


# --------------------------------------------------------------------------- #
# Display names — real names for lists, never mentions
# --------------------------------------------------------------------------- #
# Leaderboards used to print ``member.mention``. That was harmless while cards
# were embeds, but V2 promotes the text to a TextDisplay, where a mention pings.
# ``guild.get_member`` also returns ``None`` whenever the Members intent is off,
# which is how a rich list ended up showing raw snowflakes (``User 123…``).
# These helpers resolve a plain-text name that is always safe to display.
_NAME_CACHE: dict[tuple[int, int], tuple[float, str]] = {}
_NAME_TTL = 900.0
_NAME_CACHE_MAX = 4096


def invalidate_name_cache() -> None:
    """Drop cached display names (call on member update / leave)."""
    _NAME_CACHE.clear()


def member_cache_complete(client) -> bool:
    """True only when *every* guild member is cached (Members intent on).

    Without the intent the cache holds the bot itself plus anyone who has been
    in voice, so ``guild.owner`` is ``None`` and ``role.members`` is near-empty.
    Anything that reports a count or a total must ask first instead of printing
    a confidently wrong number.
    """
    return bool(getattr(getattr(client, "intents", None), "members", False))


async def display_name(guild, user_id: int, *, client=None) -> str:
    """Return a plain-text display name for a guild member.

    Resolution order: local cache -> member cache (Members intent) -> REST
    ``fetch_member`` (works without the intent) -> global user -> ``User <id>``.
    Never returns a mention, so it cannot ping and is safe inside a V2 card.
    """
    if not user_id:
        return "Unknown"

    guild_id = getattr(guild, "id", 0) or 0
    key = (guild_id, user_id)
    now = time.monotonic()
    cached = _NAME_CACHE.get(key)
    if cached and now - cached[0] < _NAME_TTL:
        return cached[1]

    name = None

    if guild is not None:
        member = guild.get_member(user_id)
        if member is not None:
            name = member.display_name

    if not name and guild is not None:
        try:
            name = (await guild.fetch_member(user_id)).display_name
        except Exception:
            name = None

    if not name:
        api = client or getattr(getattr(guild, "_state", None), "client", None)
        fetch_user = getattr(api, "fetch_user", None)
        if fetch_user is not None:
            try:
                user = await fetch_user(user_id)
                name = getattr(user, "display_name", None) or getattr(user, "name", None)
            except Exception:
                name = None

    if not name:
        name = f"User {user_id}"

    if len(_NAME_CACHE) >= _NAME_CACHE_MAX:
        _NAME_CACHE.clear()
    _NAME_CACHE[key] = (now, name)
    return name


async def display_names(guild, user_ids, *, client=None) -> list[str]:
    """Resolve many names at once (concurrently, so a top-10 is one round-trip)."""
    return list(await asyncio.gather(*(display_name(guild, uid, client=client) for uid in user_ids)))
