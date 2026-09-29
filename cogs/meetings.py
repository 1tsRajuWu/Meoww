"""Staff and team meetings — ``/meeting``, and the notices owed on them.

A meeting is a promise with three parts, and each part lives where it cannot
drift from the others: **when** (parsed once, in ``utils/meetings``), **where**
(a channel for the words, a voice channel or a sentence for the room), and **who
is told, and when** — the announcement the moment it is booked, then the notice
schedule (``1h,15m``) that fires into that same channel on the way to the start.

Three decisions shape the file, each written for a way this kind of command
usually goes wrong:

* **The card is the record.** The announcement in the channel carries the RSVP
  buttons, and the row's ``announce_message_id`` is how any later press finds
  its meeting — so a button works after a redeploy, because it carries no state
  of its own and one view registered at boot answers every card ever sent
  (the same rule ``cogs/qna.py``'s Answer button follows).
* **A notice is a row, not a timer.** ``utils/meetings`` holds the queue, the
  sweep claims each notice with one conditional ``UPDATE``, and a claim the
  process died in is put *back* rather than settled — a duplicate reminder is a
  nuisance, a missing one is the feature silently not happening. A notice that
  is too late to be worth sending is recorded as missed with the reason, which
  is the only place that fact can live: a notice that did not go out leaves
  nothing in the channel to look at.
* **The module gate is the cog's, not each callback's.** One group is one
  feature, so ``interaction_check`` refuses every subcommand at once — and that
  same check is what ``tests/test_command_scope.py`` reads to prove a command a
  server loses is one the bot would actually refuse.

The group is one of Discord's hundred command slots: six verbs live under one
``/meeting`` rather than costing six top-level names.
"""
from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import meetings, style
from utils.style import ACCENT, DANGER, SUCCESS, WARN, Panel, send_panel

#: The gate message's destination, spelled the way every other cog spells it.
DASHBOARD_URL = "https://dacatdashboard.pages.dev/dashboard"

#: The three answers, in the order the card draws them — a decision's order
#: (in, maybe, out) rather than an emoji's.
ANSWERS: tuple[tuple[str, str, discord.ButtonStyle], ...] = (
    ("yes", "I'm in", discord.ButtonStyle.success),
    ("maybe", "Maybe", discord.ButtonStyle.secondary),
    ("no", "Can't make it", discord.ButtonStyle.danger),
)
ANSWER_EMOJI = {"yes": "✅", "maybe": "❓", "no": "❌"}
ANSWER_WORD = {"yes": "I'm in", "maybe": "Maybe", "no": "Can't make it"}

#: Fixed custom ids, which is the whole persistence mechanism: discord.py
#: dispatches a press by ``(type, custom_id)``, first against the message the
#: card was sent with and then against the views registered at boot — so two
#: processes two days apart answer the same button with the same code.
BUTTON_PREFIX = "meeting_rsvp:"

#: How often the sweep looks for a notice whose moment has arrived. Half a
#: minute is a rounding error against the hour-scale of a meeting, and it keeps
#: a late notice late by no more than that.
NOTICE_INTERVAL_SECONDS = 30
#: How often finished meetings are forgotten (``meetings.HISTORY_DAYS`` old).
PRUNE_INTERVAL_SECONDS = 6 * 3600


# --------------------------------------------------------------------------- #
# The card, and the reply buttons on it
# --------------------------------------------------------------------------- #
def mentions_for(meeting: dict) -> discord.AllowedMentions:
    """Who this card may notify: the audience role, and nobody else.

    All four fields are stated because discord.py treats an *omitted* field as
    allowed — an ``AllowedMentions(roles=[x])`` would still ship with
    ``everyone=True``, and a meeting card is exactly where a server called
    ``@everyone`` would turn one reminder into a server-wide page.
    """
    role = (meeting or {}).get("audience_role_id")
    if role:
        return discord.AllowedMentions(
            users=False, roles=[int(role)], everyone=False, replied_user=False
        )
    return style.NO_PINGS


def _panel(
    *,
    title: str,
    body: str,
    fields: list[tuple[str, str]] | None = None,
    footer: str | None = None,
    accent: int = ACCENT,
    buttons: bool = False,
    persistent: bool = False,
) -> Panel:
    """One place a meeting card is assembled, so every surface draws the same one.

    ``persistent`` is for a card that stays in a channel: its view listens for
    as long as the process lives, and after a redeploy the registered
    :class:`RsvpView` picks the same buttons up by their ids.
    """
    panel = Panel(
        title=title,
        body=body,
        fields=fields or [],
        footer=footer,
        accent=accent,
        timeout=None if persistent else 180,
    )
    if buttons:
        panel.add_row(*[rsvp_button(answer) for answer, _label, _style in ANSWERS])
    return panel


def announcement_panel(
    meeting: dict, tally: dict | None = None, *, cancelled: bool = False, reason: str = ""
) -> Panel:
    """The card that announces a meeting — and, edited in place, cancels it."""
    title, body, fields, footer = meetings.announcement_text(
        meeting, tally, cancelled=cancelled, reason=reason
    )
    return _panel(
        title=title,
        body=body,
        fields=fields,
        footer=footer,
        accent=DANGER if cancelled else ACCENT,
        buttons=not cancelled,
        persistent=True,
    )


def reminder_panel(meeting: dict, offset: int, *, manual: bool = False, note: str = "") -> Panel:
    """A notice on its way out: the start, the place, the room, the agenda."""
    title, body, fields, footer = meetings.reminder_text(
        meeting, offset, manual=manual, note=note
    )
    return _panel(title=title, body=body, fields=fields, footer=footer, accent=WARN)


def _rsvp_handler(answer: str):
    """The callback behind one RSVP button — one definition, two lives.

    It is attached to the buttons the announcement is sent with *and* to the
    ones on the view registered at boot, because the message-scoped view wins
    while this process is alive and the registered one takes over after it is
    gone. Two implementations would be two tallies.
    """

    async def handle(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return await _ephemeral(
                interaction, "❌ This card belongs in a server, and this one is not in one."
            )
        checker = getattr(interaction.client, "is_module_enabled", None)
        if checker is not None and not checker(guild.id, "meetings"):
            return await _ephemeral(
                interaction,
                "⚠️ The **Meetings** system is turned off on this server, so replies are "
                f"closed. An administrator can turn it back on in the [Web Dashboard]({DASHBOARD_URL}).",
            )
        meeting = meetings.for_message(getattr(interaction.message, "id", 0) or 0)
        if meeting is None:
            return await _ephemeral(
                interaction, "❌ I can't find that meeting any more — it may have been pruned."
            )
        if str(meeting.get("status")) != meetings.STATUS_SCHEDULED:
            return await _ephemeral(
                interaction,
                f"❌ **{meeting.get('title', 'Meeting')}** is cancelled, so its replies are closed.",
            )
        user_id = getattr(interaction.user, "id", 0) or 0
        if not meetings.set_rsvp(meeting["id"], user_id, answer):
            return await _ephemeral(
                interaction, "❌ I couldn't record that — try again in a moment."
            )

        # Acknowledge first. The tally edit is a second network call on the
        # card, and an interaction token does not wait for one that can be slow;
        # the reply also tells the presser what they are now recorded as, which
        # a silent edit of a card they may have scrolled past would not.
        await _ephemeral(
            interaction,
            f"{ANSWER_EMOJI[answer]} Recorded — you're down as **{ANSWER_WORD[answer]}** "
            f"for **{meeting.get('title', 'Meeting')}**.",
        )
        try:
            await style.edit_to_panel(
                interaction.message,
                announcement_panel(meeting, meetings.tally(meeting["id"])),
                allowed_mentions=mentions_for(meeting),
            )
        except Exception as exc:  # a stale tally must not look like a dead bot
            print(
                f"[MEETINGS] Could not refresh the card for #{meeting['id']} "
                f"({type(exc).__name__}: {exc})",
                flush=True,
            )

    return handle


def rsvp_button(answer: str) -> discord.ui.Button:
    """One of the three reply buttons, with the shared callback attached."""
    label = ANSWER_WORD[answer]
    style_value = next(s for key, _label, s in ANSWERS if key == answer)
    button = discord.ui.Button(
        label=label,
        style=style_value,
        emoji=ANSWER_EMOJI[answer],
        custom_id=BUTTON_PREFIX + answer,
    )
    button.callback = _rsvp_handler(answer)
    return button


class RsvpView(discord.ui.View):
    """What the reply buttons keep doing after a redeploy.

    Registered once in the cog's constructor, with no state in it: the card's
    message id is what finds the meeting, so this one view answers every
    announcement ever posted, however old.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for answer, _label, _style in ANSWERS:
            self.add_item(rsvp_button(answer))


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
async def _ephemeral(interaction: discord.Interaction, text: str) -> None:
    """Answer privately, whichever state the interaction is in."""
    kwargs = {"content": text, "ephemeral": True}
    if interaction.response.is_done():
        return await interaction.followup.send(**kwargs)
    return await interaction.response.send_message(**kwargs)


async def _refuse(
    interaction: discord.Interaction, body: str, *, title: str = "Not done"
) -> None:
    """The refusal card: a sentence with what to do next, never a bare error."""
    await send_panel(
        interaction,
        Panel(title=title, body=body, accent=DANGER, footer="Nothing was changed."),
        ephemeral=True,
    )


def _find_meeting(guild, meeting_id: int) -> tuple[dict | None, str | None]:
    """The meeting, or the sentence that says why there isn't one.

    The guild is checked here rather than in each callback so a number typed in
    the wrong server is "not in this server" rather than a meeting another
    server can read from this one.
    """
    meeting = meetings.get(meeting_id)
    guild_id = getattr(guild, "id", None)
    if meeting is None or meeting.get("guild_id") != guild_id:
        return None, (
            f"There is no meeting `#{meeting_id}` in this server — "
            "`/meeting list` prints the numbers."
        )
    return meeting, None


def _jump_url(guild, meeting: dict) -> str:
    """A link to the card, assembled rather than fetched: three ids, no request."""
    message_id = meeting.get("announce_message_id")
    if not message_id or not guild:
        return ""
    return f"https://discord.com/channels/{guild.id}/{meeting.get('channel_id')}/{message_id}"


def _postable(guild, target) -> str | None:
    """Why this channel cannot take the card, or ``None`` if it can.

    Checked before anything is written: a meeting saved and then refused by the
    channel is a meeting in the list that has no card anywhere.
    """
    if target is None:
        return "I don't have a channel to post in — pick one with the `channel` option."
    me = getattr(guild, "me", None)
    if me is None:
        return None
    try:
        if not target.permissions_for(me).send_messages:
            return (
                f"I can't post in {target.mention} — give me **Send Messages** there, "
                "or pick another channel with the `channel` option."
            )
    except Exception:
        return None
    return None


def _channel_for(guild, meeting: dict):
    """Where this meeting's words go: the chosen channel, else the system one.

    The fallback exists because a notice for a meeting whose channel was deleted
    is still worth sending — but it is never silent about the move: the card's
    footer says the original channel is gone.
    """
    channel = None
    getter = getattr(guild, "get_channel_or_thread", None) or getattr(
        guild, "get_channel", None
    )
    if getter:
        channel = getter(meeting.get("channel_id"))
    if channel is not None:
        return channel, ""
    return getattr(guild, "system_channel", None), (
        "posted here because the original channel is gone"
        if getattr(guild, "system_channel", None) is not None
        else ""
    )


# --------------------------------------------------------------------------- #
# The cog
# --------------------------------------------------------------------------- #
class Meetings(commands.Cog):
    """``/meeting``: book it, tell people, remind them, and close it out."""

    meeting_group = app_commands.Group(
        name="meeting",
        description="Schedule a meeting and tell people about it.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # The persistent half of the RSVP buttons: the message-scoped view
        # above answers while this process lives, this answers after it stops.
        bot.add_view(RsvpView())
        self._last_prune: float = 0.0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The feature switch, read per server — one gate for the whole group."""
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "meetings"):
                await interaction.response.send_message(
                    "⚠️ The **Meetings** system is currently **turned off** on this server.\n"
                    f"An administrator can re-enable it in the [Web Dashboard]({DASHBOARD_URL}).",
                    ephemeral=True,
                )
                return False
        return True

    # ── creating ────────────────────────────────────────────────────────── #

    @meeting_group.command(
        name="create",
        description="Schedule a meeting: when, where, who to tell, and the notices.",
    )
    @app_commands.describe(
        title="What the meeting is called.",
        when="When it starts: `tomorrow 18:00`, `in 2 hours`, `2026-10-03 18:00`. UTC unless you add `+2`.",
        channel="Where the announcement and notices are posted. Defaults to here.",
        voice="The voice channel the meeting takes place in.",
        role="Who to ping: on the announcement and on every notice.",
        where="Where it happens in words — a room, a link, a stage.",
        agenda="What it is about — what people read before it starts.",
        notices="When to remind people: `1h,15m`, `1d,1h`, or `none`. Units: m, h, d, w.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def meeting_create(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 100],
        when: str,
        channel: discord.TextChannel | None = None,
        voice: discord.VoiceChannel | None = None,
        role: discord.Role | None = None,
        where: str | None = None,
        agenda: app_commands.Range[str, 1, 900] | None = None,
        notices: str | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            return

        now = int(time.time())
        starts_at, error = meetings.parse_when(when, now)
        if error:
            return await _refuse(interaction, f"❌ {error}", title="I couldn't read that time")

        offsets, error = meetings.parse_notices(
            (notices or "").strip() or meetings.DEFAULT_NOTICES
        )
        if error:
            return await _refuse(interaction, f"❌ {error}", title="I couldn't read those notices")

        target = channel or interaction.channel
        blocked = _postable(guild, target)
        if blocked:
            return await _refuse(interaction, f"❌ {blocked}", title="I can't post there")

        meeting_id = meetings.create(
            guild.id,
            title=str(title),
            starts_at=starts_at,
            channel_id=getattr(target, "id", 0),
            created_by=getattr(interaction.user, "id", 0),
            created_by_name=str(
                getattr(interaction.user, "display_name", "")
                or getattr(interaction.user, "name", "")
            ),
            agenda=str(agenda or ""),
            voice_channel_id=getattr(voice, "id", None),
            location=str(where or ""),
            audience_role_id=getattr(role, "id", None),
            notices=offsets,
            now=now,
        )
        if meeting_id is None:
            return await _refuse(
                interaction,
                "❌ I couldn't save that meeting — the database refused the write. "
                "Nothing was posted, so nothing is half-announced.",
            )

        meeting = meetings.get(meeting_id)
        if meeting is None:  # pragma: no cover — a write that cannot be read back
            meetings.forget(meeting_id)
            return await _refuse(interaction, "❌ I couldn't read the meeting back, so I dropped it.")

        # Deferred before the card goes out: the post and the receipt are two
        # network calls, and an interaction token is good for three seconds.
        await interaction.response.defer(ephemeral=True)
        try:
            message = await send_panel(
                target,
                announcement_panel(meeting, meetings.tally(meeting_id)),
                allowed_mentions=mentions_for(meeting),
            )
        except Exception as exc:
            # Roll back rather than keep a meeting nobody was ever told about:
            # `/meeting list` would promise a card the channel does not hold.
            meetings.forget(meeting_id)
            await interaction.followup.send(
                f"❌ I couldn't post the card in {getattr(target, 'mention', 'that channel')} "
                f"({type(exc).__name__}: {exc}), so I dropped the meeting — nothing was "
                "scheduled. Check my **Send Messages** permission there and try again.",
                ephemeral=True,
            )
            return
        meetings.set_message_id(meeting_id, getattr(message, "id", 0))

        await send_panel(
            interaction,
            _receipt_panel(
                meeting,
                meetings.pending_notice_rows(meeting_id),
                jump_url=getattr(message, "jump_url", "") or "",
            ),
            ephemeral=True,
        )

    # ── reading ─────────────────────────────────────────────────────────── #

    @meeting_group.command(name="list", description="The meetings this server has coming up.")
    async def meeting_list(self, interaction: discord.Interaction):
        rows = meetings.upcoming(getattr(interaction, "guild_id", 0) or 0)
        title, body, fields, footer = meetings.list_text(
            rows, getattr(getattr(interaction, "guild", None), "name", "")
        )
        await send_panel(
            interaction,
            Panel(title=title, body=body, fields=fields, footer=footer, accent=ACCENT),
            ephemeral=True,
        )

    @meeting_group.command(
        name="view",
        description="One meeting in detail: time, place, notices, and who replied.",
    )
    @app_commands.describe(id="The meeting's number — printed by /meeting list.")
    async def meeting_view(self, interaction: discord.Interaction, id: int):
        meeting, error = _find_meeting(interaction.guild, id)
        if error:
            return await _refuse(interaction, f"❌ {error}", title="No such meeting")

        tally = meetings.tally(id)
        names = await self._reply_names(interaction.guild, id, tally)
        title, body, fields, footer = meetings.detail_text(
            meeting,
            notice_rows=meetings.notice_rows(id),
            tally=tally,
            names=names,
            jump_url=_jump_url(interaction.guild, meeting),
        )
        await send_panel(
            interaction,
            Panel(title=title, body=body, fields=fields, footer=footer, accent=ACCENT),
            ephemeral=True,
        )

    async def _reply_names(self, guild, meeting_id: int, tally: dict) -> dict:
        """Display names for each answer, capped at eight — a card, not a roll call.

        Names come from the cache where they can (``display_names`` resolves
        what it must), and a name that cannot be resolved drops out rather than
        becoming an id: the counts beside them are the record.
        """
        names: dict[str, list[str]] = {}
        for answer in ("yes", "maybe", "no"):
            ids = meetings.rsvp_ids(meeting_id, answer)[:8]
            if not ids or guild is None:
                names[answer] = []
                continue
            try:
                names[answer] = list(await style.display_names(guild, ids))
            except Exception:
                names[answer] = []
        return names

    # ── changing ────────────────────────────────────────────────────────── #

    @meeting_group.command(
        name="reschedule",
        description="Move a meeting to a new time and rebuild its notices for it.",
    )
    @app_commands.describe(
        id="The meeting's number — printed by /meeting list.",
        when="The new start: `tomorrow 18:00`, `in 2 hours`, `2026-10-03 18:00`.",
        notices="New notice times, e.g. `1h,15m`. Leave empty to keep the ones it has.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def meeting_reschedule(
        self,
        interaction: discord.Interaction,
        id: int,
        when: str,
        notices: str | None = None,
    ):
        meeting, error = _find_meeting(interaction.guild, id)
        if error:
            return await _refuse(interaction, f"❌ {error}", title="No such meeting")
        if str(meeting.get("status")) != meetings.STATUS_SCHEDULED:
            return await _refuse(
                interaction,
                f"❌ **{meeting.get('title', 'Meeting')}** is cancelled — a cancelled "
                "meeting cannot be moved. Book a new one with `/meeting create`.",
                title="That meeting is cancelled",
            )

        now = int(time.time())
        starts_at, error = meetings.parse_when(when, now)
        if error:
            return await _refuse(interaction, f"❌ {error}", title="I couldn't read that time")

        if (notices or "").strip():
            offsets, error = meetings.parse_notices(notices)
            if error:
                return await _refuse(
                    interaction, f"❌ {error}", title="I couldn't read those notices"
                )
        else:
            offsets = meetings.offsets_of(meeting)

        if not meetings.reschedule(id, starts_at, offsets, now):
            return await _refuse(
                interaction,
                "❌ That meeting changed while I was moving it — run it again.",
                title="Nothing was moved",
            )

        updated = meetings.get(id) or meeting
        await interaction.response.defer(ephemeral=True)
        refreshed = await self._refresh_card(interaction.guild, updated)
        title, body, fields, footer = meetings.moved_text(
            updated,
            notice_rows=meetings.pending_notice_rows(id),
            jump_url=refreshed or _jump_url(interaction.guild, updated),
        )
        await send_panel(
            interaction,
            Panel(title=title, body=body, fields=fields, footer=footer, accent=SUCCESS),
            ephemeral=True,
        )

    @meeting_group.command(
        name="ping",
        description="Send the notice to people right now, in the meeting's channel.",
    )
    @app_commands.describe(id="The meeting's number — printed by /meeting list.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def meeting_ping(self, interaction: discord.Interaction, id: int):
        meeting, error = _find_meeting(interaction.guild, id)
        if error:
            return await _refuse(interaction, f"❌ {error}", title="No such meeting")
        if str(meeting.get("status")) != meetings.STATUS_SCHEDULED:
            return await _refuse(
                interaction,
                f"❌ **{meeting.get('title', 'Meeting')}** is cancelled, so there is "
                "nothing to tell people about.",
                title="That meeting is cancelled",
            )

        guild = interaction.guild
        channel, note = _channel_for(guild, meeting)
        blocked = _postable(guild, channel)
        if blocked:
            return await _refuse(interaction, f"❌ {blocked}", title="I can't post there")

        await interaction.response.defer(ephemeral=True)
        try:
            message = await send_panel(
                channel,
                reminder_panel(meeting, 0, manual=True, note=note),
                allowed_mentions=mentions_for(meeting),
            )
        except Exception as exc:
            await interaction.followup.send(
                f"❌ The notice did not go out ({type(exc).__name__}: {exc}). "
                "The meeting itself is unchanged.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"📣 The notice is posted in {channel.mention} — "
            f"[open it]({getattr(message, 'jump_url', '')}).\n"
            "Its scheduled notices still go out at their own times.",
            ephemeral=True,
        )

    @meeting_group.command(
        name="cancel",
        description="Cancel a meeting and stand down any notices still queued.",
    )
    @app_commands.describe(
        id="The meeting's number — printed by /meeting list.",
        reason="Why — shown on the card in the channel.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def meeting_cancel(
        self, interaction: discord.Interaction, id: int, reason: str | None = None
    ):
        meeting, error = _find_meeting(interaction.guild, id)
        if error:
            return await _refuse(interaction, f"❌ {error}", title="No such meeting")

        if not meetings.cancel(id):
            return await _refuse(
                interaction,
                f"❌ **{meeting.get('title', 'Meeting')}** is already cancelled.",
                title="Already cancelled",
            )

        cancelled = meetings.get(id) or meeting
        await interaction.response.defer(ephemeral=True)
        jump = await self._refresh_card(
            interaction.guild, cancelled, cancelled=True, reason=str(reason or "")
        )
        stood_down = sum(
            1
            for row in meetings.notice_rows(id)
            if row.get("status") == meetings.NOTICE_CANCELLED
        )
        body = f"**{cancelled.get('title', 'Meeting')}** is cancelled."
        if stood_down:
            body += f"\n{stood_down} notice{'s' if stood_down != 1 else ''} stood down."
        if jump:
            body += f"\n[The card in the channel]({jump})"
        await send_panel(
            interaction,
            Panel(title="📅 Cancelled", body=body, accent=DANGER, footer="Nothing else will be sent."),
            ephemeral=True,
        )

    async def _refresh_card(
        self, guild, meeting: dict, *, cancelled: bool = False, reason: str = ""
    ) -> str:
        """Edit the card in the channel into its new state. ``""`` if it could not.

        Every failure path returns the empty string rather than raising: the
        command's own answer is what the caller is waiting for, and a card that
        cannot be edited is a note in the log, not a refused command.
        """
        message_id = meeting.get("announce_message_id")
        if not message_id or guild is None:
            return ""
        getter = getattr(guild, "get_channel_or_thread", None) or getattr(
            guild, "get_channel", None
        )
        channel = getter(meeting.get("channel_id")) if getter else None
        if channel is None:
            print(
                f"[MEETINGS] The card for #{meeting.get('id')} is in a channel that "
                "is gone; it was not updated.",
                flush=True,
            )
            return ""
        try:
            message = await channel.fetch_message(int(message_id))
        except Exception as exc:
            print(
                f"[MEETINGS] Could not fetch the card for #{meeting.get('id')} "
                f"({type(exc).__name__}: {exc}).",
                flush=True,
            )
            return ""
        try:
            await style.edit_to_panel(
                message,
                announcement_panel(
                    meeting, meetings.tally(meeting.get("id") or 0),
                    cancelled=cancelled, reason=reason,
                ),
                allowed_mentions=mentions_for(meeting),
            )
            return getattr(message, "jump_url", "") or ""
        except Exception as exc:
            print(
                f"[MEETINGS] Could not edit the card for #{meeting.get('id')} "
                f"({type(exc).__name__}: {exc}).",
                flush=True,
            )
            return ""

    # ── the notices ─────────────────────────────────────────────────────── #

    @tasks.loop(seconds=NOTICE_INTERVAL_SECONDS)
    async def notices(self):
        """Send whatever is due, in one bounded sweep. Never raises.

        A loop that dies is a feature that stops happening while everything
        still looks configured, so the sweep catches its own failures and keeps
        its next tick.
        """
        try:
            await self._notice_sweep()
        except Exception as exc:
            print(
                f"[MEETINGS] Notice sweep failed ({type(exc).__name__}: {exc})",
                flush=True,
            )

    @notices.before_loop
    async def _before_notices(self):
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:  # a stand-in bot in the test-suite never becomes ready
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        """Start the sweep, once the bot can actually reach a channel.

        Started here rather than in ``__init__`` for the reason ``cogs/tracker``
        starts its own flush there: the loop waits for readiness anyway, and a
        listener survives every reconnect while ``__init__`` runs once. The guard
        is what keeps ``on_ready``'s every firing from spawning a second sweep.
        """
        if not self.notices.is_running():
            self.notices.start()

    async def _notice_sweep(self) -> None:
        now = int(time.time())

        for stale in meetings.reclaim_stale(now):
            print(
                f"[MEETINGS] Notice {stale} was interrupted mid-send and is going out again.",
                flush=True,
            )

        for row in meetings.due_notices(now):
            notice_id = row["notice_id"]
            if not meetings.claim_notice(notice_id, now):
                continue  # another sweep took it; the claim was the answer
            if str(row.get("status")) != meetings.STATUS_SCHEDULED:
                meetings.mark_notice(
                    notice_id, meetings.NOTICE_CANCELLED, "the meeting is not running", now
                )
                continue
            if now - int(row["due_at"]) > meetings.STALE_NOTICE_SECONDS:
                # Too late to be a reminder. Saying so in the row is the only
                # place this can live: a notice that never went out leaves
                # nothing in the channel to look at.
                meetings.mark_notice(
                    notice_id,
                    meetings.NOTICE_MISSED,
                    "over half an hour late before it was picked up",
                    now,
                )
                print(
                    f"[MEETINGS] Notice {notice_id} for '{row['title']}' was missed — "
                    "the bot was away when it was due.",
                    flush=True,
                )
                continue
            ok, note = await self._send_notice(row)
            meetings.mark_notice(
                notice_id,
                meetings.NOTICE_SENT if ok else meetings.NOTICE_MISSED,
                "" if ok else note,
                now,
            )
            print(
                f"[MEETINGS] Notice {notice_id} for '{row['title']}': "
                + ("sent" if ok else f"not sent — {note}"),
                flush=True,
            )

        if now - self._last_prune >= PRUNE_INTERVAL_SECONDS:
            self._last_prune = now
            pruned = meetings.prune(now)
            if pruned:
                print(
                    f"[MEETINGS] Pruned {pruned} finished meeting(s) older than "
                    f"{meetings.HISTORY_DAYS} days.",
                    flush=True,
                )

    async def _send_notice(self, row: dict) -> tuple[bool, str]:
        """Post one notice. ``(sent, why not)`` — and it never raises.

        Every refusal is a sentence rather than an exception because the caller
        writes that sentence into the row: "the notice did not go out because
        the channel is gone" is the only record this event will ever have.
        """
        guild = self.bot.get_guild(row.get("guild_id") or 0)
        if guild is None:
            return False, "I am not in that server any more"
        checker = getattr(self.bot, "is_module_enabled", None)
        if checker is not None and not checker(guild.id, "meetings"):
            return False, "the Meetings system was switched off"

        channel, note = _channel_for(guild, row)
        if channel is None:
            return False, "the notice channel is gone and this server has no system channel"
        blocked = _postable(guild, channel)
        if blocked:
            return False, blocked
        try:
            await send_panel(
                channel,
                reminder_panel(row, int(row.get("offset_seconds") or 0), note=note),
                allowed_mentions=mentions_for(row),
            )
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, note

    def cog_unload(self):
        self.notices.cancel()


def _receipt_panel(meeting: dict, notice_rows, *, jump_url: str = "") -> Panel:
    """The `/meeting create` confirmation, built from the rows actually written."""
    title, body, fields, footer = meetings.receipt_text(
        meeting, notice_rows, jump_url=jump_url
    )
    return Panel(title=title, body=body, fields=fields, footer=footer, accent=SUCCESS)


async def setup(bot: commands.Bot):
    await bot.add_cog(Meetings(bot))
