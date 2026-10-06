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
# The private draft — the same booking, filled in on a card only one person sees
# --------------------------------------------------------------------------- #
#: The times the draft offers without typing. Chosen rather than typed because
#: the four answers people actually want are all clock arithmetic, and a select
#: cannot be mistyped. The fifth option opens the modal for everything else.
WHEN_CHOICES: tuple[tuple[str, str], ...] = (
    ("In an hour", "in 1 hour"),
    ("In three hours", "in 3 hours"),
    ("Tomorrow at 18:00", "tomorrow 18:00"),
    ("In a week", "in 7 days"),
)
#: The value the When select sends for "let me type it" — never a time.
CUSTOM_WHEN = "__custom__"
#: The notice schedules offered, as ``(what it reads as, what it parses as)``.
NOTICE_CHOICES: tuple[tuple[str, str], ...] = (
    ("An hour and 15 minutes before", "1h,15m"),
    ("A day and an hour before", "1d,1h"),
    ("An hour before", "1h"),
    ("No reminders", "none"),
)
#: How long a draft stays answerable. Discord drops a card's buttons after its
#: view times out, and a draft nobody finished is not worth an hour-long socket.
DRAFT_TIMEOUT_SECONDS = 15 * 60


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


def _channel_of(guild, channel_id) -> object | None:
    """A channel by id, through whichever getter this guild happens to have.

    ``None`` for a missing id as well as a missing channel: a draft may not have
    picked one yet, and "nothing chosen" is not an error to report.
    """
    if not channel_id:
        return None
    getter = getattr(guild, "get_channel_or_thread", None) or getattr(
        guild, "get_channel", None
    )
    return getter(channel_id) if getter else None


def _role_of(guild, role_id) -> object | None:
    """A role by id, or ``None`` — the draft's audience, which is optional."""
    getter = getattr(guild, "get_role", None)
    return getter(role_id) if getter and role_id else None


def _channel_for(guild, meeting: dict):
    """Where this meeting's words go: the chosen channel, else the system one.

    The fallback exists because a notice for a meeting whose channel was deleted
    is still worth sending — but it is never silent about the move: the card's
    footer says the original channel is gone.
    """
    channel = _channel_of(guild, meeting.get("channel_id"))
    if channel is not None:
        return channel, ""
    return getattr(guild, "system_channel", None), (
        "posted here because the original channel is gone"
        if getattr(guild, "system_channel", None) is not None
        else ""
    )


def _write_refusal() -> str:
    """What to tell somebody whose meeting the database would not take.

    The layer below keeps the database's own words for the failure, and they are
    appended here when there are any: for three days this card said "the
    database refused the write" while the sentence that named the missing table
    sat in a log nobody reads. An operator's own card is the one place a storage
    error may be spelled out.
    """
    reason = meetings.last_write_error()
    detail = f" (`{reason}`)" if reason else ""
    return (
        f"❌ I couldn't save that meeting — the database refused the write{detail}. "
        "Nothing was posted, so nothing is half-announced."
    )


def _private_answer(interaction: discord.Interaction):
    """A callable that answers the person who asked, privately.

    The option-filled command's answers are follow-ups on its own ephemeral
    reply; the draft's answers replace the draft message itself, which is the
    whole point of a draft being one message. Both shapes arrive here as one
    callable so :meth:`Meetings._book` never has to know which it is in.
    """

    async def answer(payload) -> None:
        if isinstance(payload, str):
            return await interaction.followup.send(payload, ephemeral=True)
        return await send_panel(interaction, payload, ephemeral=True)

    return answer


def _draft_answer(interaction: discord.Interaction):
    """The draft's answer: the private message becomes the receipt."""

    async def answer(payload) -> None:
        if isinstance(payload, str):
            return await style.edit_card(interaction, content=payload)
        return await style.edit_card(interaction, content=None, view=payload)

    return answer


class Draft:
    """A meeting that is not one yet — the fields, and nothing else.

    Deliberately not a database row. A half-filled meeting is not a meeting: it
    has no card anybody could answer and no time worth reminding anybody about,
    so it lives in this object for as long as the private card lives, and the
    row is written once, complete, by the same code the plain command uses.
    """

    def __init__(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int = 0,
        voice_id: int | None = None,
        role_id: int | None = None,
        title: str = "",
        where: str = "",
        agenda: str = "",
        notices=(3600, 900),
        notice_label: str = "",
    ) -> None:
        self.guild_id = guild_id
        self.user_id = user_id
        self.channel_id = int(channel_id or 0)
        self.voice_id = voice_id
        self.role_id = role_id
        self.title = str(title or "")
        self.where = str(where or "")
        self.agenda = str(agenda or "")
        self.notices = tuple(int(o) for o in notices)
        # Words rather than syntax: the card asks in sentences ("An hour and 15
        # minutes before") and every other field answers in them too, so the
        # default schedule reads the same way as a chosen one instead of
        # showing the reader `1h,15m` and leaving them to decode it.
        self.notice_label = str(notice_label or _label_for(meetings.DEFAULT_NOTICES))
        self.starts_at: int | None = None
        self.when_label = ""
        #: The meeting this draft became, once it became one. A second press on
        #: **Book it** — two interactions are in flight the moment somebody
        #: double-clicks, and each one gets its own token — must not be a second
        #: meeting with the same title, in the same channel, at the same minute.
        self.booked: int | None = None

    def ready(self) -> bool:
        """True once there is a name and a moment — the two things a card needs."""
        return bool(self.title.strip()) and self.starts_at is not None

    def when_line(self) -> str:
        if self.starts_at is None:
            return "not picked yet"
        label = f"{self.when_label} · " if self.when_label else ""
        return (
            f"{label}{meetings.stamp(self.starts_at, 'F')} "
            f"({meetings.stamp(self.starts_at, 'R')})"
        )

    def notice_line(self) -> str:
        if not self.notices:
            return "none — people are told once, when it is booked"
        return f"{self.notice_label} ({len(self.notices)} reminder{'s' if len(self.notices) != 1 else ''})"


def _picked(interaction: discord.Interaction, fallback: str = "") -> str:
    """The value somebody just chose, off the interaction it arrived on.

    ``interaction.data["values"]`` is where Discord puts a select's answer, and
    it is the same list the library itself reads — so a component's choice can
    be asserted without a gateway.
    """
    data = getattr(interaction, "data", None) or {}
    values = data.get("values") or []
    return str(values[0]) if values else fallback


def _draft_panel_body(draft: Draft, warning: str = "") -> str:
    """The draft's own words: what is filled in, and what is still open."""
    lines = [f"**{draft.title.strip() or '_No name yet_'}**"]
    if draft.where.strip():
        lines.append(f"📍 {draft.where.strip()}")
    if draft.agenda.strip():
        lines.append(f"📝 {draft.agenda.strip()}")
    if warning:
        lines.append("")
        lines.append(warning)
    return "\n".join(lines)


class DraftDetails(discord.ui.Modal, title="The words on the card"):
    """The three things a select cannot ask for, on one form.

    A modal is the only free-text surface Discord gives a component, so the
    name, the place in words and the agenda all arrive here — and the time, for
    the one case the presets do not cover.
    """

    def __init__(self, panel: "DraftPanel") -> None:
        super().__init__()
        self.panel = panel
        self.name = discord.ui.TextInput(
            label="What is it called?",
            required=False,
            max_length=100,
            placeholder="Sprint planning",
            default=panel.draft.title[:100] or None,
        )
        self.when = discord.ui.TextInput(
            label="Time (fills in the choice above)",
            required=False,
            max_length=60,
            placeholder="tomorrow 18:00, in 2 hours, friday 19:00",
            default=(
                panel.draft.when_label
                if panel.draft.starts_at is not None
                and panel.draft.when_label not in [label for label, _v in WHEN_CHOICES]
                else None
            ),
        )
        self.where = discord.ui.TextInput(
            label="Where",
            required=False,
            max_length=200,
            placeholder="Room 2 · the stage · a link",
            default=panel.draft.where[:200] or None,
        )
        self.agenda = discord.ui.TextInput(
            label="What it is about",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=900,
            placeholder="What people should read before it starts",
            default=panel.draft.agenda[:900] or None,
        )
        self.add_item(self.name)
        self.add_item(self.when)
        self.add_item(self.where)
        self.add_item(self.agenda)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        draft = self.panel.draft
        warning = ""
        draft.title = str(self.name.value or "").strip()[:100]
        draft.where = str(self.where.value or "").strip()[:200]
        draft.agenda = str(self.agenda.value or "").strip()[:900]
        typed = str(self.when.value or "").strip()
        if typed:
            starts_at, error = meetings.parse_when(typed, int(time.time()))
            if error:
                warning = f"⚠️ I couldn't read `{typed}` as a time — {error}"
            else:
                draft.starts_at = starts_at
                draft.when_label = typed
        await self.panel.redraw(interaction, warning=warning)


class DraftPanel(Panel):
    """The private draft card: one ephemeral message only its owner can drive.

    The draft is *state on the message* rather than a dictionary on the bot: it
    exists for as long as the card does, it dies with it, and nothing has to be
    cleaned up or expired by hand. ``interaction_check`` is the second half of
    "only I can see it" — an ephemeral message is already private, and this
    refuses anybody else who somehow reaches its components.
    """

    def __init__(self, cog, draft: Draft, *, warning: str = "") -> None:
        super().__init__(
            title="📅 Book it — a private draft",
            body=_draft_panel_body(draft, warning),
            fields=[
                ("When", draft.when_line()),
                ("Notices", draft.notice_line()),
                (
                    "Announce in",
                    f"<#{draft.channel_id}>" if draft.channel_id else "this channel",
                ),
                (
                    "Who to ping",
                    f"<@&{draft.role_id}>" if draft.role_id else "nobody is pinged",
                ),
            ],
            footer=(
                "Only you can see this draft, and nothing is scheduled until you press "
                "**Book it**. It closes after 15 minutes."
            ),
            accent=ACCENT,
            timeout=DRAFT_TIMEOUT_SECONDS,
        )
        self.cog = cog
        self.draft = draft

        when = discord.ui.Select(
            placeholder="When does it start?",
            options=[
                discord.SelectOption(label=label, value=value)
                for label, value in WHEN_CHOICES
            ]
            + [
                discord.SelectOption(
                    label="Type a time…",
                    value=CUSTOM_WHEN,
                    description="tomorrow 18:00 · in 2 hours · friday 19:00",
                )
            ],
        )
        when.callback = self._chose_when
        notices = discord.ui.Select(
            placeholder="Remind people when?",
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    default=tuple(draft.notices) == tuple(_offsets_for(value)),
                )
                for label, value in NOTICE_CHOICES
            ],
        )
        notices.callback = self._chose_notices
        channel = discord.ui.ChannelSelect(
            placeholder="Where to announce it (default: this channel)",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            default_values=(
                [discord.Object(id=draft.channel_id)] if draft.channel_id else None
            ),
        )
        channel.callback = self._chose_channel
        role = discord.ui.RoleSelect(
            placeholder="Who to ping (optional)",
            min_values=0,
            max_values=1,
            default_values=[discord.Object(id=draft.role_id)] if draft.role_id else None,
        )
        role.callback = self._chose_role
        details = discord.ui.Button(
            label="Title, place, agenda…", style=discord.ButtonStyle.secondary, emoji="📝"
        )
        details.callback = self._open_details
        book = discord.ui.Button(
            label="Book it", style=discord.ButtonStyle.success, emoji="📅",
            disabled=not draft.ready(),
        )
        book.callback = self._book
        drop = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️"
        )
        drop.callback = self._drop

        self.add_row(when, notices, channel, role)
        self.add_row(details, book, drop)

    # ── the guard ───────────────────────────────────────────────────────── #

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the person who opened the draft may use it."""
        if getattr(interaction.user, "id", 0) != self.draft.user_id:
            await _ephemeral(
                interaction,
                "❌ That draft belongs to somebody else — run `/meeting create` to "
                "start your own.",
            )
            return False
        return True

    async def redraw(self, interaction: discord.Interaction, *, warning: str = "") -> None:
        """Show the draft again, in place, carrying whatever just changed."""
        await style.edit_card(
            interaction, content=None, view=DraftPanel(self.cog, self.draft, warning=warning)
        )

    # ── the choices ──────────────────────────────────────────────────────── #

    async def _chose_when(self, interaction: discord.Interaction) -> None:
        value = _picked(interaction)
        if value == CUSTOM_WHEN or not value:
            return await interaction.response.send_modal(DraftDetails(self))
        starts_at, error = meetings.parse_when(value, int(time.time()))
        if error:
            return await self.redraw(interaction, warning=f"⚠️ {error}")
        self.draft.starts_at = starts_at
        self.draft.when_label = _label_for(value)
        await self.redraw(interaction)

    async def _chose_notices(self, interaction: discord.Interaction) -> None:
        value = _picked(interaction)
        offsets, error = meetings.parse_notices(value or meetings.DEFAULT_NOTICES)
        if error:
            return await self.redraw(interaction, warning=f"⚠️ {error}")
        self.draft.notices = tuple(offsets)
        self.draft.notice_label = _label_for(value)
        await self.redraw(interaction)

    async def _chose_channel(self, interaction: discord.Interaction) -> None:
        chosen = _picked(interaction)
        if chosen:
            self.draft.channel_id = int(chosen)
        await self.redraw(interaction)

    async def _chose_role(self, interaction: discord.Interaction) -> None:
        chosen = _picked(interaction)
        self.draft.role_id = int(chosen) if chosen else None
        await self.redraw(interaction)

    async def _open_details(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(DraftDetails(self))

    # ── the two ends ─────────────────────────────────────────────────────── #

    async def _book(self, interaction: discord.Interaction) -> None:
        """Write it, post it, and answer — through the same code as `/meeting create`."""
        draft = self.draft
        now = int(time.time())
        if draft.booked is not None:
            return await self.redraw(
                interaction,
                warning=(
                    f"⚠️ This draft is already booked as meeting `#{draft.booked}` — "
                    f"`/meeting view {draft.booked}` shows it."
                ),
            )
        if not draft.title.strip():
            return await self.redraw(
                interaction, warning="⚠️ Give it a name first — press **Title, place, agenda…**."
            )
        if draft.starts_at is None:
            return await self.redraw(interaction, warning="⚠️ Pick the time first.")
        if draft.starts_at - now < meetings.MIN_LEAD_SECONDS:
            return await self.redraw(
                interaction,
                warning="⚠️ That start time passed while this draft was open — pick a new one.",
            )

        guild = interaction.guild
        target = _channel_of(guild, draft.channel_id) or interaction.channel
        blocked = _postable(guild, target)
        if blocked:
            return await self.redraw(interaction, warning=f"⚠️ {blocked}")

        meeting = await self.cog._book(
            interaction,
            target=target,
            title=draft.title,
            starts_at=draft.starts_at,
            offsets=draft.notices,
            voice=_channel_of(guild, draft.voice_id),
            role=_role_of(guild, draft.role_id),
            where=draft.where,
            agenda=draft.agenda,
            answer=_draft_answer(interaction),
        )
        if meeting is not None:
            draft.booked = int(meeting["id"])

    async def _drop(self, interaction: discord.Interaction) -> None:
        """Throw the draft away. Nothing was written, so there is nothing to undo."""
        await style.edit_card(
            interaction,
            content=None,
            view=Panel(
                title="✖️ Draft closed",
                body=(
                    "Nothing was booked — a draft is not a meeting until you press "
                    "**Book it**. `/meeting create` starts a new one."
                ),
                accent=DANGER,
                footer="Nothing was changed.",
            ),
        )


def _label_for(value: str) -> str:
    """The words a choice was offered in, for the card to echo back."""
    for label, candidate in (*WHEN_CHOICES, *NOTICE_CHOICES):
        if candidate == value:
            return label
    return value


def _offsets_for(value: str) -> list[int]:
    """The offsets a notice choice stands for, or ``[]`` for ``none``."""
    offsets, _error = meetings.parse_notices(value)
    return list(offsets or [])


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
        description="Book a meeting — a private card first, announced once you press Book it.",
    )
    @app_commands.describe(
        title="What the meeting is called — it lands on the private card.",
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
        title: app_commands.Range[str, 1, 100] | None = None,
        when: str | None = None,
        channel: discord.TextChannel | None = None,
        voice: discord.VoiceChannel | None = None,
        role: discord.Role | None = None,
        where: str | None = None,
        agenda: app_commands.Range[str, 1, 900] | None = None,
        notices: str | None = None,
    ):
        """One way in, and it is the private card.

        Every option here *seeds the draft* rather than booking it: the ephemeral
        card is always the first surface, only the person who ran the command can
        see it, and nothing is written — no row, no announcement — until **Book
        it** is pressed. Passing the whole meeting on the command line is not a
        second path, it is a draft that arrives ready, so the promises live in one
        place (``_book``) instead of two that can drift.
        """
        guild = interaction.guild
        if guild is None:
            return

        await self._open_draft(
            interaction, title=title, when=when, channel=channel, voice=voice,
            role=role, where=where, agenda=agenda, notices=notices,
        )

    # ── the draft, and the one write both ways in go through ────────────── #

    async def _open_draft(
        self,
        interaction: discord.Interaction,
        *,
        title=None,
        when=None,
        channel=None,
        voice=None,
        role=None,
        where=None,
        agenda=None,
        notices=None,
    ) -> None:
        """Start the private draft, seeded with whatever was already given.

        The ephemeral card is the whole of the state: it is only ever visible to
        the person who ran the command, and it is edited in place as they pick,
        so the "temp" never becomes a second message, a thread or a row.
        """
        guild = interaction.guild
        here = getattr(interaction, "channel", None)
        draft = Draft(
            guild_id=guild.id,
            user_id=getattr(interaction.user, "id", 0),
            channel_id=getattr(channel, "id", None) or getattr(here, "id", 0),
            voice_id=getattr(voice, "id", None),
            role_id=getattr(role, "id", None),
            title=str(title or ""),
            where=str(where or ""),
            agenda=str(agenda or ""),
        )
        warnings: list[str] = []
        if when:
            starts_at, error = meetings.parse_when(when, int(time.time()))
            if error:
                warnings.append(f"⚠️ I couldn't read `{when}` as a time — {error}")
            else:
                draft.starts_at = starts_at
                draft.when_label = str(when)
        if notices:
            offsets, error = meetings.parse_notices(notices)
            if error:
                warnings.append(f"⚠️ I couldn't read `{notices}` as notices — {error}")
            else:
                draft.notices = tuple(offsets)
                draft.notice_label = str(notices)
        await send_panel(
            interaction,
            DraftPanel(self, draft, warning="\n".join(warnings)),
            ephemeral=True,
        )

    async def _book(
        self,
        interaction: discord.Interaction,
        *,
        target,
        title: str,
        starts_at: int,
        offsets,
        voice=None,
        role=None,
        where: str = "",
        agenda: str = "",
        answer=None,
    ) -> dict | None:
        """Write the meeting, post the card, and answer the person who asked.

        One implementation for both doors into this feature — the option-filled
        command and the private draft — because the promises are identical and
        two copies would drift apart on exactly the parts that matter: the row,
        the rollback when the card will not post, and a receipt read back from
        the rows that were actually written rather than from the request.
        """
        answer = answer or _private_answer(interaction)
        meeting_id = meetings.create(
            interaction.guild.id,
            title=str(title)[:100],
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
            now=int(time.time()),
        )
        if meeting_id is None:
            await answer(_write_refusal())
            return None

        meeting = meetings.get(meeting_id)
        if meeting is None:  # pragma: no cover — a write that cannot be read back
            meetings.forget(meeting_id)
            await answer("❌ I couldn't read the meeting back, so I dropped it.")
            return None

        # Deferred before the card goes out: the post and the receipt are two
        # network calls, and an interaction token is good for three seconds.
        if not interaction.response.is_done():
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
            await answer(
                f"❌ I couldn't post the card in {getattr(target, 'mention', 'that channel')} "
                f"({type(exc).__name__}: {exc}), so I dropped the meeting — nothing was "
                "scheduled. Check my **Send Messages** permission there and try again."
            )
            return None
        meetings.set_message_id(meeting_id, getattr(message, "id", 0))

        await answer(
            _receipt_panel(
                meeting,
                meetings.pending_notice_rows(meeting_id),
                jump_url=getattr(message, "jump_url", "") or "",
            )
        )
        return meeting

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
