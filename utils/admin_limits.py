"""Admin limits and approval system for XP and money commands.

Admins can give up to a certain amount without approval.
Amounts over the limit require owner approval via interactive buttons.
"""
from __future__ import annotations

import discord

from utils import config_audit

# ---------------------------------------------------------------------------
# Limits (configurable via environment variables)
# ---------------------------------------------------------------------------

#: Max XP an admin can give without owner approval
ADMIN_GIVEXP_LIMIT: int = int(config_audit.get("ADMIN_GIVEXP_LIMIT", "10000"))

#: Max money an admin can add without owner approval
ADMIN_ADDMONEY_LIMIT: int = int(config_audit.get("ADMIN_ADDMONEY_LIMIT", "50000"))

#: Max money an admin can set without owner approval
ADMIN_SETMONEY_LIMIT: int = int(config_audit.get("ADMIN_SETMONEY_LIMIT", "100000"))

#: Max money an admin can add to bank without owner approval
ADMIN_ADDBANK_LIMIT: int = int(config_audit.get("ADMIN_ADDBANK_LIMIT", "50000"))

#: The bot owner's user ID (for approval notifications)
OWNER_USER_ID: int = int(config_audit.get("OWNER_USER_ID", "0"))
DEFAULT_OWNER_IDS = {1526964236708745318, 1348622207999279146, 1549177590328205393}


def is_owner_user(user_id: int | None) -> bool:
    if not user_id:
        return False
    if OWNER_USER_ID and user_id == OWNER_USER_ID:
        return True
    return user_id in DEFAULT_OWNER_IDS



def needs_approval(amount: int, limit: int) -> bool:
    """Check if an amount exceeds the admin limit and needs owner approval."""
    return amount > limit


# ---------------------------------------------------------------------------
# Approval View
# ---------------------------------------------------------------------------
class ApprovalView(discord.ui.View):
    """Interactive approve/deny buttons for admin actions over the limit."""

    def __init__(
        self,
        requester: discord.Member,
        target: discord.Member,
        action_type: str,
        amount: int,
        guild: discord.Guild,
    ):
        super().__init__(timeout=300)  # 5 minutes to decide
        self.requester = requester
        self.target = target
        self.action_type = action_type
        self.amount = amount
        self.guild = guild
        self.approved = None
        self.message: discord.Message | None = None

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner_user(interaction.user.id):
            return await interaction.response.send_message(
                "❌ Only the bot owner can approve this.", ephemeral=True
            )

        self.approved = True
        self.stop()

        embed = discord.Embed(
            title="✅ Approved",
            description=(
                f"**{self.requester.display_name}** was approved to give "
                f"**{self.amount:,}** {self.action_type} to **{self.target.display_name}**."
            ),
            color=0x57F287,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="❌ Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner_user(interaction.user.id):
            return await interaction.response.send_message(
                "❌ Only the bot owner can deny this.", ephemeral=True
            )

        self.approved = False
        self.stop()

        embed = discord.Embed(
            title="❌ Denied",
            description=(
                f"**{self.requester.display_name}**'s request to give "
                f"**{self.amount:,}** {self.action_type} to **{self.target.display_name}** was denied."
            ),
            color=0xED4245,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        """Auto-deny if no response within 5 minutes."""
        if self.approved is None:
            self.approved = False
            if self.message:
                try:
                    embed = discord.Embed(
                        title="⏰ Timed Out",
                        description="Approval request timed out — automatically denied.",
                        color=0x95A5A6,
                    )
                    await self.message.edit(embed=embed, view=None)
                except Exception:
                    pass


async def request_approval(
    interaction: discord.Interaction,
    target: discord.Member,
    action_type: str,
    amount: int,
    execute_fn,
    *,
    limit: int = 0,
) -> bool:
    """Request owner approval for an admin action over the limit.

    Args:
        interaction: The original command interaction
        target: The member receiving the XP/money
        action_type: Description of what's being given (e.g., "🪙 XP", "💰 coins")
        amount: The amount being given
        execute_fn: Async function to call if approved
        limit: The relevant limit value to display in messages

    Returns:
        True if approved and executed, False otherwise
    """
    display_limit = limit or ADMIN_GIVEXP_LIMIT
    owner = None
    if OWNER_USER_ID and interaction.guild:
        owner = interaction.guild.get_member(OWNER_USER_ID)
    if owner is None and interaction.guild:
        for oid in DEFAULT_OWNER_IDS:
            m = interaction.guild.get_member(oid)
            if m is not None:
                owner = m
                break

    if owner is None and not OWNER_USER_ID and not DEFAULT_OWNER_IDS:
        # No owner configured — deny by default
        await interaction.response.send_message(
            f"❌ Amount exceeds the limit (**{display_limit:,}** {action_type}). "
            "The bot owner has not configured `OWNER_USER_ID` for approvals.",
            ephemeral=True,
        )
        return False

    if owner is None:
        # Owner not in this server
        await interaction.response.send_message(
            f"❌ Amount exceeds the limit (**{display_limit:,}** {action_type}). "
            "The bot owner is not in this server.",
            ephemeral=True,
        )
        return False

    # Create the approval view
    view = ApprovalView(
        requester=interaction.user,
        target=target,
        action_type=action_type,
        amount=amount,
        guild=interaction.guild,
    )

    embed = discord.Embed(
        title="🔔 Approval Required",
        description=(
            f"**{interaction.user.display_name}** wants to give "
            f"**{amount:,}** {action_type} to **{target.display_name}**.\n\n"
            f"This exceeds the admin limit of **{display_limit:,}** {action_type}.\n"
            f"**{owner.mention}**, please approve or deny this action."
        ),
        color=0xF1C40F,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="⏰ Expires in 5 minutes")

    await interaction.response.send_message(
        content=owner.mention,
        embed=embed,
        view=view,
    )
    # Store message reference for timeout handler
    view.message = await interaction.original_response()

    # Wait for owner to decide
    await view.wait()

    if view.approved:
        # Execute the action
        try:
            await execute_fn()
            return True
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Approved but execution failed: {exc}",
                ephemeral=True,
            )
            return False
    else:
        if view.approved is None:
            # Timed out
            await interaction.followup.send(
                "⏰ Approval request timed out — action cancelled.",
                ephemeral=True,
            )
        else:
            # Denied
            await interaction.followup.send(
                "❌ Action denied by the bot owner.",
                ephemeral=True,
            )
        return False
