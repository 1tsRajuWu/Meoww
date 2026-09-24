"""Managing what a server is made of: its channels and its roles.

The first half of the management tier, and it is deliberately the *shape* half —
channels and roles are the most destructive two things a bot can be asked to
change, so they are where the guardrails get built and proved before anything
easier is added on top of them.

Three rules, and each of them is a place this kind of command usually goes wrong:

* **The size of an action is checked before the door is opened.** A one-channel
  delete runs; deleting a category's worth of channels, or taking a role off
  everyone who has it, needs the confirmation in ``utils/confirmations`` — from
  the person who typed the command, by a press, and with the number said out
  loud. Nothing here fires a mass change off one option.
* **Every write goes through ``utils/structure``.** Not for tidiness: that module
  opens the case and writes the server's moderation log, so a command calling
  ``channel.delete()`` directly would be a state change with no record — which is
  the one thing the brief rules out.
* **A refusal reaches the log too.** A role above the bot's own, a channel it
  cannot manage, a name Discord will not take: each is answered in words *and*
  written down, so the log is a record of what was asked for rather than only of
  what succeeded.

The group is one of Discord's hundred command slots, which is why the four verbs
are subcommands of one group rather than four top-level commands.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils import confirmations, structure
from utils.moderation_actions import Actor
from utils.style import ACCENT, DANGER, SUCCESS


def _actor(interaction: discord.Interaction) -> Actor:
    """Who is asking. The member object is carried because the guard needs it."""
    return Actor(
        id=int(getattr(interaction.user, "id", 0) or 0),
        name=str(getattr(interaction.user, "display_name", "") or getattr(interaction.user, "name", "")),
        member=interaction.user,
        source="discord",
    )


def _reason(interaction: discord.Interaction, reason: str | None) -> str:
    """A reason Discord will take and a person would recognise.

    Discord refuses an audit-log reason over 512 characters, and a *refused*
    audit entry is worse than a terse one: the action still happens and the
    reason is lost.
    """
    text = (reason or "").strip()
    if not text:
        return f"asked by {getattr(interaction.user, 'name', 'somebody')}"
    return text[:480]


async def _reply(interaction: discord.Interaction, embed: discord.Embed) -> None:
    """Answer, whether or not the confirmation prompt already acknowledged this.

    The confirmation step is why this exists: it sends an ephemeral prompt when it
    needs one and sends *nothing* when the ceiling already covers the action, so
    the same call site has to work in both states. ``response.is_done()`` is
    Discord's own answer to which state we are in — guessing from the action name
    is how the one-channel delete ends up with a "this interaction failed" the
    first time somebody tries it.
    """
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


def _card(title: str, result: structure.Result, body: str = "") -> discord.Embed:
    """One shape for every answer this group gives."""
    embed = discord.Embed(
        title=title,
        description=body or result.detail or "",
        color=SUCCESS if result.ok else DANGER,
    )
    if result.names:
        shown = ", ".join(f"`{name}`" for name in result.names[:12])
        more = f" and {len(result.names) - 12} more" if len(result.names) > 12 else ""
        embed.add_field(name="What changed", value=(shown + more)[:1024], inline=False)
    if result.case_id:
        embed.set_footer(text=f"Case #{result.case_id} — written to this server's moderation log")
    elif not result.ok:
        embed.set_footer(text="Nothing was changed")
    return embed


class Structure(commands.Cog):
    """Channels and roles, with a ceiling and a record on every write."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Not named for the command: discord.py refuses a cog attribute starting with
    # ``bot_``. The command it registers is ``/structure``.
    structure_group = app_commands.Group(
        name="structure",
        description="Build and clear away this server's channels and roles.",
    )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The feature switch, read per server.

        Checked here rather than in each callback because a group is one feature:
        a server that has switched the structure commands off should see none of
        them, and the same gate is what keeps them out of its picker
        (``utils/command_scope``).
        """
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "structure"):
                await interaction.response.send_message(
                    "⚠️ The **Server Structure** system is currently **turned off** on "
                    "this server.\nAn administrator can turn it on in the "
                    "[Web Dashboard](https://meow-bot.pages.dev/dashboard).",
                    ephemeral=True,
                )
                return False
        return True

    # ── channels ─────────────────────────────────────────────────────────── #

    @structure_group.command(name="channel-create", description="Make a text channel (Manage Channels).")
    @app_commands.describe(
        name="What to call it — Discord renders spaces as dashes.",
        category="Put it under this category.",
        private="Only you and I can see it.",
        reason="Why — written to Discord's own audit log and to your moderation log.",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def channel_create(
        self,
        interaction: discord.Interaction,
        name: str,
        category: discord.CategoryChannel | None = None,
        private: bool = False,
        reason: str | None = None,
    ):
        result = await structure.create_channel(
            interaction.guild, _actor(interaction), name,
            category=category, private=private, reason=_reason(interaction, reason),
        )
        await _reply(
            interaction,
            _card(
                "🏗️ Channel created" if result.ok else "❌ Not created",
                result,
                body=(
                    f"Created **{result.names[0]}**"
                    + (f" under **{category.name}**" if category else "")
                    + (" (private)" if private else "")
                    if result.ok else result.detail
                ),
            ),
        )

    @structure_group.command(
        name="channel-delete",
        description="Delete a channel and everything in it (Manage Channels).",
    )
    @app_commands.describe(
        channel="The channel to delete.",
        reason="Why — written to Discord's own audit log and to your moderation log.",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def channel_delete(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.VoiceChannel,
        reason: str | None = None,
    ):
        """One channel at a time, and that is the whole guard.

        No confirmation for a single channel: a prompt here would be answered
        without reading, and the prompt that matters — the one for a mass
        deletion — is worth exactly as much as the reflexes it has not trained.
        """
        if not await confirmations.guard(
            interaction, action="channel_delete", count=1, subject="channel",
        ):
            return
        result = await structure.delete_channels(
            interaction.guild, _actor(interaction), [channel],
            reason=_reason(interaction, reason),
        )
        await _reply(
            interaction, _card("🗑️ Channel deleted" if result.ok else "❌ Not deleted", result)
        )

    @structure_group.command(
        name="channel-archive",
        description="Delete every channel in a category (Manage Channels, confirmed).",
    )
    @app_commands.describe(
        category="The category whose channels should go.",
        keep="A channel in that category to leave alone — usually the one you are typing in.",
        reason="Why — written to Discord's own audit log and to your moderation log.",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def channel_archive(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        keep: discord.TextChannel | discord.VoiceChannel | None = None,
        reason: str | None = None,
    ):
        """The mass deletion, which is why the confirmation exists.

        The ceiling is read from this server's own setting, so a server with three
        channels in a category is asked once where a server with forty is asked
        the same way — what changes is how many may go without being asked at all.
        """
        channels = [
            channel for channel in list(getattr(category, "channels", []) or [])
            if channel is not keep
        ]
        if not channels:
            return await _reply(
                interaction,
                _card(
                    "❌ Nothing to archive",
                    structure.Result(
                        action="channel_archive", ok=False,
                        detail=f"**{category.name}** has no channels I could take away.",
                    ),
                ),
            )
        if not await confirmations.guard(
            interaction, action="channel_archive", count=len(channels),
            subject=f"channels in {category.name}",
        ):
            return
        result = await structure.delete_channels(
            interaction.guild, _actor(interaction), channels,
            reason=_reason(interaction, reason),
        )
        if result.ok:
            result.detail = f"{result.count} channel(s) deleted from **{category.name}**."
        await _reply(
            interaction,
            _card("🗑️ Category cleared" if result.ok else "❌ Nothing deleted", result),
        )

    # ── roles ────────────────────────────────────────────────────────────── #

    @structure_group.command(name="role-create", description="Make a role (Manage Roles).")
    @app_commands.describe(
        name="What to call it.",
        colour="Hex colour, like #2dd4bf — leave it blank for the default grey.",
        reason="Why — written to Discord's own audit log and to your moderation log.",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def role_create(
        self,
        interaction: discord.Interaction,
        name: str,
        colour: str | None = None,
        reason: str | None = None,
    ):
        value = None
        if colour:
            try:
                value = int(str(colour).strip().lstrip("#"), 16)
            except ValueError:
                return await _reply(
                    interaction,
                    _card(
                        "❌ Not created",
                        structure.Result(
                            action="role_create", ok=False,
                            detail=f"`{colour}` is not a hex colour — try `#2dd4bf`.",
                        ),
                    ),
                )
        result = await structure.create_role(
            interaction.guild, _actor(interaction), name,
            colour=value, reason=_reason(interaction, reason),
        )
        await _reply(
            interaction, _card("🏷️ Role created" if result.ok else "❌ Not created", result)
        )

    @structure_group.command(name="role-delete", description="Delete a role (Manage Roles).")
    @app_commands.describe(
        role="The role to delete.",
        reason="Why — written to Discord's own audit log and to your moderation log.",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def role_delete(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        reason: str | None = None,
    ):
        problem = structure.deletable_role(interaction.guild, role)
        if problem:
            # Answered before the door, so the *refusal* is the card — and the
            # door is not asked to log a thing that never reached Discord.
            return await _reply(
                interaction,
                _card(
                    "❌ Not deleted",
                    structure.Result(action="role_delete", ok=False, detail=problem),
                ),
            )
        result = await structure.delete_role(
            interaction.guild, _actor(interaction), role, reason=_reason(interaction, reason),
        )
        await _reply(
            interaction, _card("🗑️ Role deleted" if result.ok else "❌ Not deleted", result)
        )

    @structure_group.command(
        name="role-purge",
        description="Take a role off everyone who has it, without deleting it (Manage Roles, confirmed).",
    )
    @app_commands.describe(
        role="The role to take away.",
        reason="Why — written to Discord's own audit log and to your moderation log.",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def role_purge(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        reason: str | None = None,
    ):
        """The action whose blast radius is invisible in its own command line.

        ``/structure role-purge role:@Muted`` reads like one object and touches
        every member who has it, which is exactly the shape a confirmation is for:
        the count is members, not roles, and it is said out loud before anything
        moves.
        """
        problem = structure.deletable_role(interaction.guild, role)
        if problem:
            return await _reply(
                interaction,
                _card(
                    "❌ Nothing changed",
                    structure.Result(action="role_purge", ok=False, detail=problem),
                ),
            )
        members = list(getattr(role, "members", []) or [])
        if not await confirmations.guard(
            interaction, action="role_purge", count=len(members),
            subject=structure.subject_for("role_purge", members),
        ):
            return
        result = await structure.purge_role(
            interaction.guild, _actor(interaction), role, reason=_reason(interaction, reason),
        )
        await _reply(
            interaction,
            _card("🧨 Role taken away" if result.ok else "❌ Nothing changed", result),
        )

    @structure_group.command(
        name="limits",
        description="What this server allows without a confirmation, per action (Administrator).",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def limits(self, interaction: discord.Interaction):
        """The ceilings, read from the reader the guard uses.

        One command rather than a dashboard panel for now, because the number has
        to be visible to the person who can change it and the *only* honest way to
        show it is to ask ``utils.confirmations`` — a second copy of the defaults
        in a settings page is the drift this project keeps paying for.
        """
        lines = [
            f"• `{action}` — **{confirmations.ceiling_for(interaction.guild_id, action)}** "
            f"before a confirmation is asked"
            for action in sorted(confirmations.CEILINGS)
        ]
        await _reply(
            interaction,
            discord.Embed(
                title="🧱 What needs confirming here",
                description=(
                    "Any action over these numbers stops and asks **you** to confirm it "
                    "— by a press, from whoever typed the command, with the count said "
                    "out loud.\n\n" + "\n".join(lines)
                ),
                color=ACCENT,
            ).set_footer(
                text="Per server, and higher than one channel is not a default anybody chose."
            ),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Structure(bot))
