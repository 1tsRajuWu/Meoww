import datetime
import os
import re
import sqlite3
import time
import discord
from discord import app_commands
from discord.ext import commands, tasks
from utils import moderation_actions, modlog, permissions, server_control, db
from utils.style import send_card, display_name


def _duration_words(seconds: int) -> str:
    """A timeout length as a person would say it: 10 minutes, 1 hour, 1 day."""
    minutes = max(1, int(seconds or 0) // 60)
    if minutes < 60:
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour" + ("" if hours == 1 else "s")
    days = hours // 24
    return f"{days} day" + ("" if days == 1 else "s")


def _actor(interaction: discord.Interaction) -> moderation_actions.Actor:
    """Who asked, in the shape every other audit reason in the bot uses.

    A lockdown's reason is read by the server's own administrators in Discord's
    audit log, so it names the person who ran the command — not this bot, which is
    only what Discord credits the change to.
    """
    user = interaction.user
    return moderation_actions.Actor(
        id=int(getattr(user, "id", 0) or 0),
        name=str(getattr(user, "display_name", None) or getattr(user, "name", "somebody")),
        member=user if isinstance(user, discord.Member) else None,
        source="discord",
    )


#: Table names this file will count. A name that is not one of these is skipped
#: rather than quoted into a query.
_PLAIN_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")


def _root_command_name(interaction: discord.Interaction) -> str | None:
    """The name that identifies the command, subcommand or not.

    ``interaction.command.name`` is the *sub*-command for a group member, so
    matching a group against the bypass list with it silently never matches
    (``/defaultrole set`` arrives as ``"set"``). The bot's own tree check
    already reads ``root_parent`` for this reason; do the same here.
    """
    command = interaction.command
    if command is None:
        return None
    parent = getattr(command, "root_parent", None)
    return parent.name if parent is not None else command.name


def _lock_warnings(guild, channel) -> str:
    """What this lock cannot do, said on the card, when it cannot do it.

    Locking needs **Manage Channels**; removing needs **Manage Messages**; the
    timeout needs **Moderate Members**. A bot can hold the first without the
    others, and a card that promises removal *and* a timeout while neither is
    possible is the exact kind of claim this feature exists to stop making.
    Unknown is not a warning: a channel whose permissions could not be read gets
    the ordinary card.
    """
    perms = getattr(channel, "permissions_for", None)
    me = getattr(guild, "me", None)
    if not callable(perms) or me is None:
        return ""
    try:
        granted = perms(me)
    except Exception:  # noqa: BLE001
        return ""
    said = []
    if not getattr(granted, "manage_messages", False):
        said.append(
            "⚠️ I do not have **Manage Messages** here, so I cannot remove what is posted."
        )
    if not getattr(granted, "moderate_members", False):
        said.append(
            "⚠️ I do not have **Moderate Members** here, so a spammer cannot be timed out."
        )
    return ("\n\n" + "\n".join(said)) if said else ""


def _members_intent_warning(bot) -> str:
    """The one thing that turns the default role into a no-op, said out loud.

    Member events are only delivered when the privileged **Members intent** is
    enabled, so with it off nobody ever receives the role and nothing anywhere
    reports a problem — the setting just quietly does nothing. The person
    configuring it is the only one who can fix that, so they are the one told.
    """
    if getattr(getattr(bot, "intents", None), "members", False):
        return ""
    return (
        "\n\n⚠️ The **Members intent** is off, so Discord never tells the bot that "
        "somebody joined and no role will be handed out. Enable it in the "
        "[Developer Portal](https://discord.com/developers/applications), then set "
        "`ENABLE_MEMBERS_INTENT=1`."
    )


def _local_table_counts(db_path: str) -> tuple[dict[str, int], float, float, str]:
    """Row counts, sizes and an integrity check, with blocking work done off-loop.

    ``SELECT COUNT(*)`` is a full scan, and the tables that grow here are the ones
    with the most rows — called inline this held the event loop, and therefore
    every other command and the gateway heartbeat, for as long as the biggest
    table took to count. The sync-status half of /dbstatus already ran in a worker
    thread for the same reason.
    """
    counts: dict[str, int] = {}
    size_kb = 0.0
    wal_kb = 0.0
    integrity = "unknown"
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.Error as e:
        return counts, size_kb, wal_kb, str(e)
    try:
        try:
            size_kb = os.path.getsize(db_path) / 1024
        except OSError:
            size_kb = 0.0
        try:
            wal_kb = os.path.getsize(f"{db_path}-wal") / 1024
        except OSError:
            wal_kb = 0.0  # no WAL file is the normal case after a clean checkpoint
        cur = conn.cursor()
        try:
            integrity = str(cur.execute("PRAGMA quick_check(1)").fetchone()[0])
        except sqlite3.Error as e:
            integrity = str(e)
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        for (name,) in cur.fetchall():
            # A table name is an *identifier*, and an identifier cannot be a bound
            # parameter — ``?`` stands for a value, never for a name — so a query
            # that counts one is necessarily built by hand. The rule here is that
            # only names that are *nothing but* letters, digits and underscores
            # are counted at all: every table this bot creates is one of those,
            # so a name that needs quoting is either somebody else's table or a
            # name shaped like SQL, and a row count is not worth being the place
            # that finds out which.
            table = str(name)
            if not _PLAIN_IDENTIFIER.fullmatch(table):
                continue
            try:
                counts[table] = cur.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except sqlite3.Error:
                counts[table] = -1  # unreadable
    except sqlite3.Error as e:
        print(f"[DBSTATUS] Could not read the local database: {e}", flush=True)
    finally:
        conn.close()
    return counts, size_kb, wal_kb, integrity


class ServerControl(commands.Cog):
    """Lockdown, slowmode, and role management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # One pass, at boot: the channels locked by a `/lockdown` older than the
        # ledger are armed here. A loop rather than an `on_ready` listener because
        # `on_ready` fires again on every reconnect and this must not keep asking
        # Discord's audit log the same question.
        self.adopt.start()

    def cog_unload(self):
        self.adopt.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            cmd_name = _root_command_name(interaction)
            # ``dbstatus`` belongs on this list for the same reason the other
            # three do: asking whether the database is healthy has nothing to do
            # with whether the moderation module is on. It was missing, so a
            # server with moderation off was refused ``/dbstatus`` — and it is
            # declared always-reachable in ``features.ALWAYS_ALLOWED_COMMANDS``,
            # so the per-server command rule and the enforcement disagreed about
            # it. Caught by ``tests/test_command_scope.py``.
            if cmd_name not in ["serverinfo", "userinfo", "defaultrole", "dbstatus"]:
                if not self.bot.is_module_enabled(interaction.guild_id, "moderation"):
                    await interaction.response.send_message(
                        "⚠️ The **Moderation** system is currently **turned off** on this server.\n"
                        "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                        ephemeral=True
                    )
                    return False
        return True

    @app_commands.command(name="serverinfo", description="Get comprehensive information about the server.")
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        created_ts = int(guild.created_at.timestamp())

        # ``guild.owner`` is ``get_member(owner_id)``, which is None without the
        # Members intent — this card used to say "Server Owner: Unknown" on a
        # server whose owner was sitting right there. Resolve the name instead.
        owner = guild.owner
        owner_name = (
            owner.display_name
            if owner
            else await display_name(guild, guild.owner_id, client=interaction.client)
        )

        embed = discord.Embed(
            title=f"🏰 {guild.name}",
            description=(
                f"> 👑 **Server Owner:** {owner_name}\n"
                f"> 📅 **Created:** <t:{created_ts}:D> (<t:{created_ts}:R>)\n"
                f"> 🆔 **Server ID:** `{guild.id}`"
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_author(name="DaCat — Server Information", icon_url=guild.me.display_avatar.url)

        embed.add_field(
            name="👥 Members",
            value=f"**{guild.member_count:,}** total members",
            inline=True
        )
        embed.add_field(
            name="💬 Channels",
            value=f"**{len(guild.text_channels)}** text · **{len(guild.voice_channels)}** voice",
            inline=True
        )
        embed.add_field(
            name="✨ Boost Status",
            value=f"Level **{guild.premium_tier}** ({guild.premium_subscription_count} boosts)",
            inline=True
        )
        embed.add_field(
            name="🛡️ Roles",
            value=f"**{len(guild.roles)}** roles configured",
            inline=True
        )
        embed.add_field(
            name="🔒 Verification",
            value=f"`{str(guild.verification_level).capitalize()}`",
            inline=True
        )
        embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await send_card(interaction, embed=embed)

    @app_commands.command(name="userinfo", description="Get detailed information about a user.")
    @app_commands.describe(member="The member to inspect (leave empty for yourself)")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        created_ts = int(member.created_at.timestamp())
        joined_ts = int(member.joined_at.timestamp()) if member.joined_at else None

        embed = discord.Embed(
            title=f"👤 {member.display_name}",
            description=(
                f"> 📛 **Username:** `{member.name}`\n"
                f"> 🆔 **User ID:** `{member.id}`\n"
                f"> 🤖 **Bot Account:** {'Yes' if member.bot else 'No'}"
            ),
            color=member.color if member.color.value != 0 else 0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name="DaCat — User Profile", icon_url=interaction.guild.me.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)

        embed.add_field(
            name="📅 Account Created",
            value=f"<t:{created_ts}:D>\n(<t:{created_ts}:R>)",
            inline=True
        )
        if joined_ts:
            embed.add_field(
                name="📥 Joined Server",
                value=f"<t:{joined_ts}:D>\n(<t:{joined_ts}:R>)",
                inline=True
            )
        embed.add_field(
            name="🔝 Top Role",
            value=member.top_role.mention if member.top_role else "None",
            inline=True
        )

        roles = [r.mention for r in reversed(member.roles[1:])]
        if roles:
            role_text = " ".join(roles)
            if len(role_text) > 1024:
                role_text = f"{' '.join(roles[:15])} ... (+{len(roles)-15} more)"
            embed.add_field(name=f"🎭 Roles [{len(roles)}]", value=role_text, inline=False)

        embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await send_card(interaction, embed=embed)

    @app_commands.command(name="lockdown", description="Lock this channel: nobody can send, and what they send is removed.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lockdown(self, interaction: discord.Interaction):
        # The lock goes through the same door the server-wide one uses, so this
        # channel lands in the ledger — which is both what `/unlock` restores from
        # and what the force-delete listener below reads. The old body wrote the
        # permission and nothing else, which is why unlocking it could only
        # *allow* `@everyone` to talk.
        report = await server_control.lock_channel(
            interaction.guild, interaction.channel, _actor(interaction)
        )
        if not report.ok:
            embed = discord.Embed(
                title="⚠️ Channel Not Locked",
                description=report.sentence(),
                color=0xED4245,
            )
            return await send_card(interaction, embed=embed, ephemeral=True)
        embed = discord.Embed(
            title="🔒 Channel Locked Down",
            description=(
                f"This channel has been locked by {interaction.user.mention}.\n"
                "Every message sent here is deleted until it is unlocked — for "
                "everyone, administrators included — and the sender is warned by "
                "DM. A burst of messages earns a timeout: ten minutes, then an "
                "hour, then a day."
                + _lock_warnings(interaction.guild, interaction.channel)
            ),
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="DaCat Security")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="unlock", description="Unlock this channel: stop removing messages and restore permissions.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        report = await server_control.unlock_channel(
            interaction.guild, interaction.channel, _actor(interaction)
        )
        if not report.ok:
            embed = discord.Embed(
                title="⚠️ Channel Not Unlocked",
                description=report.sentence(),
                color=0xED4245,
            )
            return await send_card(interaction, embed=embed, ephemeral=True)
        embed = discord.Embed(
            title="🔓 Channel Unlocked",
            description=(
                f"This channel has been unlocked by {interaction.user.mention}.\n"
                "Messages are kept again, and the permissions are the ones that "
                "were here before the lockdown."
            ),
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="DaCat Security")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="lockdown-server", description="Lock ALL text channels in the server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def lockdown_server(self, interaction: discord.Interaction):
        await interaction.response.defer()
        report = await server_control.lockdown(interaction.guild, _actor(interaction))
        if not report.ok:
            embed = discord.Embed(
                title="⚠️ Server Lockdown Did Not Happen",
                description=report.sentence(),
                color=0xED4245,
            )
            return await send_card(interaction, embed=embed)
        embed = discord.Embed(
            title="🚨 Server-Wide Lockdown Activated",
            description=(
                f"{report.sentence()}\n"
                "Every message sent in a locked channel is deleted until it is "
                "unlocked — for everyone, administrators included — the sender is "
                "warned by DM, and a burst earns a timeout."
            ),
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="DaCat Security")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="unlock-server", description="Unlock ALL text channels in the server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def unlock_server(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # The server-wide undo restores each channel's *own* previous overwrite
        # rather than allowing `@everyone` to talk, so a channel that was already
        # locked before the incident is not opened by lifting the lockdown.
        report = await server_control.unlock(interaction.guild, _actor(interaction))
        if not report.ok:
            embed = discord.Embed(
                title="⚠️ Server Lockdown Not Lifted",
                description=report.sentence(),
                color=0xED4245,
            )
            return await send_card(interaction, embed=embed)
        embed = discord.Embed(
            title="🔓 Server-Wide Lockdown Lifted",
            description=(
                f"{report.sentence()}\n"
                "Messages are kept in those channels again."
            ),
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="DaCat Security")
        await send_card(interaction, embed=embed)

    # ── the locks older than the ledger ─────────────────────────────────────
    #
    # Every `/lockdown` before the last pass wrote a permission and nothing else,
    # which left channels that are locked *and unrecorded* — a deny the sweep walks
    # straight past, because the row is what it reads. This is the row being
    # written for the locks this bot can prove are its own: the audit log says who
    # last touched the `@everyone` overwrite, and a channel somebody denied by hand
    # is staff talking among themselves rather than a lock of ours.

    @tasks.loop(count=1)
    async def adopt(self):
        """Arm the channels this bot locked before it kept a ledger. Once, at boot."""
        for guild in list(getattr(self.bot, "guilds", None) or []):
            try:
                adopted = await server_control.adopt_legacy_locks(
                    guild, getattr(self.bot, "user", None)
                )
            except Exception as exc:  # noqa: BLE001 — one guild must not stop the rest
                print(
                    f"[CONTROL] the adoption pass failed in guild "
                    f"{getattr(guild, 'id', 0)}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                continue
            if not adopted:
                continue
            names = ", ".join(f"#{getattr(channel, 'name', '?')}" for channel in adopted)
            print(
                f"[CONTROL] adopted {len(adopted)} lockdown(s) older than the ledger in "
                f"guild {getattr(guild, 'id', 0)}: {names}",
                flush=True,
            )
            await self._say_what_was_adopted(guild, adopted)

    @adopt.before_loop
    async def _wait_for_ready(self):
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:  # pragma: no cover - a stand-in bot in a unit test
            pass

    async def _say_what_was_adopted(self, guild, adopted) -> None:
        """One card in the server's log: which of its old locks are enforced now.

        In the log rather than in the channels themselves: the rooms are silent on
        purpose, and what an administrator needs is a record they can find later
        saying why messages started disappearing in a channel they locked weeks
        ago — and that one command opens it again.
        """
        listed = "\n".join(f"• #{getattr(channel, 'name', '?')}" for channel in adopted[:20])
        embed = discord.Embed(
            title="🔒 Locks older than my ledger are enforced now",
            description=(
                "These channels were locked before I started recording what I lock, so "
                "nothing was removing what got posted into them. The audit log says I "
                "was the last to change their permissions, so they are mine — and they "
                "are written down now.\n\n"
                f"{listed}\n\n"
                "Messages posted there are deleted and a burst is a timeout, exactly as "
                "with `/lockdown`. `/unlock` in a channel opens it again."
            ),
            color=0xED4245,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="DaCat Security")
        try:
            await modlog.post(guild, embed=embed, what="the adopted lockdowns")
        except Exception as exc:  # noqa: BLE001 — the record is a courtesy, not the lock
            print(f"[CONTROL] could not log the adopted lockdowns: {exc}", flush=True)

    # ── the lock's teeth ────────────────────────────────────────────────────
    #
    # A permission overwrite cannot stop an administrator. Discord's
    # `Administrator` permission and a server's owner bypass channel permissions
    # **by design**, which is how a channel this bot had already locked still had
    # an administrator talking in it: the deny reads as locked and is not one. The
    # ledger is what answers that — the messages are removed, the person is told
    # in their own DMs, and a burst past the wall is a timeout. There is no rank
    # the sweep skips: a locked channel is an incident, and the person still
    # typing in it is the one it is for.

    def _sweep_actor(self) -> moderation_actions.Actor:
        """The sweep named as its own actor: no person asked for this one."""
        bot_user = getattr(self.bot, "user", None)
        return moderation_actions.Actor(
            id=int(getattr(bot_user, "id", 0) or 0),
            name="the lockdown sweep",
            source="discord",
        )

    async def _time_out_the_flood(
        self, guild, member, strike: server_control.Strike, where: str
    ) -> str:
        """Time out a burst past the lock and record it; a refusal sentence, or ``""``.

        Deliberately not routed through ``moderation_actions.apply``: that door's
        hierarchy guard exists so a *moderator* cannot act on an administrator, and
        this is not a moderator's decision — it is the lock's own rule, and the
        rank of the person typing is what it is there to ignore. Discord's own
        limits still apply and are reported rather than swallowed: the **Moderate
        Members** permission, a role above mine, and the server's owner, whom
        Discord does not let any bot time out.
        """
        who = int(getattr(member, "id", 0) or 0)
        seconds = int(strike.seconds or 0)
        reason = f"spamming a locked channel (#{where}): {strike.sentence()}"
        actor = self._sweep_actor()
        if who and who == int(getattr(guild, "owner_id", 0) or 0):
            detail = "Discord does not let a bot time out the server's owner, so that part was refused."
        else:
            try:
                await member.timeout(datetime.timedelta(seconds=seconds), reason=reason)
                detail = ""
            except discord.Forbidden:
                detail = (
                    "Discord refused the timeout — I need **Moderate Members**, and my "
                    "role has to sit above theirs."
                )
            except (discord.NotFound, discord.HTTPException) as exc:
                detail = f"Discord refused the timeout ({type(exc).__name__})."
        if detail:
            print(
                f"[CONTROL] could not time out {who or '?'} for spamming #{where}: {detail}",
                flush=True,
            )
            try:
                await modlog.post(
                    guild,
                    moderation_actions.failure_note_card("timeout", guild, member, actor, detail),
                    what=f"the refused lockdown timeout for member {who}",
                )
            except Exception as exc:  # noqa: BLE001 — the refusal is already printed
                print(f"[CONTROL] could not log the refused timeout: {exc}", flush=True)
            return detail
        try:
            await moderation_actions.record(guild, "timeout", member, actor, reason)
        except Exception as exc:  # noqa: BLE001 — the timeout landed; the record is bookkeeping
            print(f"[CONTROL] timed out {who} but could not record it: {exc}", flush=True)
        print(f"[CONTROL] timed out {who or '?'} for spamming #{where} ({seconds}s)", flush=True)
        return ""

    async def _warn_by_dm(self, guild, member, channel, strike, refusal: str) -> None:
        """The strict half: the person is told, personally, that it was not a glitch.

        A DM rather than a line in the room, because a bot sentence under a deleted
        message is a conversation, and a locked channel is not having one. An inbox
        that refuses it is written to the host log; the modlog card, when the burst
        earned one, is the record either way.
        """
        where = f"#{getattr(channel, 'name', '?')}"
        lines = [
            f"Your message in **{where}** was removed. That channel is **locked**: nothing "
            "posted there is kept, and that is true of every member — administrators and "
            "the server's owner included.",
        ]
        if strike is None:
            lines.append(
                f"Send a burst of {server_control.SPAM_LIMIT} in a few seconds and you will be "
                "timed out: ten minutes first, then an hour, then a day."
            )
        else:
            lines.append(
                f"You sent **{strike.messages}** messages into it inside "
                f"{int(server_control.SPAM_WINDOW_SECONDS)} seconds, so this is timeout "
                f"**{strike.offences}** of the ladder: **{_duration_words(strike.seconds)}**."
            )
            if refusal:
                lines.append(refusal)
        embed = discord.Embed(
            title="🔒 You wrote in a locked channel",
            description="\n\n".join(lines),
            color=0xED4245,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Server", value=str(getattr(guild, "name", "this server")), inline=True)
        embed.set_footer(text="DaCat Security · /unlock in the channel lifts it")
        try:
            await send_card(member, embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(
                f"[CONTROL] could not warn {getattr(member, 'id', '?')} by DM: {exc}",
                flush=True,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Remove anything posted into a locked channel, whoever posted it."""
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        if guild is None or channel is None:
            return
        if not hasattr(channel, "id"):
            return
        if not server_control.is_removable(message):
            return  # the bot's own cards — the lockdown notice is one of them
        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(
            guild.id, "moderation"
        ):
            # A server that has turned Moderation off is not policed by this
            # either; the switch is the same switch that refuses `/lockdown`.
            return
        if not await server_control.is_locked(guild.id, channel):
            return
        name = str(getattr(channel, "name", "?") or "?")
        try:
            await message.delete()
        except discord.Forbidden:
            # The one refusal worth naming: the lock still denies the permission,
            # but the sweep needs **Manage Messages** of its own to act on it.
            print(
                f"[CONTROL] cannot remove a message in #{name} while locked — "
                "I need **Manage Messages** there.",
                flush=True,
            )
            return
        except (discord.NotFound, discord.HTTPException) as exc:
            # A message already gone is fine, and either way the next one is
            # another try.
            print(f"[CONTROL] could not remove a message in #{name}: {exc}", flush=True)
            return
        author = getattr(message, "author", None)
        strike = server_control.note_locked_message(
            guild.id, channel.id, getattr(author, "id", 0)
        )
        refusal = ""
        if strike is not None:
            refusal = await self._time_out_the_flood(guild, author, strike, name)
        if author is not None:
            await self._warn_by_dm(guild, author, channel, strike, refusal)
        print(
            f"[CONTROL] removed a message in #{name} while locked "
            f"(author {getattr(author, 'id', '?')})",
            flush=True,
        )

    @app_commands.command(name="slowmode", description="Set slowmode delay for this channel (seconds, 0 to disable).")
    @app_commands.describe(seconds="Delay in seconds (0 to 21600)")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        await interaction.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            embed = discord.Embed(
                description="⏱️ **Slowmode has been disabled** for this channel.",
                color=0x57F287
            )
        else:
            embed = discord.Embed(
                description=f"⏱️ **Slowmode set to `{seconds}` second(s)** by {interaction.user.mention}.",
                color=0x5865F2
            )
        await send_card(interaction, embed=embed)

    @app_commands.command(name="addrole", description="Add a role to a member.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def addrole(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        # Two different hierarchy questions, and only the first one was asked
        # here before: whether *I* may assign it, and whether *you* may. Without
        # the second, a moderator with Manage Roles could grant Administrator to
        # themselves or an alt.
        problem = permissions.role_grant_problem(role, interaction.user, interaction.guild)
        if problem:
            return await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("❌ I cannot assign a role higher than or equal to my own role.", ephemeral=True)
        await member.add_roles(role)
        
        embed = discord.Embed(
            title="✅ Role Assigned",
            description=f"Successfully added {role.mention} to {member.mention}.",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Actioned by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await send_card(interaction, embed=embed)

    @app_commands.command(name="removerole", description="Remove a role from a member.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def removerole(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        problem = permissions.role_grant_problem(role, interaction.user, interaction.guild)
        if problem:
            return await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
        if role >= interaction.guild.me.top_role:                    
            return await interaction.response.send_message("❌ I cannot remove a role higher than or equal to my own role.", ephemeral=True)
        await member.remove_roles(role)

        embed = discord.Embed(
            title="🗑️ Role Removed",
            description=f"Successfully removed {role.mention} from {member.mention}.",
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Actioned by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await send_card(interaction, embed=embed)

    @app_commands.command(name="announce", description="Send a styled announcement embed to a channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def announce(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str, title: str = "📢 Announcement"):
        embed = discord.Embed(
            title=title,
            description=message,
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        if interaction.guild.icon:
            embed.set_author(name=interaction.guild.name, icon_url=interaction.guild.icon.url)
        embed.set_footer(text=f"Announced by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        
        # Announcements are the one place a ping is the point: the text is written
        # by an admin with Manage Server, so honour whatever they typed. Every
        # other card stays silent by default.
        await send_card(
            channel,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=True),
        )
        await interaction.response.send_message(f"✅ Announcement posted in {channel.mention}!", ephemeral=True)

    @app_commands.command(name="dbstatus", description="Show database health: last backup, table row counts, and Supabase connection.")
    @app_commands.checks.has_permissions(administrator=True)
    async def dbstatus(self, interaction: discord.Interaction):
        import asyncio  # imported here, not inside one branch: every path needs it

        await interaction.response.defer(ephemeral=True)

        # --- local database stats (off the event loop: see _local_table_counts) ---
        db_path = db.DB_PATH
        tables, db_size_kb, wal_kb, integrity = await asyncio.to_thread(
            _local_table_counts, db_path
        )

        # --- Supabase / cloud status ---
        from utils.cloud_sync import (
            SUPABASE_URL,
            SUPABASE_KEY,
            _last_sync_hash,
            _remote_checked,
            _restored_from,
            _restored_counts,
            _storage_list,
            BUCKET,
            upload_refusal_reason,
            last_upload_ts,
        )
        supa_ok = bool(SUPABASE_URL and SUPABASE_KEY)
        cloud_files: list[dict] = []
        if supa_ok:
            cloud_files = await asyncio.to_thread(_storage_list)

        # --- build the embed ---
        embed = discord.Embed(
            title="🗄️ Database Status",
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name="DaCat — System",
            icon_url=interaction.guild.me.display_avatar.url,
        )

        # DB size. Integrity and the WAL are here because they are the two things a
        # row count cannot tell you: a corrupt restore and an un-checkpointed WAL
        # both look perfectly healthy as a count of rows.
        embed.add_field(
            name="💾 Local Database",
            value=(
                f"`{db_size_kb:.1f} KB` on disk\n"
                f"{len(tables)} table(s)\n"
                f"WAL: `{wal_kb:.1f} KB`\n"
                f"Integrity: {'✅ ok' if integrity == 'ok' else f'⚠️ {integrity[:60]}'}"
            ),
            inline=True,
        )

        # Cloud connection
        if supa_ok:
            current = next((f for f in cloud_files if f["name"] == "bot.db"), None)
            snapshots = [
                f for f in cloud_files
                if f["name"].startswith("bot-db-") and f["name"].endswith(".db")
            ]
            if current:
                size_b = current.get("metadata", {}).get("size", 0)
                embed.add_field(
                    name="☁️ Supabase Backup",
                    value=(
                        f"✅ Connected to `{BUCKET}`\n"
                        f"Current: `{size_b / 1024:.1f} KB`\n"
                        f"Snapshots: **{len(snapshots)}**"
                    ),
                    inline=True,
                )
            else:
                embed.add_field(
                    name="☁️ Supabase Backup",
                    value="⚠️ Connected but no `bot.db` found",
                    inline=True,
                )
        else:
            embed.add_field(
                name="☁️ Supabase Backup",
                value="❌ Not configured (env vars missing)",
                inline=True,
            )

        # Sync status. The refusal reason is the *whole* decision — an upload
        # silently not happening is how a backup turns out to be days old, so
        # this card is the place a human can see why.
        #
        # It talks to Supabase and reads SQLite, so it runs in a worker thread:
        # called inline it holds the event loop — and therefore every other
        # command, and the gateway heartbeat — for the length of a round trip.
        refusal = await asyncio.to_thread(upload_refusal_reason, False)
        uploaded = last_upload_ts()
        if refusal:
            sync_status = f"🛑 Not uploading: {refusal}"
        elif _remote_checked and _last_sync_hash:
            sync_status = "✅ Synced"
            if uploaded:
                # When, not just whether: a sync that stopped an hour ago and one
                # that ran twelve seconds ago read identically without this.
                age = max(0, int(time.time() - uploaded))
                sync_status += f"\nLast upload: {age}s ago"
        elif _remote_checked:
            sync_status = "⚠️ Restored but not yet uploaded"
        else:
            sync_status = "⏳ Not yet checked"
        if _restored_from:
            restored_rows = sum(_restored_counts.values())
            sync_status += f"\nRestored from `{_restored_from}` ({restored_rows} rows)"
        if len(sync_status) > 1024:
            sync_status = sync_status[:1021] + "…"
        embed.add_field(
            name="🔄 Sync Status",
            value=sync_status,
            inline=True,
        )

        # Row counts — chunked into 25-row fields (Discord field limit)
        if tables:
            lines = []
            for name, count in sorted(tables.items()):
                if count < 0:
                    lines.append(f"`{name}` — ❌")
                else:
                    lines.append(f"`{name}` — **{count:,}** rows")
            # Split into chunks of 15 lines each to stay under 1024 chars
            chunk_size = 15
            for i in range(0, len(lines), chunk_size):
                chunk = lines[i : i + chunk_size]
                embed.add_field(
                    name="📊 Table Rows" + (f" ({i // chunk_size + 1})" if i else ""),
                    value="\n".join(chunk),
                    inline=False,
                )

        embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── default role ───────────────────────────────────────────────────────

    default_role_group = app_commands.Group(
        name="defaultrole",
        description="Auto-assign a role to new members when they join.",
    )

    @default_role_group.command(name="set", description="Set a role to be auto-assigned to new members.")
    @app_commands.describe(role="The role to give to every new member")
    @app_commands.checks.has_permissions(administrator=True)
    async def defaultrole_set(self, interaction: discord.Interaction, role: discord.Role):
        problem = permissions.role_grant_problem(role, interaction.guild.me, interaction.guild)
        if problem:
            return await interaction.response.send_message(
                f"❌ I cannot assign {role.mention}: {problem}", ephemeral=True,
            )
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "❌ I cannot assign a role higher than or equal to my own role."
                " Move my role above it in **Server Settings → Roles**.",
                ephemeral=True,
            )
        if not interaction.guild.me.guild_permissions.manage_roles:
            return await interaction.response.send_message(
                "❌ I need the **Manage Roles** permission to assign roles.",
                ephemeral=True,
            )
        await db.set_setting(interaction.guild.id, "default_role_id", role.id)
        embed = discord.Embed(
            title="✅ Default Role Set",
            description=(
                f"New members will now automatically receive {role.mention} when "
                f"they join.{_members_intent_warning(self.bot)}"
            ),
            color=0x57F287,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(
            text=f"Set by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        await send_card(interaction, embed=embed)

    @default_role_group.command(name="view", description="View the current default role for new members.")
    async def defaultrole_view(self, interaction: discord.Interaction):
        role_id = await db.get_setting(interaction.guild.id, "default_role_id")
        if not role_id:
            embed = discord.Embed(
                description="ℹ️ No default role is set. Use `/defaultrole set` to configure one.",
                color=0x5865F2,
            )
            return await send_card(interaction, embed=embed)
        role = interaction.guild.get_role(int(role_id))
        if role is None:
            await db.set_setting(interaction.guild.id, "default_role_id", None)
            embed = discord.Embed(
                description="⚠️ The configured default role no longer exists. Use `/defaultrole set` to pick a new one.",
                color=0xED4245,
            )
            return await send_card(interaction, embed=embed)
        embed = discord.Embed(
            title="🎭 Default Role",
            description=(
                f"New members receive {role.mention} when they join."
                f"{_members_intent_warning(self.bot)}"
            ),
            color=role.color if role.color.value else 0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Role ID", value=f"`{role.id}`", inline=True)
        embed.add_field(name="Members with this role", value=f"`{len(role.members)}`", inline=True)
        embed.set_footer(
            text=f"Requested by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        await send_card(interaction, embed=embed)

    @default_role_group.command(name="clear", description="Remove the default role (stop auto-assigning on join).")
    @app_commands.checks.has_permissions(administrator=True)
    async def defaultrole_clear(self, interaction: discord.Interaction):
        current = await db.get_setting(interaction.guild.id, "default_role_id")
        if not current:
            embed = discord.Embed(
                description="ℹ️ No default role is currently set.",
                color=0x5865F2,
            )
            return await send_card(interaction, embed=embed)
        await db.set_setting(interaction.guild.id, "default_role_id", None)
        embed = discord.Embed(
            title="✅ Default Role Cleared",
            description="New members will no longer receive a role automatically.",
            color=0x57F287,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(
            text=f"Cleared by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        await send_card(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerControl(bot))

