"""Discord Components V2 layouts — the richest message UI Discord currently allows.

This module is the single source of truth for how DaCat renders messages.
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

from utils import brand

# Components V2 turns card text into ``TextDisplay`` components, and — unlike
# legacy embeds, where mentions never notified — *mentions inside TextDisplay do
# notify*. Before this module existed every card was an embed, so nothing pinged;
# after the V2 upgrade every ``member.mention`` placed in a leaderboard started
# tagging people. Cards therefore suppress mentions by default, and a caller that
# genuinely wants to notify someone opts in explicitly with ``allowed_mentions``.
#
# The default is what broke ``/action``, ``/rizz`` and ``/roast``: those cards are
# *about* somebody and put ``member.mention`` in the text, so suppressing mentions
# left every one of them showing a name that never pinged. They now pass
# :func:`pings`, which is the whole of the opt-in.
NO_PINGS = discord.AllowedMentions.none()

# --------------------------------------------------------------------------- #
# Palette — one accent, four states, and nothing else
# --------------------------------------------------------------------------- #
# A card has one accent, and the accent is the colour of the bar down its left
# edge. This table used to hold nine colours, five of them decorative and four of
# them states, and none of the decorative five was used anywhere in the product —
# while the cogs picked their own hex instead (251 literals across `cogs/`, not
# one of which consulted this table). So the bar was a different colour from one
# command to the next and two cards side by side argued about which product they
# belonged to.
#
# A state is a claim rather than a decoration — *this worked*, *this is a
# warning*, *this failed*, *this is information* — so those four stay, and
# :data:`ACCENT_ALIASES` is the only place a colour becomes a state. Everything
# else that reaches a card becomes :data:`ACCENT`, which is the whole rule.
#
# The value comes from ``utils/brand.py`` rather than being typed here, because
# the accent is also declared in `static/meow.css` as a CSS token and the two had
# nothing holding them together: a rebrand was two edits, in two languages, and
# the failure of missing one is a site in one colour and every card in another —
# which reads as a bug in whichever half the reader saw second.
ACCENT = brand.ACCENT
SUCCESS = 0x23A55A
WARN = 0xF59E0B
DANGER = 0xF23F43
INFO = 0x3B82F6

#: The only colours a card's chrome may take, by role name.
ACCENTS: dict[str, int] = {
    "accent": ACCENT,
    "success": SUCCESS,
    "warn": WARN,
    "danger": DANGER,
    "info": INFO,
}

#: Values a cog is likely to have written by hand, and the state each of them
#: means. Discord's own palette (`0x57F287` green, `0xED4245` red, `0xFEE75C`
#: yellow, `0x5865F2` blurple) is here, plus the colours the cogs and the help
#: panel have used for years. A value **absent from this table is not a state**:
#: it becomes the accent, so a card cannot invent a sixth colour by writing a hex
#: literal.
ACCENT_ALIASES: dict[int, int] = {
    # green — a success, however it was spelled
    0x57F287: SUCCESS, 0x2ECC71: SUCCESS, 0x23A55A: SUCCESS, 0x1DB954: SUCCESS,
    # red — a failure or a destructive thing, however it was spelled
    0xED4245: DANGER, 0xE74C3C: DANGER, 0xF23F43: DANGER, 0xFF0000: DANGER,
    # amber — a warning, however it was spelled
    0xFEE75C: WARN, 0xF1C40F: WARN, 0xF39C12: WARN, 0xF59E0B: WARN, 0xE67E22: WARN,
    # blue — information, however it was spelled
    0x3498DB: INFO, 0x3B82F6: INFO,
    # the product's own accent, including the shade every bot used before it
    0x5865F2: ACCENT, 0x7289DA: ACCENT, 0x5B5EF0: ACCENT,
}


def accent_for(value, *, default: int = ACCENT) -> int:
    """The one accent a card may take, from whatever colour a caller passed.

    Accepts an embed's colour, a palette constant or a bare ``0x`` literal and
    answers a role colour: the value itself when it is already one of the five,
    the state it means when :data:`ACCENT_ALIASES` knows it, and the product
    accent for anything else — including ``None``, a string, and a number that is
    not a colour, so a malformed payload cannot paint a card a colour that does
    not exist.
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if not 0 <= number <= 0xFFFFFF:
        return default
    if number in ACCENTS.values():
        return number
    return ACCENT_ALIASES.get(number, default)


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
#: The File display component (discord.py 2.6+). A Components V2 message
#: **does not show an attachment unless a component references it** — the file is
#: uploaded, Discord accepts the message, and the attachment is simply never
#: drawn. That is why ``/meowsound`` once posted a clip nobody could see at all;
#: the File component that fixed that is what drew the clip as a **file row with a
#: download arrow** instead, which is why that command now uses :func:`send_plain`
#: rather than a card. When this is unavailable (an older library) attachments are
#: sent as their own follow-up message instead, the only other way to show one.
FileDisplay = getattr(discord.ui, "File", None)


async def send_plain(target, *, content=None, **extras):
    """Send a payload with **no card** — the client's own rendering of it.

    Everything else in this module routes through ``send_card``, which is right for
    text and wrong for one thing: a payload whose *attachment* has to be drawn by
    the client rather than by us. A Components V2 layout is what draws a card's
    message, and a File component has no player — so a clip on a card comes out as
    a **file row with a download arrow**, where the same bytes on a payload that is
    not a layout are drawn as a player. :func:`send_sound` is the caller that needs
    that; this function is the primitive, not the rule.
    """
    return await _dispatch_send(target, {"content": content, **extras}, False)


#: The payloads one sound clip is tried in, richest first, each being what is left
#: when Discord refuses the one above it. The **names** matter because "which shape
#: went out" is the answer to "why does it look like that" — and until this existed
#: there was no answer to give: a refusal landed in a catch-all and the member saw
#: whichever attempt happened to be last, which is how this feature shipped a
#: download row, and then a second message, without a line anywhere saying so.
SOUND_SHAPES = ("card", "clip", "word")


def _sound_ladder(embed, caption) -> list[str]:
    """The shapes worth trying for *this* payload, richest first.

    A caller with no card has no first rung, and reporting ``"card"`` for a
    payload that never carried one is the kind of quiet lie this module has already
    paid for once — so the ladder is filtered from what the caller actually has
    rather than from a fixed three.
    """
    offered = {"card": embed is not None, "clip": True, "word": caption is not None}
    return [shape for shape in SOUND_SHAPES if offered[shape]]


async def send_sound(target, *, path, embed=None, caption=None) -> str | None:
    """Post one sound clip on the richest payload Discord accepts, and say which.

    Returns the shape that landed (one of :data:`SOUND_SHAPES`), or ``None`` when
    every one of them was refused — so a caller that wants to tell an operator what
    happened has an answer to tell them with.

    **Why one message, and why not a card.** Two reports pinned this shape. A
    Components V2 layout is what draws a card's message and a File component has no
    player, so the clip came out as a *file row with a download arrow* — "why is
    there download, I don't wanna download, I wanna hear". The other shape Discord
    draws as a player is a **voice message**, but it may carry nothing beside its
    one attachment, so having one meant the card became a second message — "why
    double, I only want one like it was". So: the embed and the clip on one payload,
    richest first, and never two messages.

    **The ladder covers the third report, which was silence.** Which shape a member
    gets is decided by Discord's refusal, and a refusal this layer does not report
    is indistinguishable from a command that never ran: the voice message that
    answered "no sound" shipped *inert* for a commit because every clip landed in a
    catch-all and nothing said a word. So each refusal is printed with the shape
    that was refused and what Discord called it, and the last rung is a sentence
    naming the sound rather than nothing at all — the two refusals above it are the
    ones this command has actually produced; that floor is there so a third cannot
    be silence.

    What it does **not** promise is that the clip plays on every client. Discord's
    own guidance is that a Vorbis file "may not play back on mobile", and the
    soundboard's clips are Vorbis; a voice message is the shape that plays there,
    and it costs the picture and a second message to have one.
    """
    ladder = _sound_ladder(embed, caption)
    for shape in ladder:
        payload: dict = {}
        if shape == "card":
            payload["embed"] = embed
        if shape in ("card", "clip"):
            payload["file"] = discord.File(path)
        else:
            payload["content"] = caption

        try:
            await send_plain(target, **payload)
        except Exception as exc:
            # A refused upload can leave its handle open; the next rung reopens the
            # file, so this one is closed rather than left to the collector.
            refused_file = payload.get("file")
            if refused_file is not None:
                try:
                    refused_file.close()
                except Exception:
                    pass
            print(
                f"[STYLE] a sound clip was refused as {shape!r} "
                f"({type(exc).__name__}: {exc})",
                flush=True,
            )
            continue

        if shape != ladder[0]:
            print(
                f"[STYLE] a sound clip went out as {shape!r} rather than as {ladder[0]!r}",
                flush=True,
            )
        return shape

    print("[STYLE] no shape of a sound clip landed — nothing was posted", flush=True)
    return None


def v2_ready() -> bool:
    """True when the installed discord.py exposes Components V2."""
    return hasattr(discord.ui, "LayoutView") and hasattr(discord.ui, "Container")


def pings(*users) -> discord.AllowedMentions:
    """The opt-in ping set for a card that is *about* the listed people.

    Used by the roleplay/social commands, where the entire point is that the
    person is told something happened to them. Everything else keeps
    :data:`NO_PINGS`, because a leaderboard must never notify the whole server.

    ``everyone`` and ``roles`` are named explicitly because discord.py's
    ``AllowedMentions`` treats *omitted* as allowed: constructing one with only a
    user list leaves @everyone and every role pingable, which is not a set of
    "just this person" at all.
    """
    return discord.AllowedMentions(
        users=[u for u in users if u is not None],
        roles=False,
        everyone=False,
        replied_user=False,
    )


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


# --------------------------------------------------------------------------- #
# The type scale
# --------------------------------------------------------------------------- #
# Discord draws four sizes and one hairline, and a card that uses them without a
# rule is a title, five bold labels, a caption and a footer all shouting at the
# same volume — which is what a card built out of an embed looked like: an embed's
# field *names* were as loud as the thing they labelled, so nothing was the
# subject and everything was. There is one rung per job now, and the helpers
# below are the only places the markers are written:
#
#   `title()`    the subject, ``##``. One per card, enforced in ``_build_children``.
#   `section()`  a group inside a card, ``###``.
#   body         the words themselves, no marker at all.
#   `label()`    what a value *is*, and every caption: ``-#``, Discord's small text.
#   `subtext()`  the footer, same small rung, the last thing on the card.
def title(text: str, *, url: str | None = None) -> str:
    """The subject of a card: a level-2 heading, optionally linked."""
    if not text:
        return ""
    return f"## [{text}]({url})" if url else f"## {text}"


#: Kept because cogs import it by the old name; it is the title rung.
heading = title


def section(text: str) -> str:
    """A group heading inside a card — one rung below the title."""
    return f"### {text}" if text else ""


def label(text: str) -> str:
    """Small muted text: what a value is, a caption, an eyebrow above a title.

    Discord's ``-#`` marker, which is the only way to say "this line is quieter
    than the one below it" in a card, and therefore the whole of the field
    hierarchy: a field's *name* is a label and its *value* is body text, so the
    value is visibly the thing being read.
    """
    return f"-# {text}" if text else ""


subtext = label


def separator(*, visible: bool = True, large: bool = False):
    """One hairline, or one gap, with the scale the builders below use.

    ``visible`` draws the hairline; ``large`` doubles the space around it. The
    named levels in :data:`GAP`–:data:`RULE_LARGE` are what a card should reach
    for, because a separator placed by hand is how a card gets two rules in a row
    or a rule under its own title.
    """
    kwargs = {"visible": visible}
    spacing_cls = getattr(discord, "SeparatorSpacing", None) or getattr(discord.ui, "SeparatorSpacing", None)
    if spacing_cls is not None:
        kwargs["spacing"] = getattr(spacing_cls, "large" if large else "small")
    return Separator(**kwargs)


# --------------------------------------------------------------------------- #
# The spacing scale
# --------------------------------------------------------------------------- #
# Two gaps and two rules, and nothing in between: a card that spaces things *by
# hand* is a card whose sections drift apart one edit at a time. A gap is air, a
# rule is a boundary, and ``GAP``/``BREAK`` are unequal on purpose so that "these
# lines belong together" and "this is a new group" do not look the same.
GAP = separator(visible=False, large=False)
BREAK = separator(visible=False, large=True)
RULE = separator(visible=True, large=False)
RULE_LARGE = separator(visible=True, large=True)

#: The vertical order the levels rank in, so a card that has been given a gap and
#: then a rule keeps the rule: a hairline is a stronger statement than air.
_SPACER_RANK = {(False, False): 1, (False, True): 2, (True, False): 3, (True, True): 4}


def _spacer_shape(node):
    """``(visible, large)`` for a Separator, or ``None`` for anything else.

    Reads both a live component and a serialized one, because the same rule is
    applied to an assembled card and to the JSON the preview renderer walks.
    """
    if isinstance(node, dict):
        if node.get("type") != 14:
            return None
        return (bool(node.get("divider", True)), int(node.get("spacing", 1) or 1) == 2)
    if not isinstance(node, Separator):
        return None
    spacing = getattr(node, "spacing", 1)
    return (bool(getattr(node, "visible", True)), getattr(spacing, "value", spacing) == 2)


# --------------------------------------------------------------------------- #
# Text hygiene
# --------------------------------------------------------------------------- #
# A card is assembled from strings written across a dozen cogs, so the same three
# mistakes arrive over and over: a body that ends in a newline (which becomes a
# blank line *and* the container's own gap), a body that opens with one (which
# pushes the title off the top of the card), and a hand-drawn rule — a line of
# dashes or box characters meant as a divider, which lands directly above the
# card's own hairline and reads as a double rule with nothing between them.
#: Characters a divider is drawn out of, none of which carries meaning on its own.
_RULE_CHARS = "-–—_=~─━═▬*•·."


def _is_rule_line(line: str) -> bool:
    """True for a line that is nothing but divider characters.

    Three or more of them, so an em dash used as punctuation or a single ``-``
    bullet is left alone; a line of only whitespace is not a rule either. What
    this catches is somebody drawing their own ``━━━━`` between two halves of a
    card, which lands directly above the hairline the container already draws.
    """
    stripped = line.strip()
    if sum(1 for ch in stripped if ch in _RULE_CHARS) < 3:
        return False
    return all(ch in _RULE_CHARS or ch.isspace() for ch in stripped)


def clean_text(text) -> str:
    """Normalise a card string: no stray edges, one blank line, no drawn rules.

    Returns ``""`` for anything that is only whitespace or only rule characters,
    which is what lets the builders treat "empty" and "looks empty" the same way.
    """
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    kept: list[str] = []
    for line in lines:
        if _is_rule_line(line):
            continue  # a hand-drawn divider; the card draws its own
        if not line.strip():
            if not kept or not kept[-1].strip():
                continue  # collapse a run of blank lines into one
        kept.append(line)
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)


def _demote_headings(text: str) -> str:
    """Push a body's own ``##`` headings down a rung.

    A card has one subject, and it is the one in the title. A body that carried a
    second level-2 heading made the card argue with itself about what the subject
    was — the shape an embed's ``title`` plus a body starting with ``## `` arrives
    in — so the body's headings become groups instead.
    """
    return "\n".join(
        ("###" + line[2:]) if line.startswith("## ") else line
        for line in str(text or "").splitlines()
    )


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
    """Render (name, value) pairs as a label above its value.

    The pair used to be ``**name**`` on one line and the value under it, which
    put the label and the value at the same weight — so a card of eight fields
    was eight bold labels and eight plain values, with nothing saying which of
    them was the subject. The name is now a label (``-#``, Discord's small text)
    and the value is body text, which is the entire hierarchy a card of fields
    needs: read down the quiet lines to find the field, read across to get it.

    A field with a name and no value keeps its label rather than being dropped —
    an empty field in an embed usually means "not applicable", and the label is
    what says which thing is not applicable.
    """
    if not fields:
        return ""
    blocks = []
    for name, value in fields:
        name = clean_text(name)
        value = clean_text(value)
        if not name and not value:
            continue
        blocks.append(f"{label(name)}\n{value}" if name and value else (label(name) or value))
    return "\n\n".join(blocks)


def _place(children: list, node) -> None:
    """Append one child, holding the spacing invariants.

    Three rules, all of them about what a card *looks* like rather than what it
    contains, and the first version of this builder broke every one of them:

    * a spacer is never the first child (air under the top edge of the card) or
      the last (air above the bottom edge, which reads as a missing section);
    * two spacers never sit side by side — the stronger one wins, so a gap
      followed by a rule is just the rule, and the card cannot drift apart one
      edit at a time;
    * a rule is never placed where it separates nothing, which is what a hand-
      drawn divider in a body used to leave above it.
    """
    shape = _spacer_shape(node)
    if shape is None:
        children.append(node)
        return
    if not children:
        return  # leading air: there is nothing above it to separate from
    previous = _spacer_shape(children[-1])
    if previous is None:
        children.append(node)
        return
    if _SPACER_RANK[shape] > _SPACER_RANK[previous]:
        children[-1] = node  # keep the one that says more


def _close(children: list) -> list:
    """Finish a card: no trailing air, at least one component, under the cap."""
    while children and _spacer_shape(children[-1]) is not None:
        children.pop()
    if not children:
        children.append(TextDisplay("​"))
    return children[:MAX_V2_COMPONENTS]


def _build_children(
    *,
    title: str | None = None,
    body: str | None = None,
    thumbnail_url: str | None = None,
    image_urls: list[str] | None = None,
    footer: str | None = None,
    fields: list[tuple[str, str]] | None = None,
    extra: list | None = None,
    attachments: list | None = None,
) -> tuple[list, int | None]:
    """Assemble the children of a card, and say where its footer begins.

    The order is the hierarchy, top to bottom: the subject, what it says, the
    groups of facts, the picture, then the quiet line that says who and when. The
    footer's index comes back with the list because everything appended to a card
    *after* it is built — a merged embed's later fields, a route's extra content —
    has to land above the closing rule, or the rule ends up in the middle of the
    card separating something from nothing. That was the shape of the bug: a card
    with its footer line, then another paragraph under it.
    """
    children: list = []
    title_text = clean_text(title)
    body_text = clean_text(body)
    if title_text and body_text:
        # One subject per card, so a body's own level-2 heading becomes a group.
        body_text = _demote_headings(body_text)
    head = heading(title_text) if title_text else ""
    field_block = field_text(fields)
    footer_clean = clean_text(footer)
    footer_text = subtext(footer_clean) if footer_clean else ""

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
        _place(children, Section(TextDisplay(main), accessory=thumb))
    elif main:
        _place(children, TextDisplay(main))

    if field_block:
        _place(children, BREAK)
        _place(children, TextDisplay(field_block))

    media = gallery(*(image_urls or []))
    if media is not None:
        _place(children, BREAK)
        _place(children, media)

    # Attachments are referenced here, which is the only thing that makes Discord
    # draw them on a V2 message at all (see ``FileDisplay``).
    for node in file_components(attachments):
        _place(children, node)

    for node in (extra or []):
        _place(children, node)

    if footer_text:
        _place(children, RULE)
        _place(children, TextDisplay(footer_text))

    finished = _close(children)
    closing_at = None
    if footer_text:
        # The closing block is the footer *and* the rule directly above it, so new
        # content goes above both. Marking the footer's own index put an appended
        # paragraph between the two — which is the very shape this index exists to
        # prevent: a rule with a line of text on either side of it.
        closing_at = len(finished) - 1
        if closing_at and _spacer_shape(finished[closing_at - 1]) is not None:
            closing_at -= 1
    return finished, closing_at


# --------------------------------------------------------------------------- #
# Panel — the main card builder
# --------------------------------------------------------------------------- #
class Panel(LayoutView):
    """A premium Components V2 card with optional media, fields and action rows.

    Two things it guarantees, both of them properties of the card rather than of
    the caller: **one accent** — whatever colour came in, :func:`accent_for` has
    already reduced it to one of the five the product uses — and **one place for
    new content**, :meth:`append`, which puts it above the closing rule so the
    footer stays the last thing a reader sees.
    """

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
        attachments: list | None = None,
    ):
        super().__init__(timeout=timeout)
        self._accent = accent_for(accent)
        children, footer_at = _build_children(
            title=title,
            body=body,
            thumbnail_url=thumbnail,
            image_urls=gallery,
            footer=footer,
            fields=fields,
            attachments=attachments,
        )
        self._footer_at = footer_at
        self.container = Container(*children, accent_color=self._accent)
        self.add_item(self.container)

    @property
    def accent(self) -> int:
        """The one colour this card's chrome takes."""
        return self._accent

    def _child_list(self) -> list:
        """The container's current children.

        Read from ``_children`` because ``Container.children`` hands back a
        *copy* — a splice into that copy changed nothing at all, which is how the
        first version of :meth:`append` silently did nothing. Not named
        ``_children`` either: ``View`` keeps its own item list under that name, so
        a method called that is shadowed by the attribute the base class set, and
        the shadowing surfaces as a ``TypeError`` on the first append.
        """
        listed = getattr(self.container, "_children", None)
        if isinstance(listed, list):
            return listed
        listed = getattr(self.container, "children", None)
        return listed if isinstance(listed, list) else []

    def append(self, node) -> None:
        """Add a component to the card, above the footer rather than after it.

        This exists because two callers used to reach into ``container.add_item``
        directly: a merged embed's later fields and a route's extra content both
        ended up *below* the footer's hairline, which left the rule separating the
        footer from whatever arrived next. Going through here applies the spacing
        invariants too, and refuses to go over Discord's component cap.
        """
        children = list(self._child_list())
        if len(children) >= MAX_V2_COMPONENTS:
            return
        index = len(children) if self._footer_at is None else min(self._footer_at, len(children))
        head = children[:index]
        placed = len(head)
        _place(head, node)
        upgraded = len(head) == placed and head and head[-1] is not children[placed - 1]
        if len(head) == placed and not upgraded:
            return  # a spacer with nothing to separate, or a weaker one than the last
        ordered = head + children[index:]
        if len(ordered) == len(children) + 1 and index == len(children):
            self.container.add_item(node)  # a plain append: the public path
            return
        # Otherwise the component has to go *in* rather than on the end, and the
        # public API only appends — so the container is rebuilt and swapped, which
        # keeps ``add_item``'s own wiring (a hand-spliced child would be a
        # component the library does not know it has).
        previous = self.container
        try:
            replacement = Container(*ordered, accent_color=self._accent)
            self.remove_item(previous)
            self.container = replacement
            self.add_item(replacement)
        except Exception:
            # A view that will not take the rebuild still gets the content — at the
            # bottom, which is a footer in the wrong place rather than a lost
            # component, and never an exception inside a command.
            self.container = previous
            previous.add_item(node)
        else:
            if self._footer_at is not None:
                self._footer_at = index + (len(head) - placed)

    def add_row(self, *items: discord.ui.Item):
        """Append action rows (buttons/selects) inside the card, at the bottom.

        Deliberately *below* the footer: a row is a control strip rather than
        content, and putting the footer under the buttons would make the quiet
        line the last thing on an interactive card instead of the last thing
        said.

        **One call may become several rows**, because Discord's own rule for a
        row is one select or up to five buttons (see :func:`legal_rows`):
        ``add_row(select, button)`` is two rows, and a caller who hands over
        seven buttons gets two. Splitting here rather than at send time means the
        card is right from the moment it is built, and answers the last row it
        made.
        """
        rows = [ActionRow(*group) for group in _legal_groups(list(items))]
        if not rows:
            return None
        adder = getattr(self.container, "add_item", None)
        if not callable(adder):
            adder = self.add_item
        for row in rows:
            adder(row)
        return rows[-1]


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
    attachments: list | None = None,
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
        attachments=attachments,
    )


def unreferenced_files(attachments, media_urls) -> list:
    """The uploads no component already draws.

    ``embed.set_image(url="attachment://chart.png")`` becomes a MediaGallery item
    that *does* reference the upload, so that one is already visible — adding a
    File component for it as well would draw the same picture twice. Anything the
    media does not mention is the case this exists for: an audio clip, a
    transcript, a text file, which a V2 message would otherwise swallow.
    """
    if not attachments:
        return []
    referenced = set()
    for url in media_urls or ():
        if isinstance(url, str) and "attachment://" in url:
            referenced.add(url.split("attachment://", 1)[1])
    return [f for f in attachments if getattr(f, "filename", None) not in referenced]


def file_components(attachments) -> list:
    """Display components for uploaded files, or ``[]`` when there are none.

    Only objects that are actually an upload are referenced — a ``discord.File``
    (or anything else shaped like one). A component built around some other object
    would serialize into a reference to a file that is not in the payload, which
    Discord rejects at send time rather than at construction, and a failed send is
    how a command dies.
    """
    if not attachments or FileDisplay is None:
        return []
    out = []
    for attachment in attachments:
        if not (isinstance(attachment, discord.File) or (
            hasattr(attachment, "filename") and hasattr(attachment, "fp")
        )):
            continue
        try:
            out.append(FileDisplay(attachment))
        except Exception:
            continue
    return out[:MAX_V2_COMPONENTS]


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
def embed_to_layout(embed: discord.Embed, *, timeout: float | None = 180, attachments: list | None = None) -> Panel | None:
    """Convert a legacy embed into a Components V2 card.

    Returns ``None`` when there is nothing safe to render, so callers can fall
    back to sending the embed unchanged.

    The conversion is where the type scale earns its keep, because an embed has
    no scale to copy: its ``author`` becomes the eyebrow above the title, its
    ``description`` the body, and its ``fields`` label-and-value pairs. The
    embed's colour goes through :func:`accent_for` like every other colour, so the
    hundreds of ``0x`` literals in the cogs no longer each paint a card their own
    colour — only the ones that mean a state do.
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
        description = str(embed.description)
        # An embed with a title *and* a description that opens with its own
        # level-2 heading is two subjects in one card; the body's becomes a group.
        parts.append(_demote_headings(description) if title else description)

    body = "\n".join(parts).strip()

    footer = None
    embed_footer = getattr(embed, "footer", None)
    if embed_footer and embed_footer.text:
        footer = embed_footer.text
    if getattr(embed, "timestamp", None):
        stamp = embed.timestamp
        if isinstance(stamp, datetime.datetime):
            when = stamp.strftime("%Y-%m-%d %H:%M UTC")
            footer = f"{footer} · {when}" if footer else when

    fields = [(f.name, f.value) for f in (embed.fields or [])]

    thumb_url = None
    embed_thumb = getattr(embed, "thumbnail", None)
    if embed_thumb and getattr(embed_thumb, "url", None):
        thumb_url = embed_thumb.url

    image_urls: list[str] = []
    embed_image = getattr(embed, "image", None)
    if embed_image and getattr(embed_image, "url", None):
        image_urls.append(embed_image.url)

    if not thumb_url and not image_urls:
        # Nothing to draw on the right, so the author's own picture takes that
        # slot. Cards that set an author with the bot's avatar (help, tickets,
        # most of the tuneful ones) were otherwise a bare block of text, and the
        # icon was being thrown away because a converted embed has no author row.
        author_icon = getattr(author, "icon_url", None) if author else None
        if author_icon:
            thumb_url = str(author_icon)

    if not body and not fields and not image_urls and not attachments:
        return None

    return Panel(
        title="",
        body=body,
        accent=accent_for(getattr(getattr(embed, "colour", None), "value", None)),
        thumbnail=thumb_url,
        gallery=image_urls or None,
        footer=footer,
        fields=fields,
        timeout=timeout,
        attachments=unreferenced_files(attachments, [*image_urls, thumb_url]),
    )


def embeds_to_layout(
    embeds: list[discord.Embed], *, timeout: float | None = 180, attachments: list | None = None
) -> Panel | None:
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
    base = embed_to_layout(merged, timeout=timeout, attachments=attachments)
    if base is None:
        if not extra_fields:
            return None
        return Panel(title="", body="", fields=extra_fields, timeout=timeout, attachments=attachments)
    if extra_fields:
        base.append(TextDisplay(field_text(extra_fields)))
    return base


# --------------------------------------------------------------------------- #
# The one row shape Discord refuses
# --------------------------------------------------------------------------- #
# An ActionRow is either **one** select — which takes the row's whole width — or
# up to five buttons. Discord enforces that when the message is sent, and it says
# so in the only place a builder cannot see: the response to the request, as
# `50035 COMPONENT_LAYOUT_WIDTH_EXCEEDED` naming the component that overflowed.
# Nothing before the network call can catch it, because the layout is perfectly
# valid Python either way — which is how the `/help` panel spent its whole life
# answering every member with "An unexpected error occurred". Its category menu
# and its bug button were added to a single row; Discord's own answer was
# `components.0.components.3.components.1`, the button beside the menu.
#
# So the rule gets an implementation rather than a comment: every row that
# reaches Discord from this module is measured first, and a row that would be
# refused is rebuilt as the rows it should have been. Repair rather than refuse,
# for the same reason `_clamp` trims instead of raising — the member is waiting
# for an answer, and a card that is one row taller is an answer.
MAX_ROW_BUTTONS = 5

#: Every select kind shares ``BaseSelect`` (``Select``, ``UserSelect``,
#: ``RoleSelect``, ``ChannelSelect``, ``MentionableSelect``), so one isinstance
#: covers all five. Read defensively: an older library that lacks the base class
#: still gets the string select, which is the kind this project builds.
SELECT_TYPES = tuple(
    kind for kind in (getattr(discord.ui, "BaseSelect", None), discord.ui.Select) if kind
)


def _is_select(item) -> bool:
    return isinstance(item, SELECT_TYPES)


def _legal_groups(items: list) -> list[list]:
    """The rows ``items`` are allowed to be: one select each, five buttons each."""
    items = list(items)
    if not items:
        return []
    selects = [item for item in items if _is_select(item)]
    others = [item for item in items if not _is_select(item)]
    if not (len(selects) > 1 or (selects and others)):
        if len(items) <= (1 if selects else MAX_ROW_BUTTONS):
            return [items]
    groups = [[item] for item in selects]
    groups += [
        others[start : start + MAX_ROW_BUTTONS]
        for start in range(0, len(others), MAX_ROW_BUTTONS)
    ]
    return [group for group in groups if group]


def _repaired_row(row) -> list:
    """Legal replacements for one ActionRow, or ``[row]`` when it already is one."""
    items = list(getattr(row, "children", ()) or ())
    groups = _legal_groups(items)
    if len(groups) == 1 and len(groups[0]) == len(items):
        return [row]
    return [ActionRow(*group) for group in groups]


def _legalise(holder) -> int:
    """Rebuild refused rows anywhere under ``holder``. Answers how many were rebuilt."""
    children = list(getattr(holder, "children", ()) or ())
    if not children:
        return 0
    ordered: list = []
    rebuilt = 0
    for child in children:
        if isinstance(child, ActionRow):
            replacement = _repaired_row(child)
            if len(replacement) == 1 and replacement[0] is child:
                ordered.append(child)
            else:
                ordered.extend(replacement)
                rebuilt += 1
        else:
            rebuilt += _legalise(child)
            ordered.append(child)
    if rebuilt and len(ordered) != len(children):
        clear = getattr(holder, "clear_items", None)
        add = getattr(holder, "add_item", None)
        if callable(clear) and callable(add):
            clear()
            for item in ordered:
                add(item)
    return rebuilt


def legal_rows(view) -> int:
    """Make a Components V2 layout's rows ones Discord will accept.

    Call it before anything is sent or serialised. Answers the number of rows
    rebuilt, and says so in the host log when it changed anything, because a
    silent repair is a caller who never learns their row was refused.
    """
    rebuilt = _legalise(view)
    if rebuilt:
        print(
            f"[STYLE] Rebuilt {rebuilt} ActionRow(s) Discord would have refused "
            f"with 50035 COMPONENT_LAYOUT_WIDTH_EXCEEDED — a row holds one select "
            f"or up to {MAX_ROW_BUTTONS} buttons, never both.",
            flush=True,
        )
    return rebuilt


# --------------------------------------------------------------------------- #
# Sending helpers
# --------------------------------------------------------------------------- #
async def send_panel(target, view: LayoutView, *, ephemeral: bool = False, allowed_mentions=None):
    """Send a prebuilt Components V2 layout to an interaction or channel.

    Mentions are suppressed unless the caller passes ``allowed_mentions`` — see
    the note on ``NO_PINGS`` for why V2 cards must not ping by accident.
    """
    legal_rows(view)
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
    attachments: list | None = None,
) -> Panel | None:
    """Build a V2 layout from an embed/embeds/content, or None if not possible."""
    if not v2_ready():
        return None
    try:
        if embed is not None:
            layout = embed_to_layout(embed, timeout=timeout, attachments=attachments)
            if layout is not None and content:
                layout.append(TextDisplay(clean_text(content)))
            return layout
        if embeds:
            layout = embeds_to_layout(list(embeds), timeout=timeout, attachments=attachments)
            if layout is not None and content:
                layout.append(TextDisplay(clean_text(content)))
            return layout
        if content or attachments:
            return card(body=str(content or ""), timeout=timeout, attachments=attachments)
    except Exception:
        return None
    return None


def _split_attachments(extras: dict) -> list:
    """Pull ``file``/``files`` out of the keyword soup, as one ordered list.

    They are removed from ``extras`` on purpose: an attachment has to travel with
    the component that *references* it, so it is added back to the outbound
    payload deliberately rather than passed through by accident.
    """
    single = extras.pop("file", None)
    multi = extras.pop("files", None)
    files: list = []
    if isinstance(multi, (list, tuple)):
        files.extend(multi)
    elif multi is not None:
        files.append(multi)
    if single is not None:
        files.insert(0, single)
    return [f for f in files if f is not None]


def _attachment_kwargs(attachments: list) -> dict:
    """The one attachment keyword to send — never both."""
    if not attachments:
        return {}
    if len(attachments) == 1:
        return {"file": attachments[0]}
    return {"files": attachments}


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
    if isinstance(clean.get("view"), LayoutView):
        legal_rows(clean["view"])
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
    # An edit is the other way a layout reaches Discord, and the `/help` panel
    # edits itself in place when a category is picked — so the same rule has to
    # hold here, or the panel is refused one click after it was sent.
    if isinstance(clean.get("view"), LayoutView):
        legal_rows(clean["view"])
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

    ``file``/``files`` are referenced by the card's own File components, which is
    the only thing that makes Discord display an attachment on a V2 message. When
    the installed library has no File component the card is skipped entirely and
    the legacy payload is sent instead, where the attachment is visible as a file.
    """
    attachments = _split_attachments(extras)
    passthrough = {k: v for k, v in extras.items() if v is not None}
    file_kwargs = _attachment_kwargs(attachments)

    if view is not None:
        return await _dispatch_send(
            target,
            {"content": content, "embed": embed, "embeds": embeds, "view": view, **file_kwargs, **passthrough},
            ephemeral,
        )

    legacy = {"content": content, "embed": embed, "embeds": embeds, **file_kwargs, **passthrough}
    if attachments and FileDisplay is None:
        # Nothing would reference the file, so a card would swallow it.
        return await _dispatch_send(target, legacy, ephemeral)

    layout = _layout_from(embed, embeds, content, timeout, attachments=attachments)
    if layout is None:
        return await _dispatch_send(target, legacy, ephemeral)

    try:
        return await _dispatch_send(target, {"view": layout, **file_kwargs, **passthrough}, ephemeral)
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
