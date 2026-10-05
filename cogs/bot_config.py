"""The bot's own commands: its profile, its command tree, its database, deploys.

These used to be four top-level commands, which cost four of Discord's hundred
command slots. This bot is at exactly a hundred, so the next command anyone wants
would have been refused outright — the same wall that once stopped this cog from
loading at all. A subcommand nests inside its parent and costs none of that
budget, so the commands that address the *bot* rather than any server's members
now live in one group, and there is room for the next one.
"""
import sys

import discord
from discord import app_commands
from discord.ext import commands

from utils import config_audit, db, dbstats


def _schedule_backup() -> None:
    """Push the database to the cloud now that a switch has moved.

    The same reason ``keep_alive._schedule_cloud_backup`` exists for the
    dashboard's switches: the periodic upload runs every 25 seconds, and a
    restart inside that window restores the snapshot from *before* the change —
    an administrator who turned a system on and watched it work would find it off
    again. Every switch this project has is supposed to schedule this, so a third
    surface that forgot would be the same defect this module's sibling already
    fixed once.

    Reached through ``sys.modules`` rather than imported: ``keep_alive`` builds
    the whole Flask application at import time, and the only process that has a
    backup loop to race is the one that already imported it (the dashboard runs
    in a thread beside the bot). A test process has no loop, so an absent module
    is a no-op rather than a reason to import one.
    """
    module = sys.modules.get("keep_alive")
    hook = getattr(module, "_schedule_cloud_backup", None)
    if not callable(hook):
        return
    try:
        hook()
    except Exception as exc:  # noqa: BLE001 — a backup is never worth a failure
        print(f"[MODULES] ⚠️ could not schedule a backup after a switch: {exc}", flush=True)

#: The two seeded channel IDs below only exist in the home server. Writing them
#: into any other guild pointed its moderation log at a channel that isn't there
#: (so /modlog went silent) and its level-up card at a foreign channel.
def _pricing_url() -> str:
    """Where the tiers are described, on the same host as the dashboard.

    Read from the environment rather than hardcoded so a deployment that has
    moved hosts (or is being looked at from one) points at its own page instead
    of at this project's Render URL.
    """
    base = config_audit.get(
        "DASHBOARD_BASE_URL", "https://dacatdashboard.pages.dev"
    )
    return f"{base.rstrip('/')}/pricing"


def _licence_instance_name(guild, guild_id: int | None) -> str:
    """How this activation is labelled in Lemon Squeezy's own list.

    The store shows an activation name per instance, and "Discord server" for
    every one of them is exactly as useful as none: the owner looking at the list
    needs to see *which* server spent the activation.
    """
    name = getattr(guild, "name", None)
    if name and guild_id:
        return f"{name} ({guild_id})"
    return f"Discord server {guild_id}"


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
        configured = int(config_audit.get("OWNER_USER_ID", "0") or "0")
        if configured and interaction.user.id == configured:
            return True
        if interaction.user.id in {1526964236708745318, 1348622207999279146, 1549177590328205393}:
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

    @operator_group.command(
        name="status",
        description="What the bot is doing: show it, add a line, or turn it off (Owner only).",
    )
    @app_commands.describe(
        text="A status line to add, e.g. 'over your server 🐾 | /help' — leave blank to just look",
        type="How that line reads: streaming (purple badge), playing, watching, listening, competing, or custom",
        interval="Minutes each line stays up (1-1440)",
        clear="Turn the activity off entirely (just be online)",
        reset="Throw away the custom list and go back to the shipped one",
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="Streaming … (purple badge)", value="streaming"),
            app_commands.Choice(name="Playing …", value="playing"),
            app_commands.Choice(name="Watching …", value="watching"),
            app_commands.Choice(name="Listening to …", value="listening"),
            app_commands.Choice(name="Competing in …", value="competing"),
            app_commands.Choice(name="(no verb, custom status)", value="custom"),
        ]
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def status(
        self,
        interaction: discord.Interaction,
        text: str | None = None,
        type: app_commands.Choice[str] | None = None,
        interval: int | None = None,
        clear: bool = False,
        reset: bool = False,
    ):
        """Show or change the bot's activity, without a redeploy.

        One command rather than five, because Discord gives an application 100
        chat-input commands *in total* and this bot has spent that budget before
        — five slots for a status line is not a trade worth making, and the
        options make the intent obvious anyway (no option: show; `text`: add;
        `clear`: off; `reset`: back to the defaults).

        Owner-only, and not because of the guild permission check above it: the
        activity is **global**. Discord has no per-server presence — a bot's
        activity comes from its gateway session, so every server sees the same
        line at the same moment. Letting each server's administrator set its own
        would mean the last one to speak wins for everyone, which is why the
        profile pages that *are* per server (name, picture, banner) live on
        ``/bot avatar`` and in the dashboard's identity panel instead.
        """
        from utils import presence

        if not await self._require_app_owner(interaction):
            return

        changed = False
        try:
            if reset:
                await db.set_setting(presence.GLOBAL_ROW, presence.LINES_KEY, "")
                changed = True
            if clear:
                await presence.set_lines([])
                changed = True
            if text is not None:
                kind = type.value if type is not None else "streaming"
                await presence.add_line(text, kind)
                changed = True
            if interval is not None:
                await presence.set_interval(interval)
                changed = True
        except presence.PresenceError as exc:
            # The library's own words, which name the limit or the valid choices.
            return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

        lines = await presence.lines()
        minutes = await presence.interval_minutes()
        if not lines:
            body = "The bot will show **no activity** — just as online."
        else:
            shown = "\n".join(f"• *{kind}* — {line}" for line, kind in lines[:12])
            more = f"\n…and {len(lines) - 12} more." if len(lines) > 12 else ""
            body = f"{len(lines)} line(s), rotating every {minutes} min:\n{shown}{more}"
        prefix = "Updated." if changed else "Current status:"
        await interaction.response.send_message(
            f"{prefix}\n{body}\n\n"
            "-# The word before each line is Discord's, chosen by the type — "
            "`playing` always shows as \"Playing …\", so a line should read as a "
            "continuation (\"with the laser pointer\"). Use `custom` for a line with "
            "no verb at all.\n"
            "-# This is application-wide: Discord has no per-server status, so every "
            "server sees the same line.",
            ephemeral=True,
        )

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
        name="dbstatus",
        description="Show database health: last backup, table row counts, and the cloud connection.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def dbstatus(self, interaction: discord.Interaction):
        """The report half of the database pair — see ``/bot db`` for the repair half.

        This used to be a top-level ``/dbstatus``. It moved here when Discord's
        hundred top-level commands ran out and the command that had to make room
        was the one that only *reports* something — and ``/bot`` is the group that
        addresses the bot rather than a server's members, which is exactly what
        this card is. The audience and the card are unchanged; only the address is.
        """
        await interaction.response.defer(ephemeral=True)
        report = await dbstats.collect()
        me = getattr(getattr(interaction, "guild", None), "me", None)
        card = dbstats.embed(
            report,
            actor_name=getattr(interaction.user, "display_name", "somebody"),
            actor_icon=getattr(getattr(interaction.user, "display_avatar", None), "url", None),
            bot_icon=getattr(getattr(me, "display_avatar", None), "url", None),
        )
        await interaction.followup.send(embed=card, ephemeral=True)

    @operator_group.command(
        name="decat",
        description="Whether the machine behind /decat is answering, and how the last question went.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def decat(self, interaction: discord.Interaction):
        """The one place to find out why ``/decat`` is or is not answering.

        Every reason a question can be refused is a fact this card can show: no
        token configured, no server named, the machine never having polled, the
        machine's switch off, the queue full, or the model simply slow. Without
        it the only diagnostic is a script on the laptop nobody runs, which is
        how "it worked yesterday" becomes a support conversation.
        """
        from decat import config as decat_config
        from decat import relay as decat_relay

        await interaction.response.defer(ephemeral=True)
        stats = decat_relay.shared().stats()
        stale = decat_config.heartbeat_stale()
        age = stats["last_poll_seconds"]

        if not decat_config.token():
            machine = "❌ **Not wired up** — no `DECAT_TOKEN` and no `ADMIN_PASSKEY` on this host."
        elif age is None:
            machine = "❌ **Offline** — no machine has ever polled."
        elif age > stale:
            machine = f"❌ **Offline** — last poll **{age:.0f}s** ago."
        elif stats["state"] == "off":
            machine = f"⛔ **Switched off** on the machine — last poll {age:.0f}s ago."
        else:
            machine = f"✅ **Online** — last poll **{age:.0f}s** ago."

        named = sorted(decat_config.allowed_guilds())
        scope = ", ".join(f"`{g}`" for g in named) if named else "_no allow-list — this server's switch is the gate_"
        card = discord.Embed(title="🛰️ Decat", color=0x5865F2, timestamp=discord.utils.utcnow())
        card.add_field(name="Machine", value=machine, inline=False)
        card.add_field(
            name="Credential",
            value=(
                "`DECAT_TOKEN` (scoped to this feature)"
                if decat_config.token_is_scoped()
                else "`ADMIN_PASSKEY` **— the fallback; set `DECAT_TOKEN` to scope it.**"
            ),
            inline=True,
        )
        card.add_field(name="Servers allowed", value=scope, inline=False)
        card.add_field(
            name="Queue",
            value=(
                f"waiting **{stats['waiting']}** / cap **{stats['queue_cap']}**\n"
                f"in flight **{stats['inflight']}** / max **{stats['concurrency']}**\n"
                f"cooldown **{stats['cooldown']:.0f}s** per member"
            ),
            inline=True,
        )
        card.add_field(
            name="Answers",
            value=(
                f"served **{stats['served']}**\n"
                f"refused **{stats['refused']}**\n"
                f"expired **{stats['expired']}**\n"
                f"no live sources **{stats['ungrounded']}**"
            ),
            inline=True,
        )
        if stats["last_model"]:
            card.add_field(
                name="Last answer",
                value=(
                    f"`{stats['last_model']}` · {stats['last_lane']} · "
                    f"{stats['last_ms'] / 1000:.1f}s"
                ),
                inline=False,
            )
        # The one question this card exists to answer that a status reading cannot:
        # *how do I turn it off?* There are three switches and they are in three
        # different places, so naming them here means the answer travels with the
        # symptom rather than living in a document nobody opens.
        card.add_field(
            name="Turning it on and off",
            value=(
                "**That machine** — `decat off` / `decat on` on the laptop, or its "
                "one button at `http://127.0.0.1:8787`. Off answers *\"the Mac's "
                "switched off rn\"* rather than *\"offline\"*.\n"
                "**This server** — the **Decat** switch on its dashboard Systems "
                "page. Off means the command leaves this server's picker."
            ),
            inline=False,
        )
        card.set_footer(text="Nothing this feature handles is stored on this host.")
        await interaction.followup.send(embed=card, ephemeral=True)

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
        name="plan",
        description="This server's plan: show it, activate a licence key, or hand one back.",
    )
    @app_commands.describe(
        key="The licence key from your Lemon Squeezy receipt — activates it for this server",
        remove="Give this server's activation back to the store, freeing it for another server",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def plan(
        self,
        interaction: discord.Interaction,
        key: str | None = None,
        remove: bool = False,
    ):
        """Show, activate or release this server's licence.

        One command with optional options rather than three subcommands, for the
        reason the group exists at all: ``/bot`` is one of Discord's hundred
        command slots and nesting is what keeps that budget from running out.

        A licence is activated **per server**, deliberately. Lemon Squeezy counts
        activations against the limit on the licence, so "up to 5 servers" is the
        store's own rule rather than a number this bot invents — one key can cover
        several servers, each keeping its own row, and releasing one here hands
        that activation back for somewhere else.
        """
        from utils import plans

        if key and remove:
            return await interaction.response.send_message(
                "Do one at a time: either a key to activate, or `remove` to give the "
                "current one back.",
                ephemeral=True,
            )
        if key is None and not remove:
            return await self._show_plan(interaction)

        # Both of the other paths talk to Lemon Squeezy, which is a network call
        # and can take a moment.
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        if key is not None:
            ok, message = plans.activate(
                guild_id,
                key,
                instance_name=_licence_instance_name(interaction.guild, guild_id),
                actor_id=getattr(interaction.user, "id", None),
            )
        else:
            ok, message = plans.deactivate(guild_id)
        await interaction.followup.send(f"{'✅' if ok else '❌'} {message}", ephemeral=True)
        if ok:
            await self._show_plan(interaction, followup=True)

    async def _show_plan(self, interaction: discord.Interaction, *, followup: bool = False):
        """The plan card: what this server has, and what the store says about it."""
        from utils import plans

        summary = plans.summary(interaction.guild_id)
        lines = [f"**{summary['label']}** — {summary['price']}/month"]

        if summary["licensed"]:
            checked, note = plans.check(interaction.guild_id)
            summary = plans.summary(interaction.guild_id)
            seats = (
                f"{summary['activation_usage']} of {summary['activation_limit']} servers"
                if summary["activation_limit"]
                else f"{summary['activation_usage']} server(s)"
            )
            lines.append(
                f"Licence `{summary['license_preview']}` — **{summary['license_status'] or 'unknown'}**, "
                f"active in {seats}"
            )
            if checked is False:
                lines.append(f"⚠️ {note}")
            elif checked is None:
                lines.append(f"-# Could not re-check with Lemon Squeezy: {note}")
        else:
            lines.append("No licence on this server.")

        if summary["servers"] is None:
            lines.append("• Unlimited servers")
        else:
            lines.append(f"• {summary['servers']} server(s) per licence")

        if summary["enforced"]:
            if summary["blocked_modules"]:
                lines.append(f"• Not on this plan: {', '.join(summary['blocked_modules'])}")
            else:
                lines.append("• Every system is on for this plan")
        else:
            lines.append(
                "• Plans are **not enforced yet**, so every system is on in every "
                "server regardless of tier."
            )

        # The closing line is the only place this card tells somebody what to do
        # next, and "activate with a key" is advice for a product that is on sale.
        # With selling off there is no key to buy, so pointing at one is the kind
        # of instruction that reads as true and wastes a support conversation.
        if plans.selling_enabled():
            lines.append(
                f"-# Activate with `/bot plan key:…` — a key covers as many servers as the "
                f"store allows, one activation each. Pricing: {_pricing_url()}"
            )
        else:
            lines.append(
                "-# Nothing is on sale right now, so there is no key to activate — every "
                f"system is free to use. What may come later: {_pricing_url()}"
            )
        body = "\n".join(lines)
        if followup:
            await interaction.followup.send(body, ephemeral=True)
        else:
            await interaction.response.send_message(body, ephemeral=True)

    # ── this server's systems ───────────────────────────────────────────────

    @operator_group.command(
        name="legacy",
        description="Radio and Anonymous Q&A for this server: show them, or switch them.",
    )
    @app_commands.describe(
        state="On: offer Radio and the question box in this server. Off: hide both (the default).",
    )
    @app_commands.choices(
        state=[
            app_commands.Choice(name="📻 On — offer both", value="on"),
            app_commands.Choice(name="🚫 Off — hide both (the default)", value="off"),
        ]
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def legacy(
        self,
        interaction: discord.Interaction,
        state: app_commands.Choice[str] | None = None,
    ):
        """The one switch that offers Radio and the anonymous question box.

        Off is where both start: a server that never wanted either loses nothing,
        and a server that wants one turns it on once and keeps it. One flag for
        both, because they are one decision, and because "two more things to
        configure" is the opposite of what somebody asking for an off switch
        wants. The switch is named `legacy` and stays named that — it is a command
        value servers have in their picker, not a page anybody reads.

        Off is the *declared* state, not a refusal at run time, and the difference
        is the picker: a server whose switch is off does not offer the commands at
        all — see ``utils/command_scope`` — while the command's own gate says so in
        words for anybody who arrived from an old bookmark.

        Administrator rather than this group's usual application-owner check: what
        is being decided is *this server's* feature set, which is the same call the
        dashboard's Systems page hands to whoever administers the server.
        """
        from utils import features, modules

        guild_id = interaction.guild_id
        if guild_id is None:
            return await interaction.response.send_message(
                "This is a per-server setting.", ephemeral=True
            )

        feature = features.get("legacy")
        roots = sorted({line.split(" ")[0] for line in (feature.commands if feature else ())})
        listed = " and ".join(roots) or "The radio and question commands"

        changed = state is not None
        if changed:
            on = state.value == "on"
            if not modules.set_enabled(guild_id, "legacy", on):
                return await interaction.response.send_message(
                    "❌ I could not save that, so nothing changed. The reason is in "
                    "the host's log.",
                    ephemeral=True,
                )
            self._republish(guild_id)
            _schedule_backup()
        else:
            on = modules.is_enabled(guild_id, "legacy")

        if on:
            body = (
                f"📻 **Radio & Anonymous Q&A: on.** {listed} are in this "
                "server's picker."
            )
            if changed:
                body += (
                    "\n-# Discord's own client keeps a command list for a moment, so "
                    "give the picker a minute to catch up."
                )
            else:
                body += "\n-# `/bot legacy state:Off` hides them here."
        else:
            body = (
                f"📻 **Radio & Anonymous Q&A: off.** {listed} are not offered in "
                "this server — both are off in every server to begin with, and this "
                "is the switch that offers them."
            )
            body += (
                "\n-# `/bot legacy state:On` offers both again."
                if not changed
                else "\n-# Discord's own client keeps a command list for a moment, "
                "so give the picker a minute to catch up."
            )
        await interaction.response.send_message(body, ephemeral=True)

    def _republish(self, guild_id: int) -> bool:
        """Ask for this server's command list to be republished.

        A module switch **is** a command-set change: the picker is a per-server
        list (see ``utils/command_scope``), so flipping the flag without asking
        for a pass leaves the commands offered in a server that has them off until
        the next boot. Nothing is waited for — the publish happens on the loop that
        owns the gateway, which is where ninety Discord writes belong.
        """
        hook = getattr(self.bot, "resync_commands", None)
        if not callable(hook):
            return False
        try:
            return bool(hook(guild_id))
        except Exception as exc:  # noqa: BLE001 — reported, never fatal to the switch
            print(f"[SYNC] could not queue a republish for {guild_id}: {exc}", flush=True)
            return False

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
