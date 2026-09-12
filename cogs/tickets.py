import discord
from discord import app_commands
from discord.ext import commands
import datetime

class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.primary, custom_id="ticket_open_btn", emoji="📩")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        existing_channel = discord.utils.get(guild.text_channels, name=f"ticket-{interaction.user.name.lower()}")
        if existing_channel:
            return await interaction.response.send_message(
                f"❌ You already have an open ticket in {existing_channel.mention}!", ephemeral=True
            )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }

        staff_role = discord.utils.get(guild.roles, name="Staff") or discord.utils.get(guild.roles, name="Moderator") or discord.utils.get(guild.roles, name="Admin")
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True)

        category = discord.utils.get(guild.categories, name="Tickets")
        if category is None:
            category = await guild.create_category("Tickets")

        channel = await guild.create_text_channel(
            f"ticket-{interaction.user.name.lower()}", overwrites=overwrites, category=category
        )

        now = int(discord.utils.utcnow().timestamp())
        embed = discord.Embed(
            title=f"📩 Support Ticket — #{interaction.user.name}",
            description=(
                f"Welcome {interaction.user.mention}! Our team will be with you shortly.\n\n"
                f"> 📝 **Describe your issue:** Please explain your question or concern in detail below.\n"
                f"> 📎 **Attachments:** Feel free to share any relevant screenshots or links."
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name="🐾 Meow Support Desk", icon_url=guild.me.display_avatar.url)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Opened By", value=f"{interaction.user.mention}\n`{interaction.user.name}`", inline=True)
        embed.add_field(name="🕐 Opened At", value=f"<t:{now}:R>", inline=True)
        embed.set_footer(text=f"User ID: {interaction.user.id} • Click Close Ticket below when resolved")

        await channel.send(content=f"{interaction.user.mention}", embed=embed, view=TicketCloseView())
        await interaction.response.send_message(f"✅ Your ticket has been created: {channel.mention}", ephemeral=True)


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close_btn", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (interaction.user.guild_permissions.manage_channels or interaction.channel.name.endswith(interaction.user.name.lower())):
            return await interaction.response.send_message("❌ Only staff or the ticket creator can close this ticket.", ephemeral=True)

        embed = discord.Embed(
            title="🔒 Closing Ticket",
            description="This ticket has been marked as resolved and will delete in **5 seconds**.\nThank you for contacting support!",
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Closed by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)
        await discord.utils.sleep_until(discord.utils.utcnow() + datetime.timedelta(seconds=5))
        await interaction.channel.delete()


class Tickets(commands.Cog):
    """Button-based ticket system and management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(TicketOpenView())
        bot.add_view(TicketCloseView())

    @app_commands.command(name="ticket-setup", description="Post the ticket-creation panel in this channel.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def ticket_setup(self, interaction: discord.Interaction):
        guild = interaction.guild
        embed = discord.Embed(
            title="📩 Support & Help Desk",
            description=(
                "Need help, want to ask a question, or have an issue to report?\n"
                "Click the button below to create a **private support ticket**.\n\n"
                "> ⏱️ **Fast Assistance:** Our staff will respond as soon as possible\n"
                "> 🔒 **Private:** Only you and server staff can view the ticket\n"
                "> 🐾 **Respectful:** Please do not open troll or empty tickets"
            ),
            color=0x2B2D31,
            timestamp=discord.utils.utcnow()
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_author(name=f"{guild.name} • Support", icon_url=guild.icon.url if guild.icon else guild.me.display_avatar.url)
        embed.set_footer(text="🐾 Meow Tickets • Click below to open a ticket", icon_url=guild.me.display_avatar.url)

        await interaction.channel.send(embed=embed, view=TicketOpenView())
        await interaction.response.send_message("✅ Support ticket panel successfully created!", ephemeral=True)

    @app_commands.command(name="ticket-add", description="Add a user to the current ticket.")
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

    @app_commands.command(name="ticket-remove", description="Remove a user from the current ticket.")
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

