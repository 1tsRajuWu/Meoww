"""The voice companion: the bot sits with somebody alone in a voice channel.

`utils.company` holds the rules and this holds the connection — and the split is
the point, because every rule worth arguing about (when somebody counts as alone,
when the bot leaves, what it says) is testable without a gateway, while the half
that needs Discord can only be tested by using it.

Three things this deliberately does *not* do, each of them a way to get the bot
kicked out of a server:

* **It does not arrive instantly.** Somebody alone for a moment is somebody whose
  friend is reconnecting, so the bot waits (``utils.company.GRACE_SECONDS``) before
  it moves — and it leaves the moment its reason to be there is gone. Arriving
  slowly and leaving quickly is the shape of a guest rather than a squatter.
* **It does not hold more than one connection.** A Discord client can only be in
  one voice channel at a time, so a second join would silently kick the first: the
  companion never takes the socket from another voice feature, it waits.
* **It does not follow somebody out.** Leaving a channel the bot is in is an
  answer, and it is remembered for a few minutes. Following somebody *into* another
  channel in the same breath is a move rather than an exit, and that it does do —
  it is the same situation one room over.

And it does not listen: the connection is deafened *and* muted, which is a state
everybody in the channel can see rather than a claim in a document.

The one thing Discord will not let it do is watch a stream. A bot has no access to
Go Live — there is no API, and the gateway never carries the video — so what the
card says is what is true: the person is live, the console shows it, and watching
is not on offer.
"""
from __future__ import annotations

import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import company, style

#: How often the "somebody has been alone long enough" question is asked. Every
#: five seconds over cached members is free — no API call is made here, and the
#: only thing that happens is a comparison against a clock.
RECONCILE_SECONDS = 5

#: How long to leave the bot alone after a failed connection. A channel it cannot
#: join (missing Connect, a voice server that is down) is not worth retrying every
#: cycle, and a loop of failed handshakes is how a bot gets rate-limited at the
#: voice gateway.
CONNECT_BACKOFF_SECONDS = 300.0

#: How often a *failure* may be written to the host log. A refusal that repeats is
#: one refusal, and a log nobody can read is a log nobody reads.
LOG_EVERY = 60.0


class Company(commands.Cog):
    """Keeps somebody alone in a voice channel company, if the server allows it."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        #: channel id -> the moment its only occupant became alone.
        self._alone_since: dict[int, float] = {}
        #: member id -> when they left a channel the bot was sitting in.
        self._left_us: dict[int, float] = {}
        self._blocked_until = 0.0
        self._last_failure_at = 0.0
        #: Where the companion is sitting *for this feature*, and why. Kept here
        #: rather than read back off the voice client, so the console can say which
        #: person the bot went to sit with — which is the fact an operator asks for.
        self._sitting: dict = {}
        self.reconcile.start()

    def cog_unload(self):
        self.reconcile.cancel()

    # ------------------------------------------------------------------ #
    # Bookkeeping
    # ------------------------------------------------------------------ #
    @property
    def _bot_id(self) -> int:
        return int(getattr(getattr(self.bot, "user", None), "id", 0) or 0)

    def _connected_channel(self):
        """Where the bot is connected, if anywhere (one connection, ever)."""
        clients = list(getattr(self.bot, "voice_clients", None) or [])
        if not clients:
            return None
        return getattr(clients[0], "channel", None)

    def _module_on(self, guild) -> bool:
        checker = getattr(self.bot, "is_module_enabled", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(getattr(guild, "id", 0), "company"))
        except Exception:
            return True

    def snapshot(self) -> dict:
        """What the companion is doing, as a plain dict the console can read.

        Synchronous and total on purpose: this is called from Flask's thread while
        the loop is running on the bot's, so it takes no ``await`` and touches only
        values that were already computed. Every field is either read or absent.
        """
        sitting = dict(self._sitting or {})
        now = time.time()
        rooms = []
        for channel_id, since in list(self._alone_since.items()):
            rooms.append({
                "channel_id": str(channel_id),
                "waiting_seconds": max(0, int(now - since)),
            })
        rooms.sort(key=lambda room: room["waiting_seconds"], reverse=True)
        return {
            "sitting": sitting or None,
            "waiting": rooms,
            "left_us": [str(member_id) for member_id in self._left_us],
            "blocked_seconds": max(0, int(self._blocked_until - now)),
            "grace_seconds": company.GRACE_SECONDS,
            "cooldown_seconds": company.COOLDOWN_SECONDS,
        }

    def _note(self, channel, now: float) -> None:
        """Start or end the clock on a channel's 'one person, on their own'."""
        if channel is None:
            return
        if company.alone(getattr(channel, "members", None), bot_id=self._bot_id):
            self._alone_since.setdefault(int(channel.id), now)
        else:
            self._alone_since.pop(int(channel.id), None)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """React to somebody arriving in, or leaving, a voice channel.

        The two halves are not symmetric on purpose. The *clock* is started here,
        so an arrival is noticed at once and honoured after the grace period, while
        a departure is acted on immediately: the bot leaving a channel that is now
        empty, or that has real company in it, is a small kindness and making
        somebody wait five seconds for it would be strange.
        """
        if getattr(member, "bot", True):
            return
        now = time.time()
        self._note_live(member, after)
        was_ours = self._is_ours(before.channel)

        if was_ours:
            if after.channel is None:
                # They left voice altogether. That is an answer, and it is
                # remembered: the bot does not follow them back in for a while.
                self._left_us[int(member.id)] = now
            elif after.channel is not before.channel:
                await self._handle_move(after.channel)
                return

        for channel in (before.channel, after.channel):
            self._note(channel, now)

        await self._leave_where_the_company_is_gone()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The module gate, where the rest of this bot keeps it.

        A cog-level check rather than one inside the callback: this is the shape
        every other gated cog uses, and it is the shape the rule in
        ``utils/command_scope`` is checked against — the registry decides what a
        server is *offered*, and this decides what the bot *refuses*, and the two
        sets have to be the same one (``tests/test_command_scope.py``).
        """
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "company"):
                await interaction.response.send_message(
                    "⚠️ The **Voice Company** system is currently **turned off** on this "
                    "server.\nAn administrator can re-enable it in the "
                    "[Web Dashboard](https://meow-bot.pages.dev/dashboard).",
                    ephemeral=True,
                )
                return False
        return True

    def _note_live(self, member, after) -> None:
        """Keep the sitting record honest when the person starts or stops a stream.

        The record is what the owner's dashboard shows, and a stream that started
        after the bot sat down is exactly as interesting as one that was already
        running — so it is updated from the same event rather than only at arrival.
        """
        if not self._sitting:
            return
        if int(getattr(member, "id", 0) or 0) != int(self._sitting.get("person_id", 0) or 0):
            return
        channel = getattr(after, "channel", None)
        if channel is None:
            return
        state = getattr(member, "voice", None) or after
        self._sitting["streaming"] = bool(getattr(state, "self_stream", False))
        self._sitting["video"] = bool(getattr(state, "self_video", False))

    def _radio_owns(self, guild) -> bool:
        """Whether the radio is using this guild's connection right now.

        The two features share one socket, because a Discord client sits in one
        voice channel at a time — so the companion has to be able to stand down.
        Without this, a companion that decided the room had company would
        disconnect the bot *mid-stream*: the radio is on the same connection it
        was about to close. Imported here rather than at module scope so this
        stays the only direction of dependency between the two features (the
        radio does not know the companion exists).
        """
        try:
            from utils import radio_stream  # noqa: PLC0415

            return radio_stream.holds_voice(getattr(guild, "id", None))
        except Exception:
            return False

    def _is_ours(self, channel) -> bool:
        connected = self._connected_channel()
        return (
            channel is not None
            and connected is not None
            and getattr(connected, "id", None) == getattr(channel, "id", None)
        )

    async def _handle_move(self, target) -> None:
        """Somebody the bot was sitting with changed channel.

        A move rather than an exit, so the honest reading is *still alone, one room
        over*: if their new channel has nobody else in it, the bot comes along —
        which is cheaper and calmer than disconnecting and rejoining, and it does
        not start a five-minute cooldown over somebody switching rooms.
        """
        if self._radio_owns(getattr(target, "guild", None)):
            return
        client = next(iter(getattr(self.bot, "voice_clients", None) or []), None)
        if client is None:
            return
        if company.alone(getattr(target, "members", None), bot_id=self._bot_id):
            try:
                await client.move_to(target)
                self._log(f"moved to #{self._name(target)} — still on their own")
            except Exception as exc:
                self._failure(f"could not move to #{self._name(target)} ({type(exc).__name__}: {exc})")
                return
            self._alone_since.pop(int(getattr(target, "id", 0)), None)
            if self._sitting:
                self._sitting["channel_id"] = str(getattr(target, "id", 0) or 0)
                self._sitting["channel"] = self._name(target)
                self._sitting["since"] = int(time.time())
                self._sitting["reason"] = "they moved rooms, still on their own"
            return
        await self._leave(client, "somebody real is in the room now")

    async def _leave_where_the_company_is_gone(self) -> None:
        """Disconnect from any channel whose reason to be occupied has gone."""
        for client in list(getattr(self.bot, "voice_clients", None) or []):
            channel = getattr(client, "channel", None)
            if channel is None:
                continue
            guild = getattr(channel, "guild", None)
            if guild is not None and self._radio_owns(guild):
                # Somebody pressed play. The connection is the radio's now, and
                # closing it would stop what they are listening to — which is a
                # stranger way to say goodbye than anything this feature could
                # intend.
                await self._say_brb(channel)
                self._sitting = {}
                continue
            if guild is not None and not self._module_on(guild):
                await self._leave(client, "the module was switched off")
                continue
            humans = company.humans_in(channel, bot_id=self._bot_id)
            if company.decide(humans=humans, here=True) == company.LEAVE:
                await self._leave(client, "the channel emptied" if humans == 0 else "somebody real joined")

    def _resolve_channel(self, channel_id: str):
        """The channel object for an id, or ``None``. Never raises."""
        try:
            return self.bot.get_channel(int(channel_id))
        except Exception:
            return None

    async def _say_brb(self, currently_in) -> None:
        """Tell whoever was being sat with why the bot just walked out.

        Music takes the connection to another room, because one Discord client sits
        in one voice channel — so somebody who had company a moment ago is alone
        again with no evidence of why. Unsaid, that reads as *it left because of me*,
        when the truth is a two-room errand it was asked to run.

        **Nothing is said when the radio started in the room the bot was already
        in.** That person is now less alone than they were, and a goodbye for a song
        playing in their own channel is noise — which is why the check is on the
        channel *changing* rather than on the radio simply starting. It also honours the
        server's own arrival setting: a server that asked for silent arrivals has
        asked for silence here too, so it sends nothing at all.
        """
        sitting = self._sitting
        if not sitting or not sitting.get("greeted"):
            return
        was_in = str(sitting.get("channel_id") or "")
        now_in = str(getattr(currently_in, "id", "") or "")
        if not was_in or was_in == now_in:
            return
        channel = self._resolve_channel(was_in)
        if channel is None:
            return
        card = style.card(
            title="🎧 brb",
            body=company.leaving(pick=random.choice),
            accent=style.ACCENT,
            footer=company.leaving_footer(),
            timeout=None,
        )
        try:
            await style.send_panel(channel, card)
            self._log(f"said brb in #{self._name(channel)} — the radio took the connection")
        except Exception as exc:
            # A courtesy that cannot be sent is not a reason to lose the run.
            self._failure(
                f"could not say brb in #{self._name(channel)} ({type(exc).__name__}: {exc})"
            )

    async def _leave(self, client, reason: str) -> None:
        """Disconnect, quietly. A failed disconnect is reported, never raised."""
        channel = getattr(client, "channel", None)
        try:
            await client.disconnect(force=True)
            self._log(f"left voice — {reason}")
        except Exception as exc:  # pragma: no cover - a broken socket is not a crash
            self._failure(f"could not leave voice ({type(exc).__name__}: {exc})")
        finally:
            if channel is not None:
                self._alone_since.pop(int(getattr(channel, "id", 0)), None)
            # The sitting record is about a connection, so it dies with the
            # connection — a console that still showed the bot in a room it had
            # left would be the one thing an operator cannot check for themselves.
            self._sitting = {}

    # ------------------------------------------------------------------ #
    # Arriving
    # ------------------------------------------------------------------ #
    @tasks.loop(seconds=RECONCILE_SECONDS)
    async def reconcile(self):
        """Look for somebody who has been alone long enough, and sit with them."""
        try:
            await self._reconcile_once()
        except Exception as exc:  # a loop that dies is a feature that vanishes
            self._failure(f"reconcile failed ({type(exc).__name__}: {exc})")

    @reconcile.before_loop
    async def _wait_for_ready(self):
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            # Stand-in or uninitialised bot in unit tests
            pass

    async def _reconcile_once(self) -> None:
        now = time.time()
        connected = self._connected_channel()
        if connected is not None:
            guild = getattr(connected, "guild", None)
            if guild is not None and not self._module_on(guild) and not self._radio_owns(guild):
                await self._leave(
                    next(iter(getattr(self.bot, "voice_clients", None) or []), None),
                    "the module was switched off",
                )
                return
        if now < self._blocked_until or connected is not None:
            return
        guilds = list(getattr(self.bot, "guilds", None) or [])
        for guild in guilds:
            if not self._module_on(guild):
                continue
            settings = await company.get(getattr(guild, "id", None))
            if not settings.enabled:
                continue
            target = self._pick(guild, now)
            if target is not None:
                await self._arrive(target, settings)
                return

    def _pick(self, guild, now: float):
        """The channel this guild wants the bot in, or ``None``.

        A member who left a channel the bot was in is skipped for a while: they had
        the company and chose to leave it, which is the one unambiguous "no" this
        feature can be given without a command.
        """
        for channel in company.voice_channels(guild):
            started = self._alone_since.get(int(getattr(channel, "id", 0)))
            if started is None or now - started < company.GRACE_SECONDS:
                continue
            if not company.eligible(
                channel,
                bot_id=self._bot_id,
                afk_channel_id=getattr(getattr(guild, "afk_channel", None), "id", None),
            ):
                continue
            occupant = next(iter(getattr(channel, "members", None) or []), None)
            if occupant is None:
                continue
            left_at = self._left_us.get(int(occupant.id))
            if left_at is not None and now - left_at < company.COOLDOWN_SECONDS:
                continue
            return channel
        return None

    async def _arrive(self, channel, settings: company.Settings) -> bool:
        """Connect to ``channel``, and say hello if the server wants that."""
        name = self._name(channel)
        if not self._can_connect(channel):
            self._blocked_until = time.time() + CONNECT_BACKOFF_SECONDS
            self._failure(f"cannot connect to #{name}: the bot lacks Connect there")
            return False
        try:
            # Deafened *and* muted: it can neither hear the room nor speak into it.
            await channel.connect(self_deaf=True, self_mute=True, timeout=15)
        except Exception as exc:
            self._blocked_until = time.time() + CONNECT_BACKOFF_SECONDS
            self._failure(f"could not connect to #{name} ({type(exc).__name__}: {exc})")
            return False

        occupant = next(iter(getattr(channel, "members", None) or []), None)
        state = getattr(occupant, "voice", None)
        streaming = bool(getattr(state, "self_stream", False))
        guild = getattr(channel, "guild", None)
        self._sitting = {
            "guild_id": str(getattr(guild, "id", 0) or 0),
            "guild": self._name(guild),
            "channel_id": str(getattr(channel, "id", 0) or 0),
            "channel": name,
            "person_id": str(getattr(occupant, "id", 0) or 0),
            "person": self._name(occupant),
            "since": int(time.time()),
            "streaming": streaming,
            "video": bool(getattr(state, "self_video", False)),
            "greeted": bool(settings.greeting),
            "reason": "they were alone in the room",
        }
        self._log(
            f"sitting in {self._name(guild)}/#{name}"
            + (" — they are live" if streaming else "")
        )
        self._alone_since.pop(int(getattr(channel, "id", 0)), None)
        if settings.greeting:
            await self._greet(channel, streaming=streaming)
        return True

    def _can_connect(self, channel) -> bool:
        """Whether the bot's own permissions allow Connect here.

        Checked rather than attempted, because a failed handshake is a failed UDP
        negotiation: asking first turns an error into a sentence.
        """
        myself = getattr(getattr(channel, "guild", None), "me", None)
        permissions = getattr(channel, "permissions_for", None)
        if myself is None or not callable(permissions):
            return True  # a stand-in with no member object: try it
        try:
            return bool(permissions(myself).connect)
        except Exception:
            return True

    async def _greet(self, channel, *, streaming: bool) -> None:
        """Say hi in the voice channel's own text chat.

        The picture travels as the card's thumbnail rather than as an emoji tag,
        because a custom emoji is only usable by a client that shares a server with
        it — this deployment is not in every server the tag came from, and a tag
        that cannot be used is printed as literal text. When it *is* usable the tag
        rides along at the end of the line, so the animation comes with it.
        """
        emoji = company.resolve_emoji(self.bot)
        card = style.card(
            title="🫂 You're not alone",
            body=company.body(
                company.emoji_markup(emoji),
                company.greeting(streaming=streaming, pick=random.choice),
            ),
            accent=style.ACCENT,
            thumbnail=company.picture_url(emoji),
            fields=company.fields(streaming=streaming, channel=f"<#{getattr(channel, 'id', '')}>"),
            footer=company.footer(),
            timeout=None,
        )
        try:
            await style.send_panel(channel, card)
        except Exception as exc:
            # The room is what matters; the message is a courtesy, and a channel
            # that refuses one is still a channel the bot is sitting in.
            self._failure(
                f"could not say hello in #{self._name(channel)} ({type(exc).__name__}: {exc})"
            )

    # ------------------------------------------------------------------ #
    # The command
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="company",
        description="Whether I sit with somebody who is alone in a voice channel.",
    )
    @app_commands.describe(
        mode="Auto: join a channel whose only occupant is one person. Off: never join.",
        greeting="Say hello when I arrive, or sit there quietly.",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="🫂 Auto (default)", value="auto"),
            app_commands.Choice(name="🚫 Off — never join", value="off"),
        ],
        greeting=[
            app_commands.Choice(name="Say hello", value="on"),
            app_commands.Choice(name="Arrive silently", value="off"),
        ],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def company_cmd(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str] = None,
        greeting: app_commands.Choice[str] = None,
    ):
        """Set this server's company rules, or show what they are.

        Server-scoped and administrator-only, like every other switch that changes
        what the bot does on its own: one person deciding that the bot should sit in
        the voice channels is a decision the server makes, not a member.
        """
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ This is a per-server setting.", ephemeral=True
            )

        if mode is None and greeting is None:
            current = await company.get(interaction.guild.id)
            alone = len(company.alone_rows(interaction.guild, bot_id=self._bot_id))
            streaming = len(company.stream_rows(interaction.guild))
            sitting = self._connected_channel()
            where = f"<#{sitting.id}>" if sitting is not None else "nowhere right now"
            return await interaction.response.send_message(
                f"**Company in voice:** {current.describe()}\n"
                f"-# Alone in a voice channel right now: {alone} · live or on camera: {streaming}"
                f" · I'm sitting in {where}\n"
                "-# `/company mode:Auto greeting:off` changes it. Discord does not let a bot "
                "watch a stream — I can see that somebody is live, and that is all.",
                ephemeral=True,
            )

        current = await company.get(interaction.guild.id)
        if mode is not None:
            current = await company.set_mode(interaction.guild.id, mode.value) or current
        if greeting is not None:
            current = await company.set_greeting(interaction.guild.id, greeting.value) or current
        await interaction.response.send_message(
            f"✅ **Company in voice:** {current.describe()}\n"
            "-# A channel counts as 'alone' when its only occupant is one person. I arrive after a "
            "short wait, deafened and muted, and leave as soon as somebody real arrives.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    @staticmethod
    def _name(thing) -> str:
        return str(getattr(thing, "name", thing) or "?")

    @staticmethod
    def _log(message: str) -> None:
        """A thing that happened, once. Joins and leaves are rare by design."""
        print(f"[COMPANY] {message}", flush=True)

    def _failure(self, message: str) -> None:
        """A thing that failed, at most once a minute."""
        now = time.time()
        if now - self._last_failure_at < LOG_EVERY:
            return
        self._last_failure_at = now
        print(f"[COMPANY] {message}", flush=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Company(bot))
