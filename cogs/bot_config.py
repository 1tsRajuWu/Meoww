"""The bot's own commands: its profile, its command tree, its database, deploys.

These used to be four top-level commands, which cost four of Discord's hundred
command slots. This bot is at exactly a hundred, so the next command anyone wants
would have been refused outright — the same wall that once stopped this cog from
loading at all. A subcommand nests inside its parent and costs none of that
budget, so the commands that address the *bot* rather than any server's members
now live in one group, and there is room for the next one.
"""
import discord
from discord import app_commands
from discord.ext import commands

#: The two seeded channel IDs below only exist in the home server. Writing them
#: into any other guild pointed its moderation log at a channel that isn't there
#: (so /modlog went silent) and its level-up card at a foreign channel.
HOME_GUILD_ID = 1542278896148422676
HOME_BOT_CMD_CHANNEL_ID = 1547637176697757846
HOME_LOG_CHANNEL_ID = 1546718348119773260


def _push_embed(heading: str, requester: str, body: str, colour: int) -> discord.Embed:
    return discord.Embed(
        title=f"🚀 {heading}",
        description=f"{body}\n\n**Requested by** {requester}",
        color=colour,
        timestamp=discord.utils.utcnow(),
    )


#: What `/bot push` says when there is no deploy hook to hand the deploy to. The
#: release notes come from the changelog that shipped *with* the running code, so
#: posting them is honest — and saying plainly that no code moved is the whole
#: point of reporting the absence of a hook instead of implying a deploy.
_NO_HOOK_REPORT: dict[str, tuple[str, str]] = {
    "posted": (
        "Release notes posted — nothing deployed",
        "No deploy hook is configured, so no code moved. This version's release "
        "notes are in this server's update channel. Put a Render Deploy Hook URL in "
        "`RENDER_DEPLOY_HOOK_URL` and this command can ship the code itself.",
    ),
    "up_to_date": (
        "Nothing new to post — nothing deployed",
        "This server has already been told about the version that is running, and "
        "no deploy hook is configured, so nothing was deployed either.",
    ),
    "unbound": (
        "Nowhere to post the notes — nothing deployed",
        "This server has no update channel bound, so there was nowhere for the "
        "notes to go. A channel named `#update` is picked up automatically, or run "
        "`/bot db` to bind what is there.",
    ),
    "unreachable": (
        "The update channel is gone — nothing deployed",
        "The channel this server has bound for updates no longer exists, so the "
        "notes were not posted. Set a new one and run `/bot db`.",
    ),
    "failed": (
        "Posting the notes failed — nothing deployed",
        "The release notes could not be posted, so nothing happened at all. The "
        "next boot tries again, and the reason is in the host's log.",
    ),
}


class BotConfig(commands.Cog):
    """Operator commands for the bot itself, grouped so they cost one slot."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Not named for the command: discord.py refuses any cog attribute starting
    # with ``bot_`` (it reserves the prefix), so the group is ``operator_group``
    # while the command it registers is ``/bot``.
    operator_group = app_commands.Group(
        name="bot",
        description="The bot itself: profile, slash commands, database, deploys.",
    )

    async def _require_app_owner(self, interaction: discord.Interaction) -> bool:
        """Only the Discord application owner may edit the global bot profile.

        Checking guild.owner_id let *any* server owner the bot shared a guild
        with replace the avatar/banner for every guild — a multi-tenant takeover.
        """
        import os
        configured = int(os.environ.get("OWNER_USER_ID", "0") or "0")
        if configured and interaction.user.id == configured:
            return True
        if await interaction.client.is_owner(interaction.user):
            return True
        await interaction.response.send_message(
            "❌ Only the **bot application owner** can change the bot's profile.",
            ephemeral=True,
        )
        return False

    async def _push_channel(self, guild):
        """Where a push is reported: this server's test channel, else its update one.

        The test channel is the surface an operator is watching while they try
        things out, which is why a push is reported there first — and the update
        channel is the fallback, because every server that wants to hear about
        releases has one bound.
        """
        from utils import bot_channels

        if guild is None:
            return None
        for role in ("test", "update"):
            channel_id = await bot_channels.bound(guild.id, role)
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is not None:
                return channel
        return None

    async def _announce(self, guild) -> str:
        """Tell this server what changed, through the path the boot uses.

        Reached through the client rather than imported: ``bot.py`` is ``__main__``
        in production, so importing it by name would build a second bot.
        """
        announce = getattr(self.bot, "announce_release_notes_to_guild", None)
        if announce is None or guild is None:
            return "failed"
        return await announce(guild)

    # ── profile ────────────────────────────────────────────────────────────

    @operator_group.command(name="avatar", description="Set the bot's profile picture (Owner only).")
    @app_commands.describe(image="The image file to use as the avatar")
    @app_commands.checks.has_permissions(administrator=True)
    async def avatar(self, interaction: discord.Interaction, image: discord.Attachment):
        if not await self._require_app_owner(interaction):
            return

        if not image.content_type or not image.content_type.startswith("image/"):
            return await interaction.response.send_message("Please upload a valid image file.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            image_bytes = await image.read()
            await self.bot.user.edit(avatar=image_bytes)
            await interaction.followup.send("Avatar updated successfully! (It may take a moment to cache in Discord)")
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to update avatar: {e.text}")
        except Exception as e:
            await interaction.followup.send(f"An unexpected error occurred: {e}")

    @operator_group.command(name="banner", description="Set the bot's profile banner (Owner only).")
    @app_commands.describe(image="The image file to use as the banner")
    @app_commands.checks.has_permissions(administrator=True)
    async def banner(self, interaction: discord.Interaction, image: discord.Attachment):
        if not await self._require_app_owner(interaction):
            return

        if not image.content_type or not image.content_type.startswith("image/"):
            return await interaction.response.send_message("Please upload a valid image file.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            image_bytes = await image.read()
            await self.bot.user.edit(banner=image_bytes)
            await interaction.followup.send("Banner updated successfully! (It may take a moment to cache in Discord)")
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to update banner: {e.text}")
        except Exception as e:
            await interaction.followup.send(f"An unexpected error occurred: {e}")

    # ── the bot's own machinery ────────────────────────────────────────────

    @operator_group.command(name="sync", description="Publish any missing or out-of-date slash commands.")
    # Declared rather than hand-rolled: the guild owner is always an
    # administrator, so this is the same audience as the body check it replaces.
    @app_commands.checks.has_permissions(administrator=True)
    async def sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # Deliberately *not* `tree.sync()` / `copy_global_to` + `tree.sync(guild=)`.
        # Those are **bulk overwrites**, and Discord applies them destructively: the
        # scope's whole command list is cleared before the new one is sent. A run
        # that hits the daily write limit (error 30034) therefore loses every
        # command it had not re-sent yet — this guild went from 74 commands to one
        # exactly that way, and `/bot sync` was a second route to the same wipe. The
        # publisher upserts only what is missing or out of date, so it can never
        # take a command away.
        publisher = getattr(self.bot, "publish_commands", None)
        if publisher is None:
            return await interaction.followup.send(
                "❌ This bot build cannot publish commands. Restart the bot and try again."
            )
        try:
            results = await publisher(self.bot, force=True)
            lines = ["✅ **Slash commands published** — nothing was removed."]
            for name, count in results.items():
                label = "Global (every server)" if name == "(global)" else name
                lines.append(f"• {label}: **{count}** commands")
            lines.append(
                "\nHitting Discord's daily write limit is normal — the rest publish "
                "themselves in the background as it refills."
            )
            await interaction.followup.send("\n".join(lines))
        except Exception as e:
            await interaction.followup.send(f"❌ Sync failed: {e}")

    @operator_group.command(name="db", description="Verify and update all bot database tables and default guild settings.")
    @app_commands.checks.has_permissions(administrator=True)
    async def db(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            from utils import db
            await db.setup()
            notes = ["• All database tables verified and created."]
            if interaction.guild:
                guild = interaction.guild
                if guild.id == HOME_GUILD_ID:
                    await db.set_setting(guild.id, "bot_cmd_channel_id", HOME_BOT_CMD_CHANNEL_ID)
                    await db.set_setting(guild.id, "log_channel_id", HOME_LOG_CHANNEL_ID)
                    notes.append(
                        "• Command channel set to `💬・bot-cmd` and moderation logs to `logs`."
                    )
                else:
                    logs = discord.utils.get(guild.text_channels, name="logs")
                    if logs:
                        await db.set_setting(guild.id, "log_channel_id", logs.id)
                        notes.append(f"• Moderation logs bound to {logs.mention}.")
                    else:
                        notes.append(
                            "• No `#logs` channel found here, so the log channel was left "
                            "alone — set one with `/modlog`."
                        )
                    cmd_channel = (
                        discord.utils.get(guild.text_channels, name="bot-cmd")
                        or discord.utils.get(guild.text_channels, name="💬・bot-cmd")
                    )
                    if cmd_channel:
                        await db.set_setting(guild.id, "bot_cmd_channel_id", cmd_channel.id)
                        notes.append(f"• Level-up cards will post in {cmd_channel.mention}.")
                    else:
                        notes.append(
                            "• No `#bot-cmd` channel found here, so level-up cards stay "
                            "off until one exists."
                        )
                # The rest of the house channels — release notes, failures, bug
                # reports, the test channel — bind by name the same way, and this is
                # the one way to re-run that without waiting for a restart.
                from utils import bot_channels

                newly = await bot_channels.ensure_bound(guild)
                if newly:
                    notes.append(f"• Bound {bot_channels.describe(guild, newly)}.")
                # ``roles_for``, not ``ROLES``: the operator's two channels
                # (`guilds`, `cmdlog`) belong to the bot's own server and can
                # never bind here, so listing them as "still unbound" would send
                # an admin off to create a channel that will never be picked up.
                missing = [
                    role
                    for role in bot_channels.roles_for(guild.id)
                    if await bot_channels.bound(guild.id, role) is None
                ]
                if missing:
                    # The names come from the table the binder itself uses, so this
                    # hint cannot fall out of step with what actually binds.
                    wanted = ", ".join(
                        f"`#{bot_channels.candidates(role)[0]}`"
                        for role in missing
                        if bot_channels.candidates(role)
                    )
                    notes.append(
                        f"• Still unbound here: {', '.join(missing)} — a channel named "
                        f"{wanted} is picked up automatically."
                    )
            notes.append("• Economy and leveling tables ready.")
            await interaction.followup.send(
                "✅ **Database Successfully Updated!**\n" + "\n".join(notes)
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Database update failed: {e}")

    @operator_group.command(
        name="push",
        description="Put the newest pushed code live, or post this version's notes.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def push(self, interaction: discord.Interaction):
        """Ship the update, in the one way this process is able to.

        A bot process cannot commit: what is running here was built from the
        repository, so "push the update" can only mean *deploy what is already on
        GitHub* — one POST to a Render Deploy Hook. With a hook configured the
        deploy is handed over, and the boot that follows posts the release notes to
        every server by itself. Without one, the honest thing is to say that no code
        moved and post this version's notes here and now.
        """
        from utils import deploy, style

        if not await self._require_app_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        where = f" in **{guild.name}**" if guild is not None else ""
        who = f"{interaction.user.mention}{where}"
        channel = await self._push_channel(guild)

        # The card goes out *before* anything is attempted, and is edited with the
        # outcome afterwards: a deploy restarts this process, and the record of who
        # asked for it must not depend on surviving that.
        card = None
        if channel is not None:
            card = await style.send_card(
                channel,
                embed=_push_embed(
                    "Deploy requested", who, "Handing the request to Render…", style.ACCENT
                ),
            )

        if deploy.configured():
            accepted, note = await deploy.trigger()
            heading = "Deploy accepted" if accepted else "Deploy not started"
            colour = style.SUCCESS if accepted else style.DANGER
            if accepted:
                note += (
                    "\n\nThe bot restarts in a couple of minutes and posts the release "
                    "notes to every server's own update channel by itself."
                )
        else:
            accepted = False
            outcome = await self._announce(guild)
            heading, note = _NO_HOOK_REPORT.get(outcome, _NO_HOOK_REPORT["failed"])
            colour = style.WARN

        if card is not None:
            try:
                await style.edit_card(card, embed=_push_embed(heading, who, note, colour))
            except Exception as e:
                # The record already exists in the channel; failing to restyle it
                # must not fail the command that made it.
                print(f"[PUSH] Could not update the push card: {e}", flush=True)

        await interaction.followup.send(f"**{heading}**\n{note}")
    @operator_group.command(
        name="bug",
        description="Report a bug in the bot itself — files a report card, not a ticket.",
    )
    @app_commands.describe(
        command="What you ran, if it was a command (for example /balance).",
        when="When it happened, as best you can say (for example “about ten minutes ago”).",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def bug(
        self,
        interaction: discord.Interaction,
        command: str | None = None,
        when: str | None = None,
    ):
        """The administrator's way into the bug-report path.

        A fault in the bot is an operator's to report — they are the ones who can
        say what they ran and what the bot answered — so this is gated on
        Administrator. The report is a **message** in this server's bug channel,
        with a thread on it; when this server has none it goes to the bot's own
        server's bug channel instead, so a report can never be swallowed. It
        deliberately does not open a ticket: tickets are for members' own problems,
        and a bug in the bot is not the bot's customer support.

        ``command`` and ``when`` are optional and only prefill the form: a report
        nobody can check is a report that names neither what was run nor when,
        and typing those twice — once here and once in the modal — is how they end
        up left blank.
        """
        from cogs import help as help_cog

        await help_cog.open_bug_report(
            interaction,
            source="`/bot bug`",
            command=command or "",
            when=when or "",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BotConfig(bot))
