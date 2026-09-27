"""A reaction is a role: members take it off and put it back on themselves.

Every other way of handing out a role needs somebody — a moderator typing
`/addrole`, a member asking them to, a message that says "ask a staff member".
Reaction roles are the one shape where nothing happens at all: the emoji is the
whole interface, it works at three in the morning, and nobody has to read a rule.

**The bot reacts for you.** `/reactionrole add` puts the emoji on the message
itself, because a mapping nobody can see is a mapping nobody uses: the
difference between this and a spreadsheet of role ids is the reaction the moment
it is configured.

The one thing this feature can get wrong silently is *permission*: Discord
refuses `add_roles` for a role at or above the bot's own, and a listener that
swallows that refusal is a feature that reports success forever while nothing
happens. So the refusal is written into the log, and the command refuses up
front — the same rule the default role's checklist row states for the same
reason. Mappings are managed from this server's dashboard as well, through the
same rows (`utils/db`), so the page and the command cannot disagree.
"""
import re

import discord
from discord import app_commands
from discord.ext import commands

from utils import db, features, permissions
from utils.style import INFO

#: The switch this cog answers to, named once — the command gate and both
#: listeners read it, and a second copy is how they come to disagree.
MODULE = "reaction_roles"

#: What the dashboard calls a mapping in a sentence, and what a link looks like.
LINK_BASE = "https://discord.com/channels"

#: Message links carry the three snowflakes in order; a pasted link is the only
#: way a person can name a message without hunting for an id.
LINK_REGEX = re.compile(r"channels/(\d+)/(\d+)/(\d+)")


def message_link(guild_id, channel_id, message_id) -> str:
    """The address of a mapped message, so a reader can go and look at it."""
    return f"{LINK_BASE}/{int(guild_id)}/{int(channel_id or 0)}/{int(message_id)}"


def _off_sentence() -> str:
    """The refusal, labelled from the registry rather than by hand."""
    feature = features.get(MODULE)
    label = feature.name if feature else MODULE.replace("_", " ").title()
    return (
        f"⚠️ The **{label}** system is currently **turned off** on this server.\n"
        "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard/systems)."
    )


class ReactionRoles(commands.Cog):
    """React to a message to get a role — and un-react to give it back."""

    group = app_commands.Group(
        name="reactionrole", description="Emoji-on-a-message roles"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _running(self, guild_id) -> bool:
        check = getattr(self.bot, "is_module_enabled", None)
        return True if check is None else bool(check(guild_id, MODULE))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and not self._running(interaction.guild_id):
            await interaction.response.send_message(_off_sentence(), ephemeral=True)
            return False
        return True

    def _may_assign(self, guild, role) -> bool:
        """Whether Discord will let the bot hand this role out.

        The check the feature fails silently without: a role at or above the
        bot's own is refused by Discord, and the listener's bare ``except`` would
        turn that into "it works, trust me".
        """
        me = getattr(guild, "me", None)
        top = getattr(getattr(me, "top_role", None), "position", None)
        position = getattr(role, "position", None)
        if top is None or position is None:
            return True  # cannot read the hierarchy: let Discord answer
        return int(top) > int(position)

    async def _message_from_link(self, guild, text):
        """The message a pasted link names, as ``(message, reason)``.

        A link rather than a `discord.Message` parameter, and for the same
        reason the dashboard's own form takes one: the command supports the
        message being in *another* channel, and a link is the one thing a person
        can copy from it without knowing what a snowflake is.
        """
        found = LINK_REGEX.search(str(text or ""))
        if not found:
            return None, (
                "That is not a message link — right-click the message, *Copy "
                "Message Link*, and paste it here."
            )
        link_guild, channel_id, message_id = (int(part) for part in found.groups())
        if link_guild != int(guild.id):
            return None, "That link is for another server — copy a link from this one."
        channel = guild.get_channel(channel_id)
        if channel is None:
            return None, "I cannot see the channel that link is in."
        try:
            return await channel.fetch_message(message_id), ""
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None, "I could not read that message — check the link, and that I can see that channel."

    @group.command(name="add", description="Link an emoji on a message to a role.")
    @app_commands.describe(
        message="The message to watch — right-click it, Copy Message Link, paste here",
        emoji="The emoji members react with",
        role="The role that reaction hands out",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def add(self, interaction: discord.Interaction, message: str, emoji: str, role: discord.Role):
        is_default = getattr(role, "is_default", None)
        if callable(is_default) and is_default():
            return await interaction.response.send_message(
                "That is @everyone — it cannot be handed out or taken away.",
                ephemeral=True,
            )
        if not self._may_assign(interaction.guild, role):
            return await interaction.response.send_message(
                f"**{role.name}** sits at or above my own role, so Discord refuses "
                "the assignment — the bot would report success while nothing "
                "changed on the member. Drag my role above it in Server Settings → "
                "Roles, or pick a lower role.",
                ephemeral=True,
            )
        # The same escalation guard `/addrole` uses. It matters *more* here than
        # there: a mapping nobody has to press is a role handed out by an emoji,
        # so a moderator who may not give themselves a role must not be able to
        # wire one up that gives it to them — or to everybody.
        blocked = permissions.role_grant_problem(
            role, interaction.user, interaction.guild
        )
        if blocked:
            return await interaction.response.send_message(blocked, ephemeral=True)
        target, reason = await self._message_from_link(interaction.guild, message)
        if target is None:
            return await interaction.response.send_message(reason, ephemeral=True)
        try:
            await target.add_reaction(emoji)
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message(
                f"I could not react with {emoji} on that message — check that the "
                "emoji exists and that I may add reactions in that channel.",
                ephemeral=True,
            )
        await db.add_reaction_role(
            interaction.guild.id, target.id, target.channel.id, str(emoji), role.id
        )
        await interaction.response.send_message(
            f"Reacting with {emoji} on that message now gives **{role.name}**, and "
            f"removing the reaction takes it back.\n{message_link(interaction.guild.id, target.channel.id, target.id)}",
            ephemeral=True,
        )

    @group.command(name="remove", description="Stop a mapping — the reaction stays, the role stops changing.")
    @app_commands.describe(message_id="The id of the mapped message", emoji="The emoji that mapped it")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def remove(self, interaction: discord.Interaction, message_id: str, emoji: str):
        try:
            number = int(str(message_id).strip())
        except (TypeError, ValueError):
            return await interaction.response.send_message(
                "That is not a message id — paste the message's link and copy the "
                "last number, or use `/reactionrole list`.",
                ephemeral=True,
            )
        removed = await db.remove_reaction_role(number, str(emoji))
        if not removed:
            return await interaction.response.send_message(
                f"Nothing was mapped to {emoji} on that message — `/reactionrole list` "
                "shows what is.",
                ephemeral=True,
            )
        await interaction.response.send_message(
            f"{emoji} no longer changes anybody's roles on that message. The "
            "reactions already on it stay where they are, and the roles members "
            "already have are untouched.",
            ephemeral=True,
        )

    @group.command(name="list", description="Every reaction role this server has.")
    async def listing(self, interaction: discord.Interaction):
        rows = await db.list_reaction_roles(interaction.guild.id)
        if not rows:
            return await interaction.response.send_message(
                "No reaction roles here yet. `/reactionrole add` links an emoji on "
                "a message to a role, and this server's dashboard manages the same "
                "list on its Server page.",
                ephemeral=True,
            )
        embed = discord.Embed(
            title="🎭 Reaction Roles — this server",
            description="Removing a reaction takes the role back. `/appeal` is for "
                        "moderation actions; nothing here needs one.",
            color=INFO,
        )
        lines = []
        for row in rows[:25]:
            role = interaction.guild.get_role(row["role_id"])
            name = role.name if role is not None else f"role {row['role_id']} (gone)"
            lines.append(
                f"{row['emoji']} → **{name}** · [message]"
                f"({message_link(interaction.guild.id, row['channel_id'], row['message_id'])})"
            )
        embed.add_field(name=f"{len(rows)} mapping{'s' if len(rows) != 1 else ''}", value="\n".join(lines), inline=False)
        if len(rows) > 25:
            embed.set_footer(text=f"…and {len(rows) - 25} more — the dashboard lists them all.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _mapped(self, payload):
        """The role a reaction should change, or ``None`` for "nothing to do".

        Every early exit here is deliberate: a bot's own reaction, a server that
        switched the system off, an emoji that maps nothing, a mapping whose
        guild is not the one the reaction landed in (a database restored from a
        backup can hold one), or a role that was deleted since.
        """
        if getattr(payload, "guild_id", None) is None:
            return None
        if not self._running(payload.guild_id):
            return None
        row = await db.reaction_role_for(payload.message_id, str(payload.emoji))
        if not row:
            return None
        role_id, guild_id = row
        if guild_id is not None and int(guild_id) != int(payload.guild_id):
            return None
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return None
        role = guild.get_role(int(role_id))
        if role is None:
            return None
        return guild, role

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        member = getattr(payload, "member", None)
        if member is not None and getattr(member, "bot", False):
            return
        mapped = await self._mapped(payload)
        if mapped is None:
            return
        guild, role = mapped
        target = member or guild.get_member(payload.user_id)
        if target is None:
            return
        try:
            await target.add_roles(role, reason="Reaction role")
        except (discord.Forbidden, discord.HTTPException) as exc:
            # Never silent: Discord refusing this is the failure a member
            # experiences as "the reaction did nothing", and the only other place
            # it can be seen is nowhere.
            print(
                f"[REACTION-ROLES] ⚠️ could not give {role.name} to "
                f"{getattr(target, 'id', '?')} in guild {guild.id}: {exc}",
                flush=True,
            )

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        mapped = await self._mapped(payload)
        if mapped is None:
            return
        guild, role = mapped
        target = guild.get_member(payload.user_id)
        if target is None:
            fetch = getattr(guild, "fetch_member", None)
            if not callable(fetch):
                return
            try:
                target = await fetch(payload.user_id)
            except Exception:  # noqa: BLE001 — they left between reaction and removal
                return
        try:
            await target.remove_roles(role, reason="Reaction role removed")
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(
                f"[REACTION-ROLES] ⚠️ could not take {role.name} back from "
                f"{getattr(target, 'id', '?')} in guild {guild.id}: {exc}",
                flush=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
