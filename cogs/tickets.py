import asyncio
import datetime
import io

import discord
from discord import app_commands
from discord.ext import commands

from utils import db
from utils.style import Panel, send_panel, ACCENT, guild_icon, guild_banner, avatar_url, send_card

TICKET_CATEGORIES = {
    "general": {
        "name": "General Support & Questions",
        "emoji": "❓",
        "slug": "support",
        "desc": "Ask questions about the server, roles, rules, or general help"
    },
    "report": {
        "name": "Report a Member / Rule Violation",
        "emoji": "🚨",
        "slug": "report",
        "desc": "Report a member breaking rules, harassment, scamming, or toxicity"
    },
    "technical": {
        "name": "Technical / Setup Help",
        "emoji": "🖥️",
        "slug": "tech",
        "desc": "Help with installs, launches, crashes, or other technical issues"
    },
    "trading": {
        "name": "Trading",
        "emoji": "🤝",
        "slug": "trading",
        "desc": "In-game trades, item exchange, trade middleman, or marketplace questions"
    },
    "games": {
        "name": "Games",
        "emoji": "🎮",
        "slug": "games",
        "desc": "Game recommendations, gameplay assistance, requests, or multiplayer"
    },
    "economy": {
        "name": "Economy & Money Support",
        "emoji": "💰",
        "slug": "economy",
        "desc": "Issues with wallet balance, bank, betting games, or shop items"
    },
    "bug": {
        "name": "Bug Report & Glitch",
        "emoji": "🛠️",
        "slug": "bug",
        "desc": "Report a bot bug, unexpected error, or technical server glitch"
    },
    "feedback": {
        "name": "Server Feedback & Suggestion",
        "emoji": "💡",
        "slug": "feedback",
        "desc": "Propose an idea or give suggestions to improve the community"
    }
}


# --------------------------------------------------------------------------- #
# Post-close feedback
# ---------------------------------------------------------------------------
#: The ticket channel waits this long for a rating before deleting itself.
FEEDBACK_GRACE_SECONDS = 60
#: The DM copy of the prompt keeps working for a day, so an opener who was away
#: when the ticket closed can still rate it.
FEEDBACK_DM_TIMEOUT = 24 * 60 * 60
#: A rating reads as a colour, so the log channel can be scanned at a glance.
FEEDBACK_COLORS = {1: 0xED4245, 2: 0xE67E22, 3: 0xF1C40F, 4: 0x57F287, 5: 0x2ECC71}

#: channel_id -> Event, set the moment the rating is in so the closer deletes now.
_FEEDBACK_EVENTS: dict[int, asyncio.Event] = {}
#: Strong references to the watcher tasks; a bare task can be garbage collected.
_FEEDBACK_TASKS: set[asyncio.Task] = set()


def _int_id(raw) -> int | None:
    """A snowflake, or ``None`` — a topic is text, so nothing here is trusted."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def ticket_topic_info(channel) -> dict:
    """Author, claimer and category as recorded in a ticket channel's topic.

    The topic is the only ticket state that survives a restart, so the close
    button reads it back instead of trusting anything held in memory.
    """
    info = {"author_id": None, "claimed_by": None, "category": "general"}
    for part in str(getattr(channel, "topic", "") or "").split("|"):
        key, _, value = part.strip().partition(":")
        key = key.strip().lower()
        if key == "author":
            info["author_id"] = _int_id(value)
        elif key == "claimed":
            info["claimed_by"] = _int_id(value)
        elif key == "category":
            info["category"] = value.strip() or "general"
    return info


def clamp_score(score) -> int:
    """A rating between 1 and 5; anything unreadable becomes 1."""
    try:
        value = int(score)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(5, value))


def star_bar(score) -> str:
    """``4`` -> ``★★★★☆``. Anything unreadable or out of range is clamped."""
    value = clamp_score(score)
    return "★" * value + "☆" * (5 - value)


class TicketFeedbackModal(discord.ui.Modal):
    """Optional comment that rides along with the star the opener picked."""

    def __init__(self, *, channel_id: int, score: int):
        super().__init__(title=f"Ticket Feedback — {score}/5")
        self.channel_id = channel_id
        self.score = score
        self.comment = discord.ui.TextInput(
            label="Anything else? (optional)",
            placeholder="What went well, or what could have gone better...",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=False,
        )
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction):
        await submit_ticket_feedback(
            interaction,
            channel_id=self.channel_id,
            score=self.score,
            comment=(self.comment.value or "").strip(),
        )


def feedback_prompt(channel_id: int, *, timeout: float | None) -> Panel:
    """The star-rating card, posted in the ticket and DMed to the opener.

    The ticket's id travels in every button's ``custom_id``, so a rating tapped
    in a DM resolves the same record the in-channel copy does — no per-ticket
    view has to be re-registered after a restart.
    """
    panel = Panel(
        title="⭐ How did we do?",
        body=(
            "Thanks for reaching out! Pick a star to rate the support you got, then "
            "add a comment if you want to say more.\n"
            "-# The ticket channel closes as soon as the rating is in."
        ),
        accent=ACCENT,
        footer=f"Ticket {channel_id}",
        timeout=timeout,
    )
    buttons = []
    for score in range(1, 6):
        button = discord.ui.Button(
            label=f"{score} ⭐",
            style=discord.ButtonStyle.secondary,
            custom_id=f"ticket_fb:{channel_id}:{score}",
        )
        button.callback = _rating_callback(channel_id, score)
        buttons.append(button)
    panel.add_row(*buttons)
    return panel


def _rating_callback(channel_id: int, score: int):
    async def rate(interaction: discord.Interaction):
        await interaction.response.send_modal(
            TicketFeedbackModal(channel_id=channel_id, score=score)
        )

    return rate


async def _feedback_reply(interaction: discord.Interaction, text: str):
    """Reply to the rating. Ephemeral only where Discord allows it."""
    kwargs = {"content": text}
    if interaction.guild is not None:
        kwargs["ephemeral"] = True
    if interaction.response.is_done():
        return await interaction.followup.send(**kwargs)
    return await interaction.response.send_message(**kwargs)


async def submit_ticket_feedback(
    interaction: discord.Interaction, *, channel_id: int, score: int, comment: str
):
    """Record a rating, log it with the people involved, then close the ticket."""
    row = await db.get_ticket_feedback(channel_id)
    if row is None:
        return await _feedback_reply(
            interaction, "❌ I lost track of that ticket, so the rating was not saved."
        )
    rating = clamp_score(score)
    if not await db.record_ticket_feedback(channel_id, rating, comment):
        return await _feedback_reply(interaction, "✅ You already rated this ticket — thank you!")

    # Answer first: the log post and thread creation can outlast the 3-second
    # window an interaction token is valid for.
    await _feedback_reply(
        interaction,
        f"💖 Thanks for the **{rating}/5** rating — it has been shared with the team.",
    )

    guild = interaction.client.get_guild(_int_id(row.get("guild_id")) or 0)
    if guild is None:
        print(
            f"[TICKETS] Rating for ticket {channel_id} was saved but guild "
            f"{row.get('guild_id')} is gone; it was not logged.",
            flush=True,
        )
        return

    await _log_ticket_feedback(guild, row=row, score=rating, comment=comment)

    event = _FEEDBACK_EVENTS.get(channel_id)
    if event is not None:
        event.set()
    else:
        # Rated from the DM once the gate had already closed. If the channel
        # outlived its watcher (a restart, say) it still gets cleaned up.
        channel = guild.get_channel(channel_id)
        if channel is not None:
            await _close_ticket_channel(channel)


async def _log_ticket_feedback(guild: discord.Guild, *, row: dict, score: int, comment: str):
    """Post the rating to the guild's log channel and open a follow-up thread."""
    channel_id = _int_id(await db.get_setting(guild.id, "log_channel_id"))
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        print(
            f"[TICKETS] Guild {guild.id} has no usable log channel (log_channel_id="
            f"{channel_id}); feedback for ticket {row.get('ticket_channel_id')} was not posted.",
            flush=True,
        )
        return

    opener_id = _int_id(row.get("author_id"))
    handler_id = _int_id(row.get("claimed_by"))
    people = [uid for uid in (opener_id, handler_id) if uid]
    rating = clamp_score(score)
    embed = discord.Embed(
        title=f"{star_bar(rating)}  Ticket Feedback — {rating}/5",
        description=comment or "*No comment left.*",
        color=FEEDBACK_COLORS.get(rating, ACCENT),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="🎫 Ticket", value=f"`{row.get('ticket_channel_id')}`", inline=True)
    embed.add_field(name="📂 Category", value=f"`{row.get('category') or 'general'}`", inline=True)
    embed.add_field(
        name="👤 Opened By",
        value=f"<@{opener_id}>" if opener_id else "`unknown`",
        inline=True,
    )
    embed.add_field(
        name="🛠️ Handled By",
        value=f"<@{handler_id}>" if handler_id else "*Nobody claimed this ticket*",
        inline=True,
    )
    embed.set_footer(text="Ticket feedback")

    try:
        message = await send_card(
            channel,
            content=("📣 New ticket feedback — " + " ".join(f"<@{uid}>" for uid in people))
            if people
            else "📣 New ticket feedback",
            embed=embed,
            # The two people involved are the only ones this card may ping.
            allowed_mentions=discord.AllowedMentions(
                users=[discord.Object(id=uid) for uid in people], roles=False, everyone=False
            ),
            timeout=None,
        )
    except Exception as e:
        print(
            f"[TICKETS] Could not post feedback for ticket {row.get('ticket_channel_id')} "
            f"in #{getattr(channel, 'name', channel_id)}: {e}",
            flush=True,
        )
        return

    try:
        await message.create_thread(
            name=f"Feedback {star_bar(rating)}"[:100], auto_archive_duration=1440
        )
    except Exception as e:
        # The feedback is already in the channel; a missing Create Threads
        # permission must not look like the whole feature failed.
        print(f"[TICKETS] Feedback posted but its thread could not be created: {e}", flush=True)


async def _close_ticket_channel(channel) -> None:
    """Delete a closed ticket, saying so when Discord refuses."""
    try:
        await channel.delete(reason="Ticket closed")
    except discord.NotFound:
        pass
    except Exception as e:
        # The closing panel promised this channel would disappear. If the delete
        # fails (usually a missing Manage Channels permission) the ticket stays
        # open, so say so rather than leaving it a mystery.
        print(f"[TICKETS] Could not delete ticket channel {channel.id}: {e}", flush=True)
        try:
            await channel.send(
                "⚠️ I could not delete this channel (missing **Manage Channels**?). "
                "A staff member can remove it manually."
            )
        except Exception:
            pass


async def _close_when_rated(bot, channel_id: int, event: asyncio.Event) -> None:
    """Delete the ticket channel once the rating is in, or after the grace period."""
    try:
        await asyncio.wait_for(event.wait(), timeout=FEEDBACK_GRACE_SECONDS)
    except asyncio.TimeoutError:
        pass
    _FEEDBACK_EVENTS.pop(channel_id, None)
    channel = bot.get_channel(channel_id)
    if channel is not None:
        await _close_ticket_channel(channel)


def _watch_feedback(bot, channel_id: int, event: asyncio.Event) -> None:
    """Start the closer without holding the close button's interaction open."""
    task = asyncio.create_task(_close_when_rated(bot, channel_id, event))
    _FEEDBACK_TASKS.add(task)
    task.add_done_callback(_FEEDBACK_TASKS.discard)


async def _ask_for_feedback(interaction: discord.Interaction, info: dict) -> bool:
    """Post the rating prompt in the ticket, and DM the opener a copy.

    Returns ``False`` when there is nobody to ask, so the caller can fall back to
    closing the channel the way it used to.
    """
    author_id = info.get("author_id")
    if not author_id:
        return False

    channel = interaction.channel
    await db.open_ticket_feedback(
        guild_id=interaction.guild.id,
        channel_id=channel.id,
        author_id=author_id,
        claimed_by=info.get("claimed_by"),
        category=info.get("category") or "general",
    )

    try:
        await send_panel(channel, feedback_prompt(channel.id, timeout=None))
    except Exception as e:
        print(f"[TICKETS] Could not post the feedback prompt in #{channel.name}: {e}", flush=True)
        return False

    # Whoever closed the ticket is looking straight at the prompt; only someone
    # else's close needs the DM, which is what keeps the opener reachable when
    # the channel goes away a minute later.
    if author_id != interaction.user.id:
        opener = interaction.guild.get_member(author_id) or interaction.client.get_user(author_id)
        if opener is None:
            try:
                opener = await interaction.client.fetch_user(author_id)
            except Exception:
                opener = None
        if opener is not None:
            try:
                await opener.send(view=feedback_prompt(channel.id, timeout=FEEDBACK_DM_TIMEOUT))
            except discord.Forbidden:
                print(
                    f"[TICKETS] DMs are closed for {author_id}; their feedback prompt "
                    "stays in the ticket channel.",
                    flush=True,
                )
            except Exception as e:
                print(f"[TICKETS] Could not DM the feedback prompt: {e}", flush=True)
    return True


class TicketQuestionModal(discord.ui.Modal):
    def __init__(self, category_key: str = "general"):
        self.category_key = category_key
        self.cat_info = TICKET_CATEGORIES.get(category_key, TICKET_CATEGORIES["general"])
        super().__init__(title=f"{self.cat_info['emoji']} {self.cat_info['name'][:35]}")

        self.subject = discord.ui.TextInput(
            label="Subject / Topic",
            placeholder="Brief summary of your question or issue...",
            max_length=100,
            required=True
        )
        self.add_item(self.subject)

        if category_key == "report":
            self.reported_member = discord.ui.TextInput(
                label="Reported Member / ID (Optional)",
                placeholder="@username, display name, or User ID of the violator...",
                max_length=100,
                required=False
            )
            self.add_item(self.reported_member)
        else:
            self.reported_member = None

        self.details = discord.ui.TextInput(
            label="Explain Your Problem / Questions / Report",
            style=discord.TextStyle.paragraph,
            placeholder="Please provide all details, questions, or evidence so staff can help you quickly...",
            min_length=10,
            max_length=1500,
            required=True
        )
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("❌ This can only be used in a server!", ephemeral=True)

        user_tag = interaction.user.name.lower()[:12]
        # Check if user already has an active ticket
        for ch in guild.text_channels:
            if ch.name.startswith("ticket-") and ch.topic and f"Author: {interaction.user.id}" in ch.topic:
                return await interaction.response.send_message(
                    f"❌ You already have an open ticket in {ch.mention}! Please use your existing ticket or close it first.",
                    ephemeral=True
                )

        await interaction.response.defer(ephemeral=True)

        # Setup Channel Overwrites
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                attach_files=True,
                embed_links=True,
                read_message_history=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                embed_links=True
            )
        }

        # Add Staff/Moderator permissions if roles exist
        for role_name in ("Staff", "Moderator", "Admin", "Support", "Helper"):
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    attach_files=True,
                    read_message_history=True
                )

        # Specialized role access for custom categories
        if self.category_key == "trading":
            trader_role = discord.utils.get(guild.roles, name="Non Profitable Trader") or discord.utils.get(guild.roles, name="Trader")
            if trader_role:
                overwrites[trader_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    attach_files=True,
                    read_message_history=True
                )

        # Category for Tickets
        cat = discord.utils.get(guild.categories, name="Tickets")
        if cat is None:
            try:
                cat = await guild.create_category("Tickets")
            except Exception:
                cat = None

        ch_name = f"ticket-{self.cat_info['slug']}-{user_tag}"
        channel = await guild.create_text_channel(
            name=ch_name,
            overwrites=overwrites,
            category=cat,
            topic=f"Author: {interaction.user.id} | Category: {self.category_key}"
        )

        now_ts = int(discord.utils.utcnow().timestamp())
        color_map = {
            "general": 0x5865F2,
            "report": 0xED4245,
            "technical": 0x1ABC9C,
            "trading": 0x3498DB,
            "games": 0x9B59B6,
            "economy": 0xF1C40F,
            "bug": 0xE67E22,
            "feedback": 0x2ECC71
        }
        embed = discord.Embed(
            title=f"{self.cat_info['emoji']} {self.cat_info['name']} — #{interaction.user.name}",
            description=(
                f"Welcome {interaction.user.mention}! Our team has received your ticket and will assist you shortly.\n\n"
                f"> 📌 **Subject:** `{self.subject.value.strip()}`"
            ),
            color=color_map.get(self.category_key, 0x5865F2),
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name=f"{guild.name} • Support Desk", icon_url=guild.me.display_avatar.url)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        embed.add_field(
            name="📝 Problem / Question / Report Details",
            value=f"```\n{self.details.value.strip()[:1000]}\n```",
            inline=False
        )

        if self.reported_member and self.reported_member.value.strip():
            embed.add_field(
                name="🎯 Reported Member / ID",
                value=f"`{self.reported_member.value.strip()}`",
                inline=True
            )

        embed.add_field(name="👤 Opened By", value=f"{interaction.user.mention}\n`{interaction.user.name}`", inline=True)
        embed.add_field(name="🕐 Created", value=f"<t:{now_ts}:R>", inline=True)
        embed.add_field(name="⚡ Status", value="`🟢 Open • Awaiting Staff`", inline=True)
        embed.set_footer(text=f"Author ID: {interaction.user.id} • Use buttons below to manage ticket")

        # Staff ping — always include the dev role
        staff_pings = []
        # Dev role (always tagged on every ticket)
        dev_role = guild.get_role(1548948461301534791)
        if dev_role:
            staff_pings.append(dev_role.mention)
        for role_name in ("Staff", "Moderator", "Admin", "Support"):
            r = discord.utils.get(guild.roles, name=role_name)
            if r:
                staff_pings.append(r.mention)
        if self.category_key == "trading":
            tr = discord.utils.get(guild.roles, name="Non Profitable Trader") or discord.utils.get(guild.roles, name="Trader")
            if tr and tr.mention not in staff_pings:
                staff_pings.append(tr.mention)
        ping_content = f"{interaction.user.mention} " + (" ".join(staff_pings) if staff_pings else "")

        await channel.send(
            content=ping_content,
            embed=embed,
            view=TicketControlsView()
        )

        await interaction.followup.send(
            f"✅ Your **{self.cat_info['name']}** ticket has been created: {channel.mention}",
            ephemeral=True
        )


class TicketCategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=info["name"],
                value=key,
                emoji=info["emoji"],
                description=info["desc"][:100]
            )
            for key, info in TICKET_CATEGORIES.items()
        ]
        super().__init__(
            placeholder="📂 Select a ticket category to open & explain your issue...",
            min_values=1,
            max_values=1,
            custom_id="ticket_category_select",
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        category_key = self.values[0]
        modal = TicketQuestionModal(category_key=category_key)
        await interaction.response.send_modal(modal)


class TicketOpenView(discord.ui.LayoutView):
    def __init__(self, guild: discord.Guild | None = None):
        super().__init__(timeout=None)
        name = guild.name if guild else "Support"
        cats = "\n".join(
            f"- {info['emoji']} **{info['name']}** — {info['desc']}"
            for info in TICKET_CATEGORIES.values()
        )
        heading = discord.ui.TextDisplay(
            f"## {name} Help Desk\nPrivate tickets with staff. Pick a category or open a quick general ticket."
        )
        icon = guild_icon(guild)
        parts = []
        if icon:
            parts.append(discord.ui.Section(heading, accessory=discord.ui.Thumbnail(icon)))
        else:
            parts.append(heading)
        parts.append(discord.ui.TextDisplay(cats))
        banner = guild_banner(guild)
        if banner:
            parts.append(discord.ui.MediaGallery(discord.MediaGalleryItem(banner)))
        select_row = discord.ui.ActionRow()
        select_row.add_item(TicketCategorySelect())
        btn_row = discord.ui.ActionRow()
        quick = discord.ui.Button(
            label="Quick Ticket",
            style=discord.ButtonStyle.primary,
            custom_id="ticket_open_btn",
            emoji="📩",
        )
        quick.callback = self.open_ticket
        btn_row.add_item(quick)
        parts.extend([select_row, btn_row, discord.ui.TextDisplay("-# Only you and staff can see your ticket")])
        self.add_item(discord.ui.Container(*parts, accent_color=ACCENT))

    async def open_ticket(self, interaction: discord.Interaction):
        modal = TicketQuestionModal(category_key="general")
        await interaction.response.send_modal(modal)


class TicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close_btn", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check permissions
        is_staff = interaction.user.guild_permissions.manage_channels
        ch_topic = interaction.channel.topic or ""
        is_creator = f"Author: {interaction.user.id}" in ch_topic

        if not (is_staff or is_creator):
            return await interaction.response.send_message("❌ Only staff or the ticket creator can close this ticket.", ephemeral=True)

        info = ticket_topic_info(interaction.channel)

        view = Panel(
            title="Closing ticket",
            body=(
                f"Marked resolved by {interaction.user.mention}.\n"
                "Feedback has been requested — this channel deletes as soon as the "
                f"rating is in, or **{FEEDBACK_GRACE_SECONDS} seconds** from now."
            ),
            accent=0xED4245,
            thumbnail=avatar_url(interaction.user),
            footer="Thanks for reaching out",
            timeout=None,
        )
        await send_panel(interaction, view)

        if not await _ask_for_feedback(interaction, info):
            # No opener recorded (a hand-made channel, or a staff test post), so
            # there is nobody to ask — close it the way this button always has.
            await discord.utils.sleep_until(discord.utils.utcnow() + datetime.timedelta(seconds=5))
            await _close_ticket_channel(interaction.channel)
            return

        event = asyncio.Event()
        _FEEDBACK_EVENTS[interaction.channel.id] = event
        _watch_feedback(interaction.client, interaction.channel.id, event)

    @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.success, custom_id="ticket_claim_btn", emoji="✋")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_staff = interaction.user.guild_permissions.manage_channels or any(
            r.name in ("Staff", "Moderator", "Admin", "Support") for r in interaction.user.roles
        )
        if not is_staff:
            return await interaction.response.send_message("❌ Only staff members can claim tickets!", ephemeral=True)

        button.disabled = True
        button.label = f"Claimed by {interaction.user.display_name[:12]}"
        button.style = discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

        # The topic is the only place the claim survives a restart, and the close
        # button reads it back to credit the right person in the feedback log.
        topic = interaction.channel.topic or ""
        if f"Claimed: {interaction.user.id}" not in topic:
            try:
                await interaction.channel.edit(topic=f"{topic} | Claimed: {interaction.user.id}")
            except Exception as e:
                print(
                    f"[TICKETS] Could not record the claimer of #{interaction.channel.name}: {e}",
                    flush=True,
                )

        view = Panel(
            title="Ticket claimed",
            body=f"{interaction.user.mention} is handling this request.",
            accent=0xF1C40F,
            thumbnail=avatar_url(interaction.user),
            timeout=None,
        )
        await send_panel(interaction, view)

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary, custom_id="ticket_transcript_btn", emoji="📜")
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        lines = [
            f"=== TICKET TRANSCRIPT FOR #{interaction.channel.name} ===",
            f"Server: {interaction.guild.name} ({interaction.guild.id})",
            f"Generated by: {interaction.user.name} ({interaction.user.id})",
            f"Date: {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "=" * 50,
            ""
        ]
        async for msg in interaction.channel.history(limit=250, oldest_first=True):
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author = f"{msg.author.name}#{msg.author.discriminator}" if msg.author.discriminator != "0" else msg.author.name
            lines.append(f"[{timestamp}] {author}: {msg.clean_content}")
            if msg.attachments:
                for att in msg.attachments:
                    lines.append(f"    [Attachment: {att.url}]")

        content = "\n".join(lines)
        file = discord.File(io.BytesIO(content.encode("utf-8")), filename=f"transcript-{interaction.channel.name}.txt")
        await interaction.followup.send("📄 Here is the transcript for this ticket:", file=file, ephemeral=True)


class Tickets(commands.Cog):
    """Category-based ticket system with problem description modals, claiming, and transcripts."""

    ticket_group = app_commands.Group(name="ticket", description="Support ticket system commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(TicketOpenView())
        bot.add_view(TicketControlsView())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "tickets"):
                await interaction.response.send_message(
                    "⚠️ The **Support Tickets** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True
                )
                return False
        return True

    @ticket_group.command(name="setup", description="Post the modern category-based ticket creation panel in this channel.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def ticket_setup(self, interaction: discord.Interaction):
        await send_panel(interaction.channel, TicketOpenView(interaction.guild))
        await interaction.response.send_message("✅ Support ticket panel posted.", ephemeral=True)

    @ticket_group.command(name="add", description="Add a user to the current ticket.")
    @app_commands.describe(member="The member to add")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def ticket_add(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.channel.name.startswith("ticket-"):
            return await interaction.response.send_message("❌ This command can only be used inside a ticket channel.", ephemeral=True)
        await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, attach_files=True)
        
        embed = discord.Embed(
            description=f"✅ {member.mention} has been added to the ticket by {interaction.user.mention}.",
            color=0x57F287
        )
        await send_card(interaction, embed=embed)

    @ticket_group.command(name="remove", description="Remove a user from the current ticket.")
    @app_commands.describe(member="The member to remove")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def ticket_remove(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.channel.name.startswith("ticket-"):
            return await interaction.response.send_message("❌ This command can only be used inside a ticket channel.", ephemeral=True)
        await interaction.channel.set_permissions(member, overwrite=None)
        
        embed = discord.Embed(
            description=f"🚪 {member.mention} has been removed from the ticket by {interaction.user.mention}.",
            color=0xED4245
        )
        await send_card(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))

