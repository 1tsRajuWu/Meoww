"""The `/alone` surface: the commands, the sweep and the reconcile loop.

`utils/alonetime.py` holds the rules and the engine — what a lock is, who may
join a room, what the ledger restores, and the two things this feature keeps in
memory and never writes down — and this holds the half that needs a gateway. The
split is `cogs/company.py`'s, for the reason that cog's docstring gives: every
rule worth arguing about is testable without Discord, and the rest can only be
tested by using it.

Four things here are decisions rather than plumbing:

* **The sweep is one listener over every voice event, not a loop.** Discord tells
  the bot the moment somebody arrives somewhere, so an intruder is moved back out
  within a heartbeat of getting in — and nothing polls voice at all, which is what
  keeps a feature that touches every room in a server cheap enough to leave on.
* **A notice does not go into the locked room.** A locked room's chat is the one
  place its holder is least likely to be reading, and a V2 card dropped there is
  gone the moment the conversation moves; the server's own answer wins instead
  (its `#bot-commands`, then `#general`, then the room itself — see
  :meth:`AloneTime._notice_channel`). Every automated sentence this cog posts goes
  through that one resolver.
* **The holder's reason is never sent anywhere but back to them.** It is read from
  the in-memory session store onto the holder's own ephemeral card and onto
  `/alone status` when the holder runs it, and it appears on no other surface:
  not the public notice, not the moderation log, not `/alone list`, not the
  dashboard. The card says so where the holder reads it, because a privacy
  promise a person cannot see is a promise they cannot rely on.
* **A join request is delivered, not published.** The holder is DMed the request
  with two buttons; only their own press changes the whitelist. A DM that Discord
  refuses falls back to the notice channel *and says that is why it is there*,
  because a request silently marked as "delivered" when nobody can read it is the
  failure this whole path exists to avoid.

The cards are built from `utils/style.py`'s type scale and nothing else: one
heading, small labels, body text, one accent, and no text emoji anywhere. The
lock mark on a room's *name* stays (`utils/alonetime.LOCK_MARK`) because it is
how a member reads a room as locked before they try to join it, but no card,
button or notice this cog draws depends on an emoji to say what it means.
"""
from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import alonetime, bot_channels, db, modlog, style
from utils.company import is_stage

#: How often a lock's reason to exist is re-checked. Nothing here is urgent — the
#: sweep handles arrivals instantly — and a minute is long enough that the cost of
#: the whole feature on an idle server is one pass and no writes.
RECONCILE_SECONDS = 60

#: How long the same member must wait before locking another room, when a server
#: has not said otherwise in its own settings. A lock, a release and a lock again
#: is a loop somebody can build by accident, and the dashboard's own setting is
#: what a server tunes rather than this.
DEFAULT_COOLDOWN_SECONDS = 60

#: How many rooms one `/alone list` prints before it says how many are left. Ten
#: rows are readable in a card; a hundred are not, and a server with a hundred
#: locked rooms needs the dashboard, not an embed.
LIST_LIMIT = 10

#: How many people one whitelist pick may add. Five is what a session needs and
#: what a picker carries without becoming a scroll; a longer list is one more
#: pick away.
WHITELIST_LIMIT = 5

#: The controls, by ``custom_id``. Fixed strings rather than per-message ids,
#: because a card outlives the process that posted it: one registration in the
#: cog's constructor answers every card the bot has ever sent — the rule
#: ``cogs/meetings.py::RsvpView`` states for its own buttons.
WHITELIST_ID = "alone:whitelist"
RENAME_ID = "alone:rename"
RELEASE_ID = "alone:release"
ASK_ID = "alone:ask"
ALLOW_ID = "alone:allow"
DENY_ID = "alone:deny"


def _wait_words(seconds: int) -> str:
    """A cooldown as a person reads it: ``4m 12s``, ``45s``, ``5m``.

    ``alonetime.duration_words`` rounds to minutes because it describes how long
    a room has been held; a cooldown a person is watching counts down in seconds,
    and telling somebody "4m" when they have 4m 59s left is how a wait feels
    longer than it is.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}m {rest}s" if rest else f"{minutes}m"


class RenameModal(discord.ui.Modal):
    """What the Rename button opens: one box, one name.

    A modal rather than a slash argument because the holder has already run their
    command and is looking at the card; sending them back to type another one
    would be this feature making its own bookkeeping their errand.
    """

    room_name = discord.ui.TextInput(
        label="Name this room for your session",
        placeholder="Study — I add the lock mark myself",
        max_length=80,
        required=True,
    )

    def __init__(self, cog: "AloneTime") -> None:
        super().__init__(title="Rename the room you are holding")
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.rename_held_room(interaction, str(self.room_name.value))


class AloneHolderView(discord.ui.View):
    """The holder's controls, registered once and stateless.

    Every press has the same question — *which lock?* — and the answer is the room
    whoever pressed is **holding**, read from the ledger rather than stored on the
    button. That is what lets one registration answer a card posted before the
    last redeploy, and it is also the guard: a press from anybody who holds
    nothing is answered with a sentence rather than a change.
    """

    def __init__(self, cog: "AloneTime") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        custom_id=WHITELIST_ID,
        placeholder="Whitelist somebody for this session…",
        min_values=1,
        max_values=WHITELIST_LIMIT,
    )
    async def whitelist(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        await self.cog.whitelist_people(interaction, self._picked(interaction, select))

    @discord.ui.button(
        label="Rename room",
        style=discord.ButtonStyle.secondary,
        custom_id=RENAME_ID,
    )
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RenameModal(self.cog))

    @discord.ui.button(
        label="Release room",
        style=discord.ButtonStyle.secondary,
        custom_id=RELEASE_ID,
    )
    async def release(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.release_held_room(interaction)

    @staticmethod
    def _picked(interaction: discord.Interaction, select) -> list[int]:
        """Who was picked, from the library's reading or Discord's own payload.

        Read from the interaction rather than trusted off the select, because the
        items on a card are built from a view's ``children`` and a select only
        carries what a dispatch wrote on it — the raw payload is the one reading
        both paths have.
        """
        values = list(getattr(select, "values", None) or [])
        if not values:
            data = interaction.data if isinstance(interaction.data, dict) else {}
            values = list(data.get("values") or [])
        picked = []
        for value in values:
            try:
                picked.append(int(value))
            except (TypeError, ValueError):
                continue
        return picked


class AloneAskView(discord.ui.View):
    """The one control on a room's public notice: ask the holder to let you in.

    Stateless like the holder's controls, with one difference the handler has to
    live with: a fixed ``custom_id`` cannot carry *which* room the notice is
    about, so the answer is looked up from the message the press came from
    (``alonetime.ask_card_room``). A restart loses that map, and the press then
    says so and names ``/alone ask`` rather than guessing at a room.
    """

    def __init__(self, cog: "AloneTime") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Ask to join",
        style=discord.ButtonStyle.secondary,
        custom_id=ASK_ID,
    )
    async def ask(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        message = getattr(interaction, "message", None)
        await self.cog.ask_to_join(
            interaction, notice_message_id=getattr(message, "id", 0)
        )


class AloneAnswerView(discord.ui.View):
    """The two answers on the holder's request card, and only the holder's.

    The card is DMed to one person, but a DM is not a permission: the press is
    checked against the request's own ``holder_id``, because a forwarded
    screenshot cannot be a way into somebody's room.
    """

    def __init__(self, cog: "AloneTime") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Allow in",
        style=discord.ButtonStyle.success,
        custom_id=ALLOW_ID,
    )
    async def allow(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.answer_request(interaction, allow=True)

    @discord.ui.button(
        label="Keep out",
        style=discord.ButtonStyle.secondary,
        custom_id=DENY_ID,
    )
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.answer_request(interaction, allow=False)


def _is_stage(channel) -> bool:
    """A stage is a performance, not a room — arriving on one is somebody's event."""
    return is_stage(channel)


class AloneTime(commands.Cog):
    """Lock a voice channel to yourself, and keep it locked."""

    alone_group = app_commands.Group(
        name="alone",
        description="Lock a voice channel to yourself — nobody else can be inside it.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        #: (guild_id, member_id) -> when they last locked a room.
        self._last_lock: dict[tuple[int, int], float] = {}
        self._last_failure_at = 0.0
        # One registration for every card this feature has ever posted: the
        # controls are stateless — they resolve the lock from whoever pressed —
        # so a button on a card from before the last redeploy still answers.
        bot.add_view(AloneHolderView(self))
        bot.add_view(AloneAskView(self))
        bot.add_view(AloneAnswerView(self))
        self.reconcile.start()

    def cog_unload(self):
        self.reconcile.cancel()

    # ------------------------------------------------------------------ #
    # The gate and the small helpers
    # ------------------------------------------------------------------ #
    @property
    def _bot_id(self) -> int:
        return int(getattr(getattr(self.bot, "user", None), "id", 0) or 0)

    def _running(self, guild_id) -> bool:
        """Whether this system is on for a server.

        A bot the tests build with no module reader answers *running*: the gate is
        about a switch the bot has to be able to see, and a bot that cannot see one
        is not evidence that somebody turned it off — the same rule
        ``cogs/automod.py`` states for its own switch.
        """
        check = getattr(self.bot, "is_module_enabled", None)
        return True if check is None else bool(check(guild_id, alonetime.MODULE))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and not self._running(interaction.guild_id):
            await interaction.response.send_message(alonetime.off_sentence(), ephemeral=True)
            return False
        return True

    @staticmethod
    def _settings(guild_id):
        return alonetime.get_settings(guild_id)

    @staticmethod
    def _channel_name(guild, channel_id) -> str:
        """The room's name as its cards print it — without the lock mark.

        The mark stays on the channel where Discord's sidebar needs it; a card
        drawn by this feature already says the room is locked, and printing the
        symbol too would be the one emoji left in a typographic UI. See
        ``alonetime.plain_name``.
        """
        getter = getattr(guild, "get_channel", None)
        channel = getter(int(channel_id)) if callable(getter) else None
        return alonetime.plain_name(
            str(getattr(channel, "name", "") or f"channel {channel_id}")
        )

    @staticmethod
    def _voice_in(member):
        """The room this member is standing in, or ``None``."""
        return getattr(getattr(member, "voice", None), "channel", None)

    @staticmethod
    def _who(user) -> str:
        """The name a card and the audit log use — never a mention (see test_naming)."""
        return str(
            getattr(user, "display_name", None) or getattr(user, "name", "") or "somebody"
        )

    def _cooldown_left(self, guild_id, member_id, seconds=None) -> int:
        last = self._last_lock.get((int(guild_id), int(member_id)))
        if last is None:
            return 0
        window = int(seconds if seconds is not None else DEFAULT_COOLDOWN_SECONDS)
        if window <= 0:
            return 0
        return max(0, window - int(time.time() - last))

    def _note_lock(self, guild_id, member_id) -> None:
        self._last_lock[(int(guild_id), int(member_id))] = time.time()
        if len(self._last_lock) > 5000:
            cutoff = time.time() - max(DEFAULT_COOLDOWN_SECONDS, 60) * 2
            for key, when in list(self._last_lock.items()):
                if when < cutoff:
                    del self._last_lock[key]

    async def _held(self, guild, member):
        """The lock this member is holding, anywhere in the guild, or ``None``."""
        for lock in alonetime.cached(guild.id).values():
            if lock.holder_id and lock.holder_id == int(getattr(member, "id", 0)):
                return lock
        return None

    async def _lock_here(self, guild, member):
        """The lock on the room this member is standing in, or ``None``."""
        channel = self._voice_in(member)
        if channel is None:
            return None
        return alonetime.lock_for(guild.id, channel.id)

    async def _guest_names(self, guild, lock) -> str:
        """Who may still join, as names — the one reading every card shows."""
        names = []
        for guest_id in lock.guests:
            names.append(await style.display_name(guild, guest_id, client=self.bot))
        for role_id in lock.guest_roles:
            found = guild.get_role(role_id) if callable(getattr(guild, "get_role", None)) else None
            names.append(str(getattr(found, "name", "") or f"role {role_id}"))
        return ", ".join(names) if names else "nobody yet"

    def _capacity_now(self, guild, lock) -> int:
        """The cap one lock's whitelist needs, roles included.

        Read through the guild because a guest **role** is counted by its members:
        leaving the roles out would shrink the capacity of a room that had them,
        and the next guest would arrive to a full room.
        """
        lookup = getattr(guild, "get_role", None)
        roles = []
        if callable(lookup):
            roles = [
                found for found in (lookup(value) for value in lock.guest_roles) if found is not None
            ]
        return alonetime.capacity_for(guild, lock.guests, roles)

    def _controls(self, view_class=AloneHolderView) -> tuple:
        """A card's controls, taken from the registered view itself.

        One definition, two uses — the trick ``cogs/qna.py::reply_button`` uses:
        the items on the card are exactly the ones the class publishes, so a card
        can never carry a button the boot registration cannot answer.
        """
        return tuple(view_class(self).children)

    def _member(self, guild, member_id):
        """The member object for one id, from the cache only.

        ``fetch_member`` is deliberately not used: a request is a courtesy on a
        voice event's path, and an HTTP round trip there is the kind of thing that
        turns an arrival into a timeout. A holder the cache does not know is
        handled by the delivery path — it falls back to the notice channel.
        """
        getter = getattr(guild, "get_member", None)
        found = getter(int(member_id)) if callable(getter) else None
        return found

    # ------------------------------------------------------------------ #
    # Where a notice goes
    # ------------------------------------------------------------------ #
    @staticmethod
    def _usable(channel) -> bool:
        """Whether this something can be posted into — a text channel or a VC chat."""
        return channel is not None and callable(getattr(channel, "send", None))

    @staticmethod
    def _channels(guild):
        for attribute in ("text_channels", "channels"):
            listed = getattr(guild, attribute, None)
            if listed:
                return list(listed)
        return []

    def _channel_named(self, guild, name):
        """A channel whose name matches (decorations ignored), or ``None``."""
        wanted = str(name or "").strip().lower()
        if not wanted:
            return None
        normal = bot_channels.normalize(wanted)
        decorated = None
        for channel in self._channels(guild):
            candidate = str(getattr(channel, "name", "") or "")
            if candidate.lower() == wanted:
                return channel
            if normal and bot_channels.normalize(candidate) == normal and decorated is None:
                decorated = channel
        return decorated

    async def _notice_channel(self, guild, room=None):
        """Where an automated notice belongs, in order of whose answer wins.

        **The server's own setting**, then the house channel ``utils/bot_channels``
        binds for automated output (``#bot-commands`` and its spellings), then
        ``#general``, and only then the room itself. The order matters: a server
        that has told the bot where its house channels are should not have that
        answer second-guessed by a name, and a locked room's chat is the place its
        holder is least likely to be reading.

        ``room`` is the last resort rather than the first so a server with none of
        the above still gets its notice where the people it concerns can read it —
        and that fallback is the behaviour this feature shipped with, kept rather
        than replaced with silence.
        """
        settings = await self._settings(getattr(guild, "id", 0))
        chosen = None
        if settings.notice_channel:
            chosen = self._channel_named(guild, settings.notice_channel)
            if not self._usable(chosen):
                getter = getattr(guild, "get_channel", None)
                if callable(getter) and str(settings.notice_channel).strip().isdigit():
                    chosen = getter(int(settings.notice_channel))
        if self._usable(chosen):
            return chosen
        try:
            bound_id = await bot_channels.bound(getattr(guild, "id", 0), "commands")
        except Exception:  # noqa: BLE001 — an unreadable binding is not a lost notice
            bound_id = None
        if bound_id:
            getter = getattr(guild, "get_channel", None)
            found = getter(int(bound_id)) if callable(getter) else None
            if self._usable(found):
                return found
        for name in alonetime.NOTICE_CHANNEL_NAMES:
            found = self._channel_named(guild, name)
            if self._usable(found):
                return found
        return room

    # ------------------------------------------------------------------ #
    # The cards
    # ------------------------------------------------------------------ #
    @staticmethod
    def _layers_words(guild, settings, capacity: int) -> str:
        """What is actually holding this room, in the order it holds it.

        One sentence rather than a paragraph, because this is the card's whole
        claim: the capacity is what Discord enforces for everybody, and the
        move-out is the backstop that says so when it cannot.
        """
        said = f"Capacity **{capacity}** — Discord refuses everybody else"
        if settings.guard and not alonetime.guard_ready(guild):
            said += " · move-out is on, but I lack **Move Members**, so I cannot put anybody back out"
        elif settings.guard:
            said += " · anybody who still gets in is moved back out"
        else:
            said += " · move-out is switched off on this server"
        return said

    async def _lock_panel(
        self,
        guild,
        channel,
        lock,
        settings,
        *,
        holder,
        capacity: int,
        just_now: str = "",
        warning: str = "",
        private: bool = False,
    ) -> "style.Panel":
        """The holder's card, and the public notice, from the same builder.

        ``private`` is the one difference between them, and it is the privacy
        boundary: the holder's ephemeral copy carries their own reason back to
        them (from the in-memory session store, never a row), and the copy posted
        in the notice channel does not — because that one is read by everybody
        with access to the channel.
        """
        stays = bool(settings.keep or (lock is not None and lock.keep))
        fields = [("Room", f"**{self._channel_name(guild, getattr(channel, 'id', 0))}**")]
        if just_now:
            fields.append(("Just now", just_now))
        fields.append(("Held by", self._who(holder)))
        fields.append(("What holds it", self._layers_words(guild, settings, capacity)))
        fields.append(
            ("Whitelist", await self._guest_names(guild, lock) if lock else "nobody yet")
        )
        if private:
            reason = alonetime.reason_for(getattr(guild, "id", 0), getattr(channel, "id", 0))
            if reason:
                fields.append(
                    (
                        "Your reason",
                        f"{reason}\n"
                        "-# Shown only to you. It is not in the database, not in the "
                        "moderation log, and not on the dashboard — it goes the "
                        "moment this room opens.",
                    )
                )
        fields.append(("Lifts", "when you leave the room" if not stays else "when you release it"))
        if warning:
            fields.append(("About the name", warning))
        card = style.card(
            title="Alone Time",
            body=(
                "This room is locked to its holder. Nobody else can be inside it — "
                "not another member, not a moderator, and not an administrator — "
                "unless the holder lets them in below."
            ),
            accent=style.ACCENT,
            fields=fields,
            footer=(
                "The whitelist lasts for this session only and clears when the room "
                "empties. `/alone status` reads any of this back."
            ),
            timeout=None,
        )
        card.add_row(*self._controls())
        return card

    async def _denied(self, guild, member, lock, *, room=None):
        """The card a turned-away member finds, and the way back in.

        **Why this is public.** Discord has no ephemeral message outside an
        interaction and a voice join is not one — ephemeral replies exist only as
        an answer to something a person typed or pressed — so there is nothing the
        bot can send that only the rejected member would see. A DM would be, and
        this feature deliberately does not send one (a stranger's DMs are not this
        server's to open); the card therefore names them once, in the server's
        notice channel, and says how to ask for an exception. It is short because
        it is addressed to one person in public.

        The *Ask to join* button is recorded against the message it lands on, so
        the press knows which room it is about without a ``custom_id`` carrying
        state.
        """
        where = self._channel_name(guild, lock.channel_id)
        card = style.card(
            title="This room is on Alone Time",
            body=(
                f"**{self._who(member)}**, you were moved out of **{where}**. It is held "
                f"by **{lock.holder_label}** right now, and nobody else may be inside it — "
                "not another member, not a moderator, and not an administrator."
            ),
            accent=style.DANGER,
            fields=[
                ("Room", f"**{where}**"),
                ("Held by", lock.holder_label),
                ("Whitelist", await self._guest_names(guild, lock)),
                (
                    "What you can do",
                    "Press **Ask to join** below and the holder gets one private card "
                    "with the answer. `/alone guests` lets them add you too. It clears "
                    "when their session ends.",
                ),
            ],
            footer=(
                f"Room id {lock.channel_id} · one request per member every five minutes · "
                "this notice is public because Discord sends no private message for a voice join."
            ),
            timeout=None,
        )
        card.add_row(*self._controls(AloneAskView))
        channel = await self._notice_channel(guild, room=room)
        message = await self._post(channel, card)
        if message is not None:
            alonetime.remember_ask_card(getattr(message, "id", 0), lock.channel_id)
        return message

    async def _request_card(self, guild, lock, requester, *, public: bool = False):
        """The card the holder answers a request on.

        ``public`` is the fallback shape, posted in the notice channel when the
        holder's DMs refused the bot: the difference is one line saying so, because
        a holder finding a request in a public channel deserves to know *why* it
        is not in their DMs.
        """
        where = self._channel_name(guild, lock.channel_id)
        footer = (
            "Only you can answer this. The request lives while the bot is running, "
            "stops being answerable after half an hour, and is not written to any log."
        )
        if public:
            footer = (
                "Your DMs are closed to me, so the request is here instead. " + footer
            )
        card = style.card(
            title="Somebody asked to join your room",
            body=(
                f"**{requester.requester_label}** asked to join **{where}**, which you are "
                "holding. Answering is the only thing that changes anything — until then "
                "they still cannot get in."
            ),
            accent=style.ACCENT,
            fields=[
                ("Room", f"**{where}**"),
                ("Who is asking", requester.requester_label),
                ("Asked", f"{alonetime.duration_words(max(0, int(time.time() - requester.asked_at)))} ago"),
                (
                    "If you allow it",
                    "Their name goes on this session's whitelist and the room's "
                    "capacity rises by one seat — for this session only.",
                ),
            ],
            footer=footer,
            timeout=None,
        )
        card.add_row(*self._controls(AloneAnswerView))
        return card

    def _card(self, *, title: str, body: str, footer: str = "", accent=None, rows=()) -> "style.Panel":
        """One small notice card, built this project's way.

        No hex literal and no ping — ``send_panel`` suppresses mentions by default
        and these sentences use plain names rather than ``<@id>`` for exactly that
        reason (``tests/test_naming.py``): explaining a move must not turn into a
        ping. ``rows`` is a control strip, and ``Panel.add_row`` is what knows
        Discord's own rule that a row is one select or five buttons.
        """
        card = style.card(
            title=title,
            body=body,
            accent=style.ACCENT if accent is None else accent,
            footer=footer or None,
            timeout=None,
        )
        if rows:
            card.add_row(*rows)
        return card

    async def _post(self, channel, card):
        """Post a prebuilt card, and never let a courtesy take the feature down."""
        if not self._usable(channel):
            return None
        try:
            return await style.send_panel(channel, card)
        except Exception as exc:  # noqa: BLE001 — a courtesy that cannot be sent is not a fault
            self._failure(f"could not post a card in that room ({type(exc).__name__}: {exc})")
            return None

    async def _notice(self, guild, *, title: str, body: str, footer: str = "", rows=(), room=None):
        """Post a notice where this server keeps its notices. See the resolver."""
        channel = await self._notice_channel(guild, room=room)
        return await self._post(
            channel, self._card(title=title, body=body, footer=footer, rows=rows)
        )

    def _case_embed(self, action: str, guild, lock: alonetime.Lock, detail: str, *, member=None) -> discord.Embed:
        """What a moderator would need to judge one Alone Time event.

        Labels and values, no emoji: the log is read on a phone at 2am, and a
        hammer-and-wrench at the start of a field name costs a character without
        adding a fact. The holder's reason is deliberately never here — the log is
        a record of *what happened*, and the reason is the one thing that never
        leaves the session.
        """
        embed = discord.Embed(
            title=f"Alone Time — {action}",
            color=style.INFO,
            timestamp=discord.utils.utcnow(),
        )
        where = self._channel_name(guild, lock.channel_id)
        embed.set_author(name="DaCat Moderation Log")
        if member is not None:
            embed.add_field(name="Member", value=self._who(member), inline=True)
        embed.add_field(name="Room", value=where, inline=True)
        embed.add_field(
            name="Holder",
            value=lock.holder_label if not lock.server_wide else "nobody — quiet hours",
            inline=True,
        )
        embed.add_field(name="What happened", value=detail[:1000] or "—", inline=False)
        embed.set_footer(text=f"Alone Time • room id {lock.channel_id}")
        return embed

    async def _case(self, guild, lock: alonetime.Lock, action: str, detail: str, *, member=None, settings=None) -> None:
        """Open a card in this server's moderation log, if the server wants them."""
        settings = settings or await self._settings(guild.id)
        if not settings.log_cases:
            return
        await modlog.post(
            guild,
            self._case_embed(action, guild, lock, detail, member=member),
            what=f"the Alone Time {action} in room {lock.channel_id}",
        )

    async def _apply_capacity(self, guild, lock, capacity: int, *, actor: str = "") -> None:
        """Raise or restore the room's cap. The one direct write this cog makes.

        Centralised because three paths need it — the picker, `/alone guests` and
        an allowed request — and a cap that follows only some of them is a guest
        refused by a full room. A refusal is reported on the host log rather than
        raised: the list is already saved, and the case is what tells a moderator
        the room's capacity and its whitelist disagree.
        """
        getter = getattr(guild, "get_channel", None)
        channel = getter(int(lock.channel_id)) if callable(getter) else None
        if channel is None:
            return
        try:
            await channel.edit(
                user_limit=int(capacity),
                reason=f"Alone Time — capacity follows the whitelist by {actor or 'the holder'}",
            )
        except Exception as exc:  # noqa: BLE001 — the list is saved; the cap says so
            self._failure(f"could not set the room's capacity ({type(exc).__name__})")

    # ------------------------------------------------------------------ #
    # The holder's controls
    # ------------------------------------------------------------------ #
    async def whitelist_people(self, interaction: discord.Interaction, member_ids) -> None:
        """Add people to the session's whitelist — the holder's call, and only theirs.

        The picker can be pressed by anybody who can read the card it sits on, so
        the first thing this does is ask the ledger whether the presser is holding
        a room. An administrator is not a special case here on purpose: their way
        in is ``/alone release``, which opens the whole room and writes a case,
        rather than slipping themselves onto somebody's list.
        """
        guild = interaction.guild
        if guild is None:
            return await self._say(interaction, "That only works inside a server.", ok=False)
        settings = await self._settings(guild.id)
        lock = await self._held(guild, interaction.user)
        if lock is None:
            return await self._say(
                interaction,
                "The whitelist belongs to whoever holds the room. An administrator can "
                "open somebody's room with `/alone release`, which is recorded in the "
                "moderation log.",
                ok=False,
            )
        guests = list(lock.guests)
        added = []
        for member_id in member_ids:
            value = int(member_id)
            if value == int(getattr(interaction.user, "id", 0)):
                continue
            if value not in guests:
                guests.append(value)
                added.append(value)
        if not added:
            return await self._say(
                interaction,
                "Nobody new was added — that is your own id, or they are already on the list.",
                ok=False,
            )
        updated = await alonetime.write_guests(guild.id, lock.channel_id, lock, guests=guests)
        capacity = self._capacity_now(guild, updated)
        await self._apply_capacity(guild, updated, capacity, actor=self._who(interaction.user))
        names = ", ".join(
            [await style.display_name(guild, member_id, client=self.bot) for member_id in added]
        )
        await self._case(
            guild, updated, "Whitelist changed",
            f"{self._who(interaction.user)} added {names} to their own room's whitelist.",
            settings=settings,
        )
        note = f"{names} may join **{self._channel_name(guild, updated.channel_id)}** this session."
        getter = getattr(guild, "get_channel", None)
        channel = getter(int(updated.channel_id)) if callable(getter) else None
        if channel is not None:
            return await style.edit_card(
                interaction,
                view=await self._lock_panel(
                    guild, channel, updated, settings, holder=interaction.user,
                    capacity=capacity, just_now=note, private=True,
                ),
            )
        await self._say(interaction, note, ok=True)

    async def rename_held_room(self, interaction: discord.Interaction, name: str) -> None:
        """Rename the room the presser holds, and keep the card current."""
        guild = interaction.guild
        if guild is None:
            return await self._say(interaction, "That only works inside a server.", ok=False)
        settings = await self._settings(guild.id)
        lock = await self._held(guild, interaction.user)
        if lock is None:
            return await self._say(
                interaction,
                "You are not holding a room, so there is nothing of yours to rename.",
                ok=False,
            )
        getter = getattr(guild, "get_channel", None)
        channel = getter(int(lock.channel_id)) if callable(getter) else None
        if channel is None:
            return await self._say(interaction, "That room is gone.", ok=False)
        report = await alonetime.rename_locked(
            guild, channel, lock, name, actor=self._who(interaction.user)
        )
        fresh = alonetime.lock_for(guild.id, lock.channel_id) or lock
        if not report.ok:
            return await self._say(interaction, report.sentence(), ok=False)
        await self._case(guild, fresh, "Room renamed", f"{report.detail}", settings=settings)
        return await style.edit_card(
            interaction,
            view=await self._lock_panel(
                guild, channel, fresh, settings, holder=interaction.user,
                capacity=self._capacity_now(guild, fresh),
                just_now=report.detail, private=True,
            ),
        )

    async def release_held_room(self, interaction: discord.Interaction) -> None:
        """The Release button: give the room back without typing a command."""
        guild = interaction.guild
        if guild is None:
            return await self._say(interaction, "That only works inside a server.", ok=False)
        settings = await self._settings(guild.id)
        lock = await self._held(guild, interaction.user)
        if lock is None:
            return await self._say(
                interaction,
                "You are not holding a room. `/alone start` locks the voice channel you are in.",
                ok=False,
            )
        await self._lift(
            guild, lock, reason=f"released by {self._who(interaction.user)}",
            settings=settings, actor=self._who(interaction.user),
        )
        await self._say(
            interaction,
            f"**{self._channel_name(guild, lock.channel_id)}** is open again — it is back "
            "exactly the way it was before you locked it.",
            ok=True,
        )

    # ------------------------------------------------------------------ #
    # Requests to join
    # ------------------------------------------------------------------ #
    async def ask_to_join(self, interaction: discord.Interaction, *, notice_message_id=0, room=None) -> None:
        """Ask the holder of a locked room to let this member in.

        Five refusals come before the request is delivered, and each one is a
        sentence rather than a failure: the room may be open now, it may be quiet
        hours (which nobody can lift for one person), the asker may already be the
        holder or already allowed in, they may be inside their five-minute wait,
        or they may already have a request waiting. Only a delivered request starts
        the cooldown — a holder whose DMs refused the bot has not been asked, and
        charging the asker for that would be punishing them for somebody else's
        settings.
        """
        guild = interaction.guild
        if guild is None:
            return await self._say(interaction, "That only works inside a server.", ok=False)
        settings = await self._settings(guild.id)
        channel_id = int(getattr(room, "id", 0) or 0)
        if not channel_id and notice_message_id:
            channel_id = alonetime.ask_card_room(notice_message_id)
        if not channel_id:
            locks = list(alonetime.cached(guild.id))
            if len(locks) == 1:
                channel_id = locks[0]
        if not channel_id:
            return await self._say(
                interaction,
                "I cannot tell which room you mean. `/alone ask` lets you pick one.",
                ok=False,
            )
        where = self._channel_name(guild, channel_id)
        lock = alonetime.lock_for(guild.id, channel_id)
        if lock is None:
            return await self._say(
                interaction, f"**{where}** is not locked any more — you can just join it.",
                ok=False,
            )
        if lock.server_wide:
            return await self._say(
                interaction,
                f"**{where}** is closed for the server's quiet hours, so there is nobody "
                "to ask — every room opens again when the window ends.",
                ok=False,
            )
        asker = interaction.user
        if int(getattr(asker, "id", 0)) == lock.holder_id:
            return await self._say(
                interaction,
                "You are holding this room — the picker on your card is how you let "
                "somebody in.",
                ok=False,
            )
        role_ids = [int(getattr(role, "id", 0) or 0) for role in getattr(asker, "roles", None) or ()]
        if lock.admits(int(getattr(asker, "id", 0)), role_ids, bot_id=self._bot_id):
            return await self._say(
                interaction, f"You are already allowed in **{where}**.", ok=False
            )
        left = alonetime.request_left(guild.id, asker.id)
        if left:
            return await self._say(
                interaction,
                f"You can ask again in **{_wait_words(left)}**. A locked room takes one "
                "request per member every five minutes, so its holder is not spammed.",
                ok=False,
            )
        waiting = alonetime.pending_request(guild.id, channel_id, asker.id)
        if waiting is not None:
            return await self._say(
                interaction,
                f"Your request is already with **{lock.holder_label}** — I have not "
                "had an answer yet.",
                ok=False,
            )

        request = alonetime.Request(
            guild_id=guild.id,
            channel_id=channel_id,
            requester_id=int(getattr(asker, "id", 0)),
            requester_name=self._who(asker),
            holder_id=lock.holder_id,
            asked_at=time.time(),
        )
        message = await self._deliver_request(
            guild, lock,
            private=await self._request_card(guild, lock, request),
            public=await self._request_card(guild, lock, request, public=True),
        )
        if message is None:
            return await self._say(
                interaction,
                f"I could not reach **{lock.holder_label}** — their DMs are closed and "
                "there is nowhere in this server I am allowed to put the request. Ask "
                "them in person.",
                ok=False,
            )
        alonetime.remember_request(getattr(message, "id", 0), request)
        alonetime.note_request(guild.id, asker.id)
        await self._say(
            interaction,
            f"Your request is with **{lock.holder_label}**. If they let you in, the room "
            "opens for you; the card they answer on is theirs alone. You can ask again "
            "in five minutes.",
            ok=True,
        )

    async def _deliver_request(self, guild, lock, *, private, public):
        """Hand one request to the holder — a DM first, the notice channel second.

        The DM is the private route and the notice channel is the fallback, and
        which one was used is stated on the card rather than left to be guessed: a
        holder who finds a request in a public channel needs to know their DMs are
        closed, or the fallback reads as the bot publishing their business.
        """
        holder = self._member(guild, lock.holder_id)
        if holder is None:
            fetch = getattr(self.bot, "fetch_user", None)
            if callable(fetch):
                try:
                    holder = await fetch(int(lock.holder_id))
                except Exception:  # noqa: BLE001 — a holder I cannot reach is the fallback
                    holder = None
        if holder is not None and callable(getattr(holder, "send", None)):
            try:
                return await style.send_panel(holder, private)
            except Exception as exc:  # noqa: BLE001 — the DM is a try, not the delivery
                self._failure(f"could not DM the holder ({type(exc).__name__})")
        channel = await self._notice_channel(guild)
        return await self._post(channel, public)

    async def answer_request(self, interaction: discord.Interaction, *, allow: bool) -> None:
        """The holder's press on a request card. Only theirs changes the room."""
        guild = interaction.guild
        message = getattr(interaction, "message", None)
        request = alonetime.request_for(getattr(message, "id", 0)) if message is not None else None
        if request is None:
            return await self._say(
                interaction,
                "That request is no longer open — the room may have been released, or the "
                "bot may have restarted since the card was sent.",
                ok=False,
            )
        if int(getattr(interaction.user, "id", 0)) != request.holder_id:
            return await self._say(
                interaction, "Only the holder of that room can answer this request.", ok=False
            )
        if request.expired():
            alonetime.forget_request(message.id)
            return await self._say(
                interaction,
                "That request has expired — it was waiting more than half an hour. They can "
                "ask again.",
                ok=False,
            )
        lock = alonetime.lock_for(guild.id, request.channel_id)
        if lock is None:
            alonetime.forget_request(message.id)
            return await self._say(
                interaction,
                f"**{self._channel_name(guild, request.channel_id)}** is open again, so "
                "there is nothing to answer — they can walk in.",
                ok=False,
            )
        if request.holder_id != lock.holder_id:
            alonetime.forget_request(message.id)
            return await self._say(
                interaction, "That room is not yours any more, so this is not yours to answer.",
                ok=False,
            )
        settings = await self._settings(guild.id)
        if not allow:
            alonetime.forget_request(message.id)
            return await self._say(
                interaction,
                f"Kept **{request.requester_label}** out. They were not told, and the room "
                "stays locked to you.",
                ok=False,
            )
        if lock.admits(
            request.requester_id,
            bot_id=self._bot_id,
        ):
            alonetime.forget_request(message.id)
            return await self._say(
                interaction, f"**{request.requester_label}** can already join this room.", ok=False
            )
        guests = list(lock.guests)
        if request.requester_id not in guests:
            guests.append(request.requester_id)
        updated = await alonetime.write_guests(guild.id, lock.channel_id, lock, guests=guests)
        capacity = self._capacity_now(guild, updated)
        await self._apply_capacity(guild, updated, capacity, actor=self._who(interaction.user))
        alonetime.forget_request(message.id)
        await self._case(
            guild, updated, "Request allowed",
            f"{self._who(interaction.user)} let {request.requester_label} in after they "
            "asked. The request itself is not stored.",
            settings=settings,
        )
        await self._say(
            interaction,
            f"**{request.requester_label}** may join "
            f"**{self._channel_name(guild, updated.channel_id)}** now, for this session. "
            f"The room's capacity is **{capacity}**.",
            ok=True,
        )

    async def _say(self, interaction, message: str, *, ok: bool = True) -> None:
        """Answer the member, in the right colour and on the right door."""
        embed = discord.Embed(
            description=message, color=style.ACCENT if ok else style.WARN
        )
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # The sweep
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """React to somebody arriving in, or leaving, a locked room.

        Copied from nobody and stolen from every voice bot before it: the *arrival*
        is what has to be answered within a heartbeat, and the *departure* is what
        gives the room back. Both halves are wrapped in one guard because a
        listener that raises on one member's event stops explaining anything to
        anybody, and the failure is reported at most once a minute.
        """
        try:
            await self._voice_event(member, before, after)
        except Exception as exc:  # noqa: BLE001 — the listener must outlive one event
            self._failure(f"voice event failed ({type(exc).__name__}: {exc})")

    async def _voice_event(self, member, before, after) -> None:
        guild = getattr(member, "guild", None)
        if guild is None or getattr(member, "bot", False):
            return  # the bot's own connection, and other bots', are not this feature's
        guild_id = int(getattr(guild, "id", 0) or 0)
        if not self._running(guild_id):
            return
        settings = await self._settings(guild_id)

        arrived = getattr(after, "channel", None)
        if arrived is not None:
            lock = alonetime.lock_for(guild_id, getattr(arrived, "id", 0))
            if lock is not None:
                await self._sweep(guild, member, before, arrived, lock, settings)

        left = getattr(before, "channel", None)
        if left is not None:
            lock = alonetime.lock_for(guild_id, getattr(left, "id", 0))
            if lock is not None and lock.holder_id == int(getattr(member, "id", 0)):
                if not (lock.keep or settings.keep):
                    await self._lift(
                        guild, lock, reason="they left the room", settings=settings
                    )

    async def _sweep(self, guild, member, before, arrived, lock, settings) -> None:
        """Turn away somebody who has just arrived in a locked room."""
        role_ids = [int(getattr(role, "id", 0) or 0) for role in getattr(member, "roles", None) or ()]
        if lock.admits(int(getattr(member, "id", 0)), role_ids, bot_id=self._bot_id):
            return
        if not settings.guard:
            return  # the server asked for the cap alone, and the card says so

        origin = getattr(before, "channel", None)
        if getattr(origin, "id", None) == lock.channel_id:
            # They were already in this room (a mute/deafen/stream change still
            # fires here). There is nowhere to put them *back* to, and moving them
            # to where they already are would be a loop of its own.
            origin = None
        await self._turn_out(guild, member, lock, settings, origin=origin, where=arrived)

    async def _clear_room(self, guild, channel, lock, settings) -> None:
        """The pass a fresh lock owes the room as it already is.

        The listener answers **arrivals**, and an arrival is not the only way
        somebody is inside a room: a lock can start under two people, and the one
        outcome this feature cannot afford is answering "this room is yours" while
        somebody else is still sitting in it. One pass, right after the lock
        applies, over whoever Discord is already reporting. Quiet hours does not
        do this on purpose — a server-wide window is not one person's room, and
        ending every call in the server is not what "quiet hours" means.
        """
        if not settings.guard:
            return
        for member in alonetime.strays_in(channel, lock, bot_id=self._bot_id):
            await self._turn_out(guild, member, lock, settings, origin=None, where=None)

    async def _turn_out(self, guild, member, lock, settings, *, origin, where=None) -> None:
        """Move one person out of somebody's room, and say so.

        ``origin`` is where they came from when the listener heard them arrive —
        and ``None`` when the room's own state is what found them, in which case
        the server's *where a turned-away member goes* setting decides, exactly as
        it does for an arrival. ``where`` is the room the notice falls back to
        when the member is sent to nowhere in particular: the room they arrived
        in, for the sweep.
        """
        target = alonetime.eviction_target(
            settings.evict, origin=origin, afk=getattr(guild, "afk_channel", None)
        )
        report = await alonetime.evict(guild, member, target, lock=lock)
        if not report.ok:
            # Discord refused. The person is still inside somebody's room, and the
            # one thing that must not happen is the holder never finding out.
            self._failure(report.detail)
            await self._tell_holder(guild, lock, report.detail)
            await self._case(guild, lock, "A member could not be moved out", report.detail,
                             member=member, settings=settings)
            return

        # Where the notice falls back to when the server has no notice channel of
        # its own: the room they were actually put in, which is the only place they
        # are known to be able to read it — never the room they were turned out of.
        landing = target if target is not None else where
        if settings.announce and landing is not None:
            await self._denied(guild, member, lock, room=landing)
        await self._case(guild, lock, "Member moved out of a locked room",
                         f"{self._who(member)} was not on the whitelist.", member=member,
                         settings=settings)

    async def _tell_holder(self, guild, lock, detail: str) -> None:
        """Tell the holder when the sweep could not do its job.

        The locked room is the fallback here (rather than ``None``) on purpose:
        the holder is the one person who can still read it, and a server with no
        notice channel must not lose the one sentence this feature owes them.
        """
        if lock.server_wide:
            return
        getter = getattr(guild, "get_channel", None)
        room = getter(int(lock.channel_id)) if callable(getter) else None
        await self._notice(
            guild,
            title="Alone Time — somebody could not be moved out",
            body=detail,
            footer="The lock itself still stands for everybody Discord does let me move.",
            room=room,
        )

    async def _lift(self, guild, lock, *, reason: str, settings=None, actor=None) -> None:
        """Give one room back, and say so where it matters."""
        settings = settings or await self._settings(guild.id)
        report = await alonetime.release_channel(guild, lock, actor=actor, reason=reason)
        if not report.ok:
            self._failure(report.detail)
            await self._tell_holder(guild, lock, report.detail)
            await self._case(guild, lock, "A locked room could not be restored", report.detail,
                             settings=settings)
            return
        getter = getattr(guild, "get_channel", None)
        room = getter(int(lock.channel_id)) if callable(getter) else None
        await self._notice(
            guild,
            title="Alone Time is over",
            body=(
                f"**{lock.holder_label}** is not holding this room any more — it is open "
                "again."
                + (f" {report.name_warning}" if report.name_warning else "")
            ),
            footer="It was locked for " + alonetime.duration_words(lock.held_seconds()) + ".",
            room=room,
        )
        await self._case(guild, lock, "Lock released", f"{report.detail} ({reason})",
                         settings=settings)

    # ------------------------------------------------------------------ #
    # The reconcile loop
    # ------------------------------------------------------------------ #
    @tasks.loop(seconds=RECONCILE_SECONDS)
    async def reconcile(self):
        """Release every lock whose reason to exist is gone. See the docstring."""
        try:
            await self._reconcile_once()
        except Exception as exc:  # noqa: BLE001 — a loop that dies is a feature that vanishes
            self._failure(f"reconcile failed ({type(exc).__name__}: {exc})")

    @reconcile.before_loop
    async def _wait_for_ready(self):
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:  # pragma: no cover - a stand-in bot in a unit test
            pass

    async def _reconcile_once(self) -> None:
        now = time.time()
        dropped = alonetime.forget_stale_requests(now=now)
        if dropped:
            self._log(f"dropped {dropped} expired join request(s)")
        for guild in list(getattr(self.bot, "guilds", None) or []):
            rows = alonetime.cached(getattr(guild, "id", 0))
            until = await alonetime.server_window_end(getattr(guild, "id", 0))
            if not rows and not until:
                continue
            if not self._running(getattr(guild, "id", 0)):
                # A system that is off must not be holding anybody's room, and the
                # sweep is not running either — so the locks are the only part of
                # it still having an effect, and they come off here.
                report = await alonetime.release_all(
                    guild, reason="the system was switched off"
                )
                await alonetime.release_server_window(guild, reason="the system was switched off")
                print(
                    f"[ALONE] system off — released {report.changed} lock(s) in "
                    f"guild {getattr(guild, 'id', 0)}",
                    flush=True,
                )
                continue
            settings = await self._settings(getattr(guild, "id", 0))
            for lock in list(rows.values()):
                getter = getattr(guild, "get_channel", None)
                channel = getter(int(lock.channel_id)) if callable(getter) else None
                present = None
                if channel is not None and lock.holder_id:
                    members = getattr(channel, "members", None)
                    if members is not None:
                        present = any(
                            int(getattr(m, "id", 0) or 0) == lock.holder_id for m in members
                        )
                reason = alonetime.release_reason(
                    lock, settings=settings, holder_present=present,
                    channel_exists=channel is not None, now=now,
                )
                if reason:
                    await self._lift(guild, lock, reason=reason, settings=settings)
            if until and now >= until:
                report = await alonetime.release_server_window(
                    guild, reason="the quiet-hours window ended"
                )
                print(
                    f"[ALONE] quiet hours ended in guild {getattr(guild, 'id', 0)} — "
                    f"{report.changed} room(s) restored",
                    flush=True,
                )

    # ------------------------------------------------------------------ #
    # /alone start
    # ------------------------------------------------------------------ #
    @alone_group.command(
        name="start",
        description="Lock the voice channel you are in to yourself — nobody else can be inside.",
    )
    @app_commands.describe(
        note="Why you are not to be disturbed. Shown back to you, never stored, never logged.",
        name="What to call the room for the rest of your session — I add the lock mark myself.",
        keep="Stay locked after you leave the room, until you release it yourself.",
    )
    async def alone_start(
        self,
        interaction: discord.Interaction,
        note: str = None,
        name: str = None,
        keep: bool = None,
    ):
        guild = interaction.guild
        settings = await self._settings(guild.id)
        if not settings.may_lock(interaction.user):
            return await self._say(
                interaction,
                "This server asks that only "
                + ("administrators " if settings.who == alonetime.WHO_ADMINS else "a named role ")
                + "may lock a room. What is in force is on the dashboard's card for Alone Time.",
                ok=False,
            )
        channel = self._voice_in(interaction.user)
        if channel is None:
            return await self._say(
                interaction,
                "Join the voice channel you want to lock, then run this again — "
                "Alone Time locks the room you are standing in.",
                ok=False,
            )
        if _is_stage(channel):
            return await self._say(
                interaction, "A stage is somebody's event, so it cannot be locked.", ok=False
            )
        existing = alonetime.lock_for(guild.id, channel.id)
        if existing is not None:
            if existing.holder_id == int(interaction.user.id):
                return await self._say(
                    interaction,
                    f"**{self._channel_name(guild, channel.id)}** is already yours — "
                    "`/alone status` shows it and `/alone stop` gives it back.",
                    ok=False,
                )
            return await self._say(
                interaction,
                f"**{self._channel_name(guild, channel.id)}** is held by "
                f"**{existing.holder_label}** right now. If it should not be, ask somebody "
                "with Manage Channels to run `/alone release`.",
                ok=False,
            )
        left = self._cooldown_left(guild.id, interaction.user.id, settings.cooldown_seconds)
        if left:
            return await self._say(
                interaction,
                f"{_wait_words(left)} before you can lock another room — this server asks "
                "for a short wait between locks so a room cannot be taken and given back "
                "in a loop.",
                ok=False,
            )

        await interaction.response.defer(ephemeral=True)
        reason = (note or "").strip()[:200]
        report = await alonetime.lock_channel(
            guild, channel, interaction.user, settings=settings,
            note=reason, name=(name or "").strip()[:80], keep=keep,
            actor=self._who(interaction.user),
        )
        if report.ok:
            self._note_lock(guild.id, interaction.user.id)
            lock = alonetime.lock_for(guild.id, channel.id)
            if lock is not None:
                # The room as it already is: the listener answers arrivals, and a
                # lock that starts under two people must not answer "this is yours"
                # while somebody else is still inside. See `_clear_room`.
                await self._clear_room(guild, channel, lock, settings)
                await self._case(
                    guild, lock, "Room locked",
                    f"{self._who(interaction.user)} locked it to themselves"
                    + (", and gave a reason this feature never writes down."
                       if reason else ".")
                    + (f" {report.detail}" if report.detail else ""),
                    settings=settings,
                )
            if settings.announce:
                await self._notice(
                    guild,
                    title="Alone Time",
                    body=(
                        f"**{self._who(interaction.user)}** has this room to themselves for "
                        "now. Anybody else who arrives is moved back out."
                    ),
                    footer=(
                        "The holder can let people in from the picker on their own card, and "
                        "anybody can ask with the button below — one request per member every "
                        "five minutes. It opens again when they leave, or when they release it."
                    ),
                    rows=self._controls(AloneAskView),
                    room=channel,
                )
            return await style.send_panel(
                interaction,
                await self._lock_panel(
                    guild, channel, lock, settings, holder=interaction.user,
                    capacity=report.capacity, warning=report.name_warning, private=True,
                ),
                ephemeral=True,
            )
        await self._say(interaction, report.sentence(), ok=False)

    # ------------------------------------------------------------------ #
    # /alone stop, /alone status
    # ------------------------------------------------------------------ #
    @alone_group.command(name="stop", description="Release the room you are holding.")
    async def alone_stop(self, interaction: discord.Interaction):
        guild = interaction.guild
        settings = await self._settings(guild.id)
        lock = await self._held(guild, interaction.user)
        if lock is None:
            return await self._say(
                interaction,
                "You are not holding a room. `/alone start` locks the voice channel you are in.",
                ok=False,
            )
        await interaction.response.defer(ephemeral=True)
        await self._lift(
            guild, lock, reason=f"released by {self._who(interaction.user)}",
            settings=settings, actor=self._who(interaction.user),
        )
        await self._say(
            interaction,
            f"**{self._channel_name(guild, lock.channel_id)}** is open again — it is back "
            "exactly the way it was before you locked it.",
            ok=True,
        )

    @alone_group.command(name="status", description="Who holds this room, and which layers are in force.")
    async def alone_status(self, interaction: discord.Interaction):
        guild = interaction.guild
        settings = await self._settings(guild.id)
        lock = await self._lock_here(guild, interaction.user) or await self._held(guild, interaction.user)
        if lock is None:
            here = self._voice_in(interaction.user)
            where = self._channel_name(guild, here.id) if here is not None else "this server"
            return await self._say(
                interaction, f"Nothing is locked in {where} right now.", ok=False
            )
        fields = list(
            alonetime.status_lines(
                lock, settings, guard_ready=alonetime.guard_ready(guild)
            )
        )
        if int(interaction.user.id) == lock.holder_id:
            reason = alonetime.reason_for(guild.id, lock.channel_id)
            if reason:
                fields.insert(
                    1,
                    (
                        "Your reason",
                        f"{reason}\n"
                        "-# Not stored, not logged, not on the dashboard — it goes when "
                        "the room opens.",
                    ),
                )
        card = style.card(
            title=f"Alone Time — {self._channel_name(guild, lock.channel_id)}",
            body="What is holding this room, and who may be inside it.",
            accent=style.ACCENT,
            fields=fields,
            footer=(
                "Discord's Administrator permission ignores channel overwrites, so the "
                "capacity is what holds the room — and the move-out is the backstop."
            ),
            timeout=None,
        )
        await style.send_panel(interaction, card, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /alone guests, /alone ask
    # ------------------------------------------------------------------ #
    @alone_group.command(name="guests", description="The few people who may still join your locked room.")
    @app_commands.describe(
        member="A member to allow in (a Discord user option, so it autocompletes)",
        role="A role whose members may join",
        remove="Take them off the guest list instead of adding them",
    )
    async def alone_guests(
        self,
        interaction: discord.Interaction,
        member: discord.Member = None,
        role: discord.Role = None,
        remove: bool = False,
    ):
        guild = interaction.guild
        settings = await self._settings(guild.id)
        lock = await self._lock_here(guild, interaction.user)
        if lock is None or lock.holder_id != int(interaction.user.id):
            return await self._say(
                interaction,
                "The whitelist belongs to whoever holds the room — stand in yours to "
                "change it. An administrator's way into somebody else's room is "
                "`/alone release`, which opens it and records a case in the moderation log.",
                ok=False,
            )
        if member is None and role is None:
            # The bare form is the reading, and it is what makes the command
            # discoverable: an empty whitelist has to say *empty*, not nothing.
            return await self._say(
                interaction,
                "Whitelist of **" + self._channel_name(guild, lock.channel_id) + "**: "
                + await self._guest_names(guild, lock)
                + ".\nAdd somebody with `/alone guests member:@them` — or from the picker on "
                "your card — and take them off with `remove:True`. It lasts for this session "
                "only, and it clears when the room empties.",
                ok=True,
            )

        guests = list(lock.guests)
        roles = list(lock.guest_roles)
        if member is not None:
            if remove:
                guests = [value for value in guests if value != member.id]
            elif member.id not in guests:
                guests.append(member.id)
        if role is not None:
            if remove:
                roles = [value for value in roles if value != role.id]
            elif role.id not in roles:
                roles.append(role.id)
        lookup = getattr(guild, "get_role", None)
        role_objects = []
        if callable(lookup):
            role_objects = [found for found in (lookup(value) for value in roles) if found is not None]
        updated = await alonetime.write_guests(
            guild.id, lock.channel_id, lock, guests=guests, guest_roles=role_objects
        )
        # The cap has to move with the list, or a guest is refused by capacity
        # before the sweep ever sees them.
        capacity = self._capacity_now(guild, updated)
        await self._apply_capacity(guild, updated, capacity, actor=self._who(interaction.user))
        said = (
            f"{self._who(member)} is " + ("off" if remove else "on")
            if member is not None else
            f"{getattr(role, 'name', 'that role')} is " + ("off" if remove else "on")
        )
        await self._say(
            interaction,
            f"{said} the whitelist for **{lock.holder_label}**'s room. "
            f"Capacity is now **{capacity}**.",
        )

    @alone_group.command(
        name="ask",
        description="Ask the holder of a locked room to let you in — once every five minutes.",
    )
    @app_commands.describe(
        room="Which locked voice channel. Leave it out and I will use the one you were just turned away from."
    )
    async def alone_ask(self, interaction: discord.Interaction, room: discord.VoiceChannel = None):
        await self.ask_to_join(interaction, room=room)

    @alone_ask.autocomplete("room")
    async def _ask_room_choices(self, interaction: discord.Interaction, current: str):
        """The server's locked rooms, by name, so `/alone ask` needs no ids.

        Autocomplete rather than a required id because the person asking was
        moved out of a room they were *in*, and may have no idea what it is
        called. A room with no lock is not offered: asking to get into an open
        room is not a request, it is joining.
        """
        guild = interaction.guild
        if guild is None:
            return []
        typed = str(getattr(current, "value", current) or "").strip().lower()
        choices = []
        for channel_id in sorted(alonetime.cached(guild.id)):
            getter = getattr(guild, "get_channel", None)
            channel = getter(int(channel_id)) if callable(getter) else None
            if channel is None:
                continue
            name = str(getattr(channel, "name", "") or f"channel {channel_id}")
            if typed and typed not in name.lower():
                continue
            choices.append(app_commands.Choice(name=name[:100], value=str(channel_id)))
        return choices[:25]

    # ------------------------------------------------------------------ #
    # /alone list, /alone release
    # ------------------------------------------------------------------ #
    @alone_group.command(name="list", description="Every room locked in this server right now.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def alone_list(self, interaction: discord.Interaction):
        guild = interaction.guild
        locks = list(alonetime.cached(guild.id).values())
        if not locks:
            return await self._say(interaction, "No room in this server is locked.", ok=False)
        fields = []
        for lock in locks[:LIST_LIMIT]:
            fields.append(
                (
                    self._channel_name(guild, lock.channel_id),
                    f"held by **{lock.holder_label}**\n"
                    f"for {alonetime.duration_words(lock.held_seconds())}"
                    + (f" · {lock.turned_away} turned away" if lock.turned_away else ""),
                )
            )
        if len(locks) > LIST_LIMIT:
            footer = f"…and {len(locks) - LIST_LIMIT} more. Release one with /alone release."
        else:
            footer = "Release one with /alone release, or from the dashboard."
        card = style.card(
            title=f"Alone Time — {len(locks)} room(s) locked",
            body="Every room this server currently has locked, and who is holding it.",
            accent=style.ACCENT,
            fields=fields,
            footer=footer,
            timeout=None,
        )
        await style.send_panel(interaction, card, ephemeral=True)

    @alone_group.command(name="release", description="Lift a lock you do not hold.")
    @app_commands.describe(channel="The locked voice channel to open again")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def alone_release(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        guild = interaction.guild
        settings = await self._settings(guild.id)
        row = await db.alone_time_row(guild.id, channel.id)
        if row is None:
            return await self._say(
                interaction, f"**{self._channel_name(guild, channel.id)}** is not locked.",
                ok=False,
            )
        lock = alonetime.from_row(row)
        await interaction.response.defer(ephemeral=True)
        await self._lift(
            guild, lock, reason=f"released by {self._who(interaction.user)}",
            settings=settings, actor=self._who(interaction.user),
        )
        await self._say(
            interaction,
            f"**{self._channel_name(guild, channel.id)}** is open again and back the "
            "way it was.",
        )

    # ------------------------------------------------------------------ #
    # /alone server — quiet hours
    # ------------------------------------------------------------------ #
    @alone_group.command(
        name="server",
        description="Lock every voice channel for a while — quiet hours, the voice twin of /lockdown-server.",
    )
    @app_commands.describe(
        minutes="How long the window lasts. Leave it out for this server's own default.",
        off="End the window now and give every room back",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def alone_server(
        self,
        interaction: discord.Interaction,
        minutes: int = None,
        off: bool = False,
    ):
        guild = interaction.guild
        settings = await self._settings(guild.id)
        await interaction.response.defer(ephemeral=True)
        if off:
            report = await alonetime.release_server_window(
                guild, actor=self._who(interaction.user), reason="ended early"
            )
            return await self._say(interaction, report.sentence(), ok=report.ok)
        window = int(minutes or settings.server_minutes or 60)
        report = await alonetime.lock_all_voice(
            guild, minutes=window, settings=settings, actor=self._who(interaction.user)
        )
        if report.ok:
            first = next(iter(alonetime.cached(guild.id).values()), None)
            if first is not None:
                await self._case(
                    guild, first, "Quiet hours started",
                    f"{self._who(interaction.user)} locked the server's voice channels for "
                    f"{window} minute(s).", settings=settings,
                )
            report.detail = f" It lifts by itself in **{window}** minute(s)."
        await self._say(interaction, report.sentence(), ok=report.ok)

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    @staticmethod
    def _log(message: str) -> None:
        print(f"[ALONE] {message}", flush=True)

    def _failure(self, message: str) -> None:
        """A failure, at most once a minute: a refusal that repeats is one refusal."""
        now = time.time()
        if now - self._last_failure_at < 60:
            return
        self._last_failure_at = now
        self._log(message)


async def setup(bot: commands.Bot):
    await bot.add_cog(AloneTime(bot))
