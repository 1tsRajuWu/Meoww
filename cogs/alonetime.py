"""The `/alone` surface: the commands, the sweep and the reconcile loop.

`utils/alonetime.py` holds the rules and the engine — what a lock is, who may
join a room, what the ledger restores — and this holds the half that needs a
gateway. The split is `cogs/company.py`'s, for the reason that cog's docstring
gives: every rule worth arguing about is testable without Discord, and the rest
can only be tested by using it.

Three things here are decisions rather than plumbing:

* **The sweep is one listener over every voice event, not a loop.** Discord tells
  the bot the moment somebody arrives somewhere, so an intruder is moved back out
  within a heartbeat of getting in — and nothing polls voice at all, which is what
  keeps a feature that touches every room in a server cheap enough to leave on.
* **A notice lands where the person ended up.** Whoever is moved out of a locked
  room cannot read that room's chat any more, so the sentence goes in the room
  they were sent back to. A card posted where only the holder can see it would be
  the bot explaining a decision to the wrong person.
* **The reconcile loop is what stops a room being locked forever.** Discord keeps
  the overwrite and the cap across a restart, so nothing ever unlocks *by itself*;
  this loop is what ends a lock whose holder left, whose channel is gone, whose
  ceiling passed, whose quiet-hours window is over — and every lock at once when a
  server switches the system off, because a system that is off must not be holding
  somebody's room.
"""
from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import alonetime, db, modlog, style
from utils.company import is_stage

#: How often a lock's reason to exist is re-checked. Nothing here is urgent — the
#: sweep handles arrivals instantly — and a minute is long enough that the cost of
#: the whole feature on an idle server is one pass and no writes.
RECONCILE_SECONDS = 60

#: How long the same member must wait before locking another room. A lock, a
#: release and a lock again is a loop somebody can build by accident, and the
#: dashboard's own setting is what a server tunes rather than this.
DEFAULT_COOLDOWN_SECONDS = 60

#: How many rooms one `/alone list` prints before it says how many are left. Ten
#: rows are readable in a card; a hundred are not, and a server with a hundred
#: locked rooms needs the dashboard, not an embed.
LIST_LIMIT = 10

#: How many people one whitelist pick may add. Five is what a session needs and
#: what a picker carries without becoming a scroll; a longer list is one more
#: pick away.
WHITELIST_LIMIT = 5

#: The two controls a holder gets, by ``custom_id``. Fixed strings rather than
#: per-message ids, because a card outlives the process that posted it: one
#: registration in the cog's constructor answers every card the bot has ever
#: sent — the rule ``cogs/meetings.py::RsvpView`` states for its own buttons.
WHITELIST_ID = "alone:whitelist"
RENAME_ID = "alone:rename"


class RenameModal(discord.ui.Modal):
    """What the Rename button opens: one box, one name.

    A modal rather than a slash argument because the holder has already run their
    command and is looking at the card; sending them back to type another one
    would be this feature making its own bookkeeping their errand.
    """

    room_name = discord.ui.TextInput(
        label="What to call this room for the rest of your session",
        placeholder="Study — I add the 🔒 myself",
        max_length=80,
        required=True,
    )

    def __init__(self, cog: "AloneTime") -> None:
        super().__init__(title="Rename the room you are holding")
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.rename_held_room(interaction, str(self.room_name.value))


class AloneView(discord.ui.View):
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
        emoji="✏️",
        custom_id=RENAME_ID,
    )
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RenameModal(self.cog))

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
        bot.add_view(AloneView(self))
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
        getter = getattr(guild, "get_channel", None)
        channel = getter(int(channel_id)) if callable(getter) else None
        return str(getattr(channel, "name", "") or f"channel {channel_id}")

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

    def _cooldown_left(self, guild_id, member_id) -> int:
        last = self._last_lock.get((int(guild_id), int(member_id)))
        if last is None:
            return 0
        left = DEFAULT_COOLDOWN_SECONDS - int(time.time() - last)
        return max(0, left)

    def _note_lock(self, guild_id, member_id) -> None:
        self._last_lock[(int(guild_id), int(member_id))] = time.time()
        if len(self._last_lock) > 5000:
            cutoff = time.time() - DEFAULT_COOLDOWN_SECONDS * 2
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

    def _controls(self) -> tuple:
        """The holder's two controls, taken from the registered view itself.

        One definition, two uses — the trick ``cogs/qna.py::reply_button`` uses:
        the items on the card are exactly the ones :class:`AloneView` publishes,
        so a card can never carry a button the boot registration cannot answer.
        """
        return tuple(AloneView(self).children)

    async def _lock_panel(
        self,
        guild,
        channel,
        lock,
        settings,
        *,
        holder,
        capacity: int,
        warning: str = "",
        note: str = "",
    ) -> "style.Panel":
        """The holder's card: what is theirs, who may come in, how it lifts.

        A Components V2 card rather than an embed because it carries the two
        controls, and because the facts of a session are a layout — a room, a
        holder, what holds the door, the whitelist, the way out — rather than a
        paragraph.
        """
        stays = bool(settings.keep or (lock is not None and lock.keep))
        layers = f"capacity **{capacity}**"
        if settings.guard and not alonetime.guard_ready(guild):
            layers += " · **Move Members** is missing, so nobody can be moved back out"
        elif settings.guard:
            layers += " · anybody else is moved back out"
        fields = [
            ("🔊 Room", f"**{self._channel_name(guild, getattr(channel, 'id', 0))}**"),
            ("👤 Held by", self._who(holder)),
            ("🚪 What holds it", layers),
            ("👥 Whitelist", await self._guest_names(guild, lock) if lock else "nobody yet"),
        ]
        if lock is not None and lock.note:
            fields.append(("📝 Why", lock.note[:200]))
        fields.append(
            ("⏳ Lifts", "when you leave the room" if not stays else "when you run `/alone stop`")
        )
        if note:
            fields.insert(0, ("✅ Just now", note))
        if warning:
            fields.append(("⚠️ About the name", warning))
        card = style.card(
            title="🌙 Alone Time — this room is yours",
            body=(
                "Nobody else can be inside it — not another member, not a moderator, "
                "and not an administrator — unless you whitelist them below."
            ),
            accent=style.ACCENT,
            fields=fields,
            footer=(
                "The whitelist lasts for this session only, and resets the moment the "
                "room empties. `/alone status` reads it back any time."
            ),
            timeout=None,
        )
        card.add_row(*self._controls())
        return card

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
            return await self._say(interaction, "❌ That only works inside a server.", ok=False)
        settings = await self._settings(guild.id)
        lock = await self._held(guild, interaction.user)
        if lock is None:
            return await self._say(
                interaction,
                "❌ The whitelist belongs to whoever holds the room. An administrator can "
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
                "ℹ️ Nobody new was added — that is your own id, or they are already on the list.",
                ok=False,
            )
        getter = getattr(guild, "get_channel", None)
        channel = getter(int(lock.channel_id)) if callable(getter) else None
        updated = await alonetime.write_guests(guild.id, lock.channel_id, lock, guests=guests)
        capacity = self._capacity_now(guild, updated)
        if channel is not None:
            try:
                await channel.edit(user_limit=capacity, reason="Alone Time — whitelist changed")
            except Exception as exc:  # noqa: BLE001 — the list is saved; the cap says so
                self._failure(f"could not raise the room's capacity ({type(exc).__name__})")
        names = ", ".join(
            [await style.display_name(guild, member_id, client=self.bot) for member_id in added]
        )
        await self._case(
            guild, updated, "Whitelist changed",
            f"{self._who(interaction.user)} added {names} to their own room's whitelist.",
            settings=settings,
        )
        note = f"{names} may join **{self._channel_name(guild, updated.channel_id)}** this session."
        if channel is not None:
            return await style.edit_card(
                interaction,
                view=await self._lock_panel(
                    guild, channel, updated, settings, holder=interaction.user,
                    capacity=capacity, note=note,
                ),
            )
        await self._say(interaction, f"✅ {note}", ok=True)

    async def rename_held_room(self, interaction: discord.Interaction, name: str) -> None:
        """Rename the room the presser holds, and keep the card current."""
        guild = interaction.guild
        if guild is None:
            return await self._say(interaction, "❌ That only works inside a server.", ok=False)
        settings = await self._settings(guild.id)
        lock = await self._held(guild, interaction.user)
        if lock is None:
            return await self._say(
                interaction,
                "❌ You are not holding a room, so there is nothing of yours to rename.",
                ok=False,
            )
        getter = getattr(guild, "get_channel", None)
        channel = getter(int(lock.channel_id)) if callable(getter) else None
        if channel is None:
            return await self._say(interaction, "❌ That room is gone.", ok=False)
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
                note=report.detail,
            ),
        )

    async def _say(self, interaction, message: str, *, ok: bool = True) -> None:
        """Answer the member, in the right colour and on the right door."""
        embed = discord.Embed(
            description=message, color=style.ACCENT if ok else style.WARN
        )
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # Cards and the log
    # ------------------------------------------------------------------ #
    def _card(self, *, title: str, body: str, footer: str = "", accent=None, rows=()) -> "style.Panel":
        """One card, built this project's way.

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

    async def _post(self, channel, card) -> None:
        """Post a prebuilt card, and never let a courtesy take the feature down."""
        try:
            await style.send_panel(channel, card)
        except Exception as exc:  # noqa: BLE001 — a courtesy that cannot be sent is not a fault
            self._failure(f"could not post a card in that room ({type(exc).__name__}: {exc})")

    async def _notice(self, channel, *, title: str, body: str, footer: str = "", rows=()) -> None:
        await self._post(channel, self._card(title=title, body=body, footer=footer, rows=rows))

    async def _denied(self, guild, landing, member, lock) -> None:
        """The card a turned-away member finds where they land.

        **Why this is not a hidden message.** Discord has no ephemeral message
        outside an interaction and a voice join is not one — ephemeral replies
        exist only as an answer to something a person typed or pressed — so there
        is nothing the bot can send that only the rejected member would see. A DM
        would be, and this feature deliberately does not send one (a stranger's
        DMs are not this server's to open); the notice they can actually read is
        the card in the room they are sent back to, and it says what happened and
        who to ask. That is the honest version of "a private notice", and it is
        the reason the card is short: it is addressed to one person in public.
        """
        where = self._channel_name(guild, lock.channel_id)
        card = style.card(
            title="🔒 Access denied — this room is on Alone Time",
            body=(
                f"**{self._who(member)}**, you were moved out of **{where}**. It is held by "
                f"**{lock.holder_label}** right now and nobody else may be inside it — not "
                "another member, not a moderator, and not an administrator."
            ),
            accent=style.DANGER,
            fields=[
                ("🔊 Room", f"**{where}**"),
                ("👤 Held by", lock.holder_label),
                ("👥 Whitelist", await self._guest_names(guild, lock)),
                (
                    "🙋 What to do",
                    "Ask the holder to add you — they have a picker on their card, and "
                    "`/alone guests` does the same. It clears when their session ends.",
                ),
            ],
            footer=(
                f"Room id {lock.channel_id} · you may be whitelisted next time — this notice "
                "is public because Discord sends no private message for a voice join."
            ),
            timeout=None,
        )
        await self._post(landing, card)

    def _case_embed(self, action: str, guild, lock: alonetime.Lock, detail: str, *, member=None) -> discord.Embed:
        """What a moderator would need to judge one Alone Time event."""
        embed = discord.Embed(
            title=f"🌙 Alone Time — {action}",
            color=style.INFO,
            timestamp=discord.utils.utcnow(),
        )
        where = self._channel_name(guild, lock.channel_id)
        embed.set_author(name="DaCat Moderation Log")
        if member is not None:
            embed.add_field(name="👤 Member", value=self._who(member), inline=True)
        embed.add_field(name="🔊 Room", value=where, inline=True)
        embed.add_field(
            name="👤 Holder",
            value=lock.holder_label if not lock.server_wide else "nobody — quiet hours",
            inline=True,
        )
        embed.add_field(name="⚙️ What happened", value=detail[:1000] or "—", inline=False)
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

        landing = target if target is not None else where
        if settings.announce and landing is not None:
            await self._denied(guild, landing, member, lock)
        await self._case(guild, lock, "Member moved out of a locked room",
                         f"{self._who(member)} was not on the whitelist.", member=member,
                         settings=settings)

    async def _tell_holder(self, guild, lock, detail: str) -> None:
        """Tell the holder when the sweep could not do its job."""
        if lock.server_wide:
            return
        channel = None
        getter = getattr(guild, "get_channel", None)
        if callable(getter):
            channel = getter(int(lock.channel_id))
        if channel is None:
            return
        await self._notice(
            channel,
            title="⚠️ Alone Time — I could not move somebody out",
            body=detail,
            footer="The lock itself still stands for everybody Discord does let me move.",
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
        channel = getter(int(lock.channel_id)) if callable(getter) else None
        if channel is not None:
            await self._notice(
                channel,
                title="🔓 Alone Time is over",
                body=(
                    f"**{lock.holder_label}** is not holding this room any more — it is "
                    "open again."
                    + (f" {report.name_warning}" if report.name_warning else "")
                ),
                footer="It was locked for " + alonetime.duration_words(lock.held_seconds()) + ".",
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
        note="Why you are not to be disturbed. Shown on the room's status, never sent anywhere.",
        name="What to call the room for the rest of your session — I add the 🔒 myself.",
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
                "🌙 This server asks that only "
                + ("administrators " if settings.who == alonetime.WHO_ADMINS else "a named role ")
                + "may lock a room. What is in force is on the dashboard's card for Alone Time.",
                ok=False,
            )
        channel = self._voice_in(interaction.user)
        if channel is None:
            return await self._say(
                interaction,
                "❌ Join the voice channel you want to lock, then run this again — "
                "Alone Time locks the room you are standing in.",
                ok=False,
            )
        if _is_stage(channel):
            return await self._say(
                interaction, "❌ A stage is somebody's event, so it cannot be locked.", ok=False
            )
        existing = alonetime.lock_for(guild.id, channel.id)
        if existing is not None:
            if existing.holder_id == int(interaction.user.id):
                return await self._say(
                    interaction,
                    f"🔒 **{self._channel_name(guild, channel.id)}** is already yours — "
                    "`/alone status` shows it and `/alone stop` gives it back.",
                    ok=False,
                )
            return await self._say(
                interaction,
                f"🔒 **{self._channel_name(guild, channel.id)}** is held by "
                f"**{existing.holder_label}** right now. If it should not be, ask somebody "
                "with Manage Channels to run `/alone release`.",
                ok=False,
            )
        left = self._cooldown_left(guild.id, interaction.user.id)
        if left:
            return await self._say(
                interaction,
                f"⏳ {left}s before you can lock another room — this server asks for a short "
                "wait between locks so a room cannot be taken and given back in a loop.",
                ok=False,
            )

        await interaction.response.defer(ephemeral=True)
        report = await alonetime.lock_channel(
            guild, channel, interaction.user, settings=settings,
            note=(note or "").strip()[:200], name=(name or "").strip()[:80], keep=keep,
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
                    + (f" for: {report.detail}" if report.detail else "."),
                    settings=settings,
                )
            if settings.announce:
                await self._notice(
                    channel,
                    title="🌙 Alone Time",
                    body=(
                        f"**{self._who(interaction.user)}** has this room to themselves for now. "
                        "I will move anybody else back out."
                    ),
                    footer=(
                        "The holder can whitelist somebody from the picker below. It opens "
                        "again the moment they leave, or when they run /alone stop."
                    ),
                    rows=self._controls(),
                )
            return await style.send_panel(
                interaction,
                await self._lock_panel(
                    guild, channel, lock, settings, holder=interaction.user,
                    capacity=report.capacity, warning=report.name_warning,
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
                "ℹ️ You are not holding a room. `/alone start` locks the voice channel you are in.",
                ok=False,
            )
        await interaction.response.defer(ephemeral=True)
        await self._lift(
            guild, lock, reason=f"released by {self._who(interaction.user)}",
            settings=settings, actor=self._who(interaction.user),
        )
        await self._say(
            interaction,
            f"🔓 **{self._channel_name(guild, lock.channel_id)}** is open again — it is back "
            "exactly the way it was before you locked it.",
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
                interaction, f"ℹ️ Nothing is locked in {where} right now.", ok=False
            )
        embed = discord.Embed(
            title=f"🌙 Alone Time — {self._channel_name(guild, lock.channel_id)}",
            color=style.ACCENT,
            timestamp=discord.utils.utcnow(),
        )
        for label, value in alonetime.status_lines(
            lock, settings, guard_ready=alonetime.guard_ready(guild)
        ):
            embed.add_field(name=label, value=value[:1024], inline=True)
        embed.set_footer(
            text=(
                "Discord's Administrator permission ignores channel overwrites, so the "
                "capacity is what holds the room — and the sweep is the backstop."
            )
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /alone guests
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
                "❌ The whitelist belongs to whoever holds the room — stand in yours to "
                "change it. An administrator's way into somebody else's room is "
                "`/alone release`, which opens it and records a case in the moderation log.",
                ok=False,
            )
        if member is None and role is None:
            # The bare form is the reading, and it is what makes the command
            # discoverable: an empty whitelist has to say *empty*, not nothing.
            return await self._say(
                interaction,
                "👥 Whitelist of **" + self._channel_name(guild, lock.channel_id) + "**: "
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
        await alonetime.write_guests(
            guild.id, lock.channel_id, lock, guests=guests, guest_roles=role_objects
        )
        # The cap has to move with the list, or a guest is refused by capacity
        # before the sweep ever sees them.
        getter = getattr(guild, "get_channel", None)
        channel = getter(int(lock.channel_id)) if callable(getter) else None
        if channel is not None:
            capacity = alonetime.capacity_for(guild, guests, role_objects)
            try:
                await channel.edit(
                    user_limit=capacity,
                    reason="Alone Time — guest list changed",
                )
            except Exception as exc:  # noqa: BLE001
                self._failure(f"could not raise the room's capacity ({type(exc).__name__})")
        said = (
            f"{self._who(member)} is " + ("off" if remove else "on")
            if member is not None else
            f"{getattr(role, 'name', 'that role')} is " + ("off" if remove else "on")
        )
        await self._say(
            interaction,
            f"✅ {said} the whitelist for **{lock.holder_label}**'s room. "
            f"Capacity is now **{alonetime.capacity_for(guild, guests, role_objects)}**.",
        )

    # ------------------------------------------------------------------ #
    # /alone list, /alone release
    # ------------------------------------------------------------------ #
    @alone_group.command(name="list", description="Every room locked in this server right now.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def alone_list(self, interaction: discord.Interaction):
        guild = interaction.guild
        locks = list(alonetime.cached(guild.id).values())
        if not locks:
            return await self._say(interaction, "ℹ️ No room in this server is locked.", ok=False)
        embed = discord.Embed(
            title=f"🌙 Alone Time — {len(locks)} room(s) locked",
            color=style.ACCENT,
            timestamp=discord.utils.utcnow(),
        )
        for lock in locks[:LIST_LIMIT]:
            embed.add_field(
                name=self._channel_name(guild, lock.channel_id),
                value=(
                    f"held by **{lock.holder_label}**\n"
                    f"for {alonetime.duration_words(lock.held_seconds())}"
                    + (f" · {lock.turned_away} turned away" if lock.turned_away else "")
                ),
                inline=True,
            )
        if len(locks) > LIST_LIMIT:
            embed.set_footer(
                text=f"…and {len(locks) - LIST_LIMIT} more. Release one with /alone release."
            )
        else:
            embed.set_footer(text="Release one with /alone release, or from the dashboard.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @alone_group.command(name="release", description="Lift a lock you do not hold.")
    @app_commands.describe(channel="The locked voice channel to open again")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def alone_release(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        guild = interaction.guild
        settings = await self._settings(guild.id)
        row = await db.alone_time_row(guild.id, channel.id)
        if row is None:
            return await self._say(
                interaction, f"ℹ️ **{channel.name}** is not locked.", ok=False
            )
        lock = alonetime.from_row(row)
        await interaction.response.defer(ephemeral=True)
        await self._lift(
            guild, lock, reason=f"released by {self._who(interaction.user)}",
            settings=settings, actor=self._who(interaction.user),
        )
        await self._say(
            interaction, f"🔓 **{channel.name}** is open again and back the way it was."
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
