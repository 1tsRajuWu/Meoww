import discord
from discord import app_commands
from discord.ext import commands
import datetime
import io
from utils.style import Panel, send_panel, ACCENT, guild_icon, guild_banner, avatar_url

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

        # Staff ping
        staff_pings = []
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

        view = Panel(
            title="Closing ticket",
            body=f"Marked resolved by {interaction.user.mention}. This channel deletes in **5 seconds**.",
            accent=0xED4245,
            thumbnail=avatar_url(interaction.user),
            footer="Thanks for reaching out",
            timeout=None,
        )
        await send_panel(interaction, view)
        await discord.utils.sleep_until(discord.utils.utcnow() + datetime.timedelta(seconds=5))
        try:
            await interaction.channel.delete()
        except Exception:
            pass

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
        await interaction.response.send_message(embed=embed)

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
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))

