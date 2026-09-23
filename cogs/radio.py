"""Radio: one command that carries the knobs, and the two that end it or explain it.

The system this replaces had fourteen commands, and three of them existed only
because it had once been a *player*: ``skip`` (nothing to skip to), ``queue`` (an
always-empty list) and ``filter`` (real-time audio presets that re-render the
stream to hear them). All three are gone, and so is the shape that produced them:
what is left is

* **``/radio play``** — station, language, country, advertising and the 24/7 seat,
  every one of them optional, with the defaults written down in
  :mod:`utils.radio`. Run it with an argument and it is a request; run it with
  none while something is on and it is the *panel* — the card with the buttons,
  which is what somebody typing this command actually wants then.
* **``/radio stop``** — end it, and give the channel back. This is the only thing
  that ends a seat, because the seat is the point: *24/7 until it is stopped*.
* **``/radio doctor``** — what this host can and cannot do for voice, including
  the one reading a capability probe cannot give: whether it can open a stream.

**Who may change what.** Playing is open to any member — it is a shared room, and
every other bot people have used lets them put something on. The three settings
that change what *everyone* hears (the language, the advertising mode, the seat)
are saved only for a caller with **Manage Server**, and a member without it still
gets their choice for the start they asked for. The card says which of the two
happened, because "saved" and "just this once" are the difference between a
setting and a favour.

The two listeners are the gateway half, and they are the reason this file exists
at all rather than everything living in ``utils.radio_stream``: a Discord client
sits in one channel at a time, so a seat has to be *reclaimed* after a restart
(:meth:`Radio.on_ready`) and after the bot is disconnected (:meth:`on_voice_state_update`),
and the alone-timer has to stand down while somebody is listening.
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from utils import db, operator_log, radio_catalogue as catalogue, radio_stream, style, voice_caps

#: The selection policy, under a name of its own: the class below declares the
#: ``/radio`` command **group** as ``radio``, and inside a class body that
#: attribute shadows this import — which is how the first draft referred to
#: ``radio.DEFAULT_LANGUAGE`` on a :class:`~discord.app_commands.Group`.
from utils import radio as policy

#: How many suggestions autocomplete may return. Discord's own ceiling is 25.
SUGGESTIONS = catalogue.SUGGESTIONS


def _unplayable() -> str:
    """The sentence a command answers with when this host cannot play at all."""
    ok, why = voice_caps.build_source()
    return "" if ok else why


class Radio(commands.Cog):
    """The radio: stations, the seat, and the honest readout of this host."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        #: The bot's own view of the players; the module registry is what other
        #: cogs read (Voice Company asks it whether the radio owns the connection).
        self.players = radio_stream.PLAYERS

    # ------------------------------------------------------------------ gate #
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The module gate, where the rest of this bot keeps it."""
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "radio"):
                await interaction.response.send_message(
                    "⚠️ The **Radio** system is currently **turned off** on this "
                    "server.\nAn administrator can re-enable it in the "
                    "[Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True,
                )
                return False
        return True

    def get_player(self, guild_id: int) -> radio_stream.RadioPlayer:
        """This guild's player, made on first use.

        The fault handler is wired here rather than in ``__init__`` so that the
        player has no idea a Discord cog exists — it calls a coroutine, and this
        decides it becomes a card and an operator entry.
        """
        player = self.players.get(int(guild_id))
        if player is None:
            player = radio_stream.RadioPlayer(self.bot, int(guild_id))
            player.on_fault = self._on_fault
        elif player.on_fault is None:
            player.on_fault = self._on_fault
        return player

    async def _ensure_voice(self, interaction: discord.Interaction):
        """Join, or move to, the member's channel. Returns the client or ``None``.

        Permission is checked before anything is attempted: a failed UDP
        handshake is an error, and "I cannot Connect there" is a sentence.
        """
        member_channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if member_channel is None:
            await style.send_card(
                interaction,
                content="🔇 **Join a voice channel first** — I'll follow you into it.",
                ephemeral=True,
            )
            return None

        guild = interaction.guild
        permissions = member_channel.permissions_for(guild.me) if guild else None
        if permissions is not None and not permissions.connect:
            await style.send_card(
                interaction,
                content=f"🚫 I don't have **Connect** permission for {member_channel.mention}.",
                ephemeral=True,
            )
            return None

        vc = guild.voice_client
        if vc is not None and not vc.is_connected():
            # A socket left over from a restart: clear it rather than tripping on it.
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            vc = None
        try:
            if vc is None:
                vc = await member_channel.connect(timeout=20.0, reconnect=True, self_deaf=False)
            elif getattr(vc.channel, "id", None) != member_channel.id:
                await vc.move_to(member_channel)
        except Exception as exc:
            await style.send_card(
                interaction,
                content=f"❌ Could not join {member_channel.mention} (`{type(exc).__name__}`).",
                ephemeral=True,
            )
            return None

        # Voice Company connects deafened and muted, which is right for a companion
        # and wrong for a radio: unmute the connection so the room can see that
        # sound is coming from the bot rather than wondering where it is from.
        if getattr(vc, "self_mute", False) or getattr(vc, "self_deaf", False):
            try:
                await guild.change_voice_state(
                    channel=vc.channel, self_mute=False, self_deaf=False
                )
            except Exception as exc:
                print(f"[RADIO] could not clear the companion's mute: {exc}", flush=True)
        return vc

    # ---------------------------------------------------------------- faults #
    #: How each fault kind reads on the card: (headline, colour, the line under
    #: it). Written out rather than assembled, because the middle column is the
    #: one a reader trusts: the card used to say *"That stream was cut off"*
    #: whatever had happened — including when nothing had been cut off and the
    #: station had simply refused the host, which is a different problem with a
    #: different fix and sent whoever read it looking in the wrong place.
    FAULT_CARDS = {
        "truncated": (
            "✂️ That station was cut off",
            style.WARN,
            "A station has no end, so a stream that stops is the connection dropping "
            "rather than the programme finishing. It was asked again before this card; "
            "if it keeps happening, that station is the problem and not this server.",
        ),
        "unplayable": (
            "⚠️ That station could not be played",
            style.WARN,
            "Nothing was cut off — the address would not open at all, which is usually "
            "the station being down or refusing this host.",
        ),
        "source_error": (
            "🔇 The audio engine failed",
            style.DANGER,
            "ffmpeg or the voice connection stopped. The next start will tell you "
            "whether it has passed.",
        ),
        "opus_missing": (
            "🔇 This host cannot encode audio",
            style.DANGER,
            "The capability probe names the missing piece; nothing will play until it "
            "is there.",
        ),
    }

    async def _on_fault(self, fault: radio_stream.Fault) -> None:
        """A station that died is news for the room *and* for the operator."""
        player = self.players.get(int(getattr(fault, "guild_id", 0) or 0)) or None
        channel = getattr(player, "text_channel", None) if player else None
        headline, colour, advice = self.FAULT_CARDS.get(
            fault.kind,
            (f"⚠️ {fault.headline.capitalize()}", style.WARN, "The radio carried on."),
        )
        if channel is not None:
            try:
                await style.send_card(
                    channel=channel,
                    embed=discord.Embed(
                        title=headline,
                        description=(
                            f"**{fault.title or 'The station'}** — {fault.detail}."
                            f"\n> {advice}"
                        ),
                        color=colour,
                    ),
                    timeout=None,
                )
            except Exception as exc:
                print(f"[RADIO] could not report a fault in the channel: {exc}", flush=True)
        await operator_log.radio_alert(self.bot, fault, player=player)

    # ----------------------------------------------------------------- cards #
    def _panel_embed(self, player: radio_stream.RadioPlayer) -> discord.Embed:
        """The playing card: what is on, where, how loud, and until when."""
        station = player.current
        embed = discord.Embed(
            title=f"{station.icon} {station.name}" if station else "📻 Nothing is playing",
            color=style.ACCENT,
        )
        if station is None:
            return embed
        kind = station.genre
        if station.language:
            kind += f" · {station.language}"
        if station.country:
            kind += f" · {station.capital or station.country}"
        mood = ", ".join(station.tags)
        if mood:
            kind += f" · {mood}"
        embed.description = f"-# {kind}"
        if station.desc:
            embed.add_field(name="About", value=station.desc, inline=False)
        channel = getattr(getattr(player.voice_client, "channel", None), "name", None)
        embed.add_field(name="Room", value=channel or "—", inline=True)
        embed.add_field(
            name="State",
            value="paused" if getattr(player.voice_client, "is_paused", lambda: False)()
            else ("playing" if getattr(player.voice_client, "is_playing", lambda: False)() else "idle"),
            inline=True,
        )
        embed.add_field(name="Volume", value=f"{int(player.volume * 100)}%", inline=True)
        embed.add_field(
            name="Seat",
            value="held until `/radio stop`" if player.stay else "leaves when the room empties",
            inline=True,
        )
        if channel:
            embed.add_field(
                name="Airtime",
                value=f"{radio_stream._mmss(player.status()['position'])}",
                inline=True,
            )
        if player.requester:
            embed.set_footer(text=f"started by {player.requester} · Meow radio")
        return embed

    async def _send_panel(self, interaction, player) -> None:
        """Post the playing card with its controls."""
        try:
            await style.send_card(
                interaction,
                embed=self._panel_embed(player),
                view=ControlsView(self, player),
                timeout=None,
            )
        except Exception as exc:
            print(f"[RADIO] the panel failed to send: {exc}", flush=True)

    @staticmethod
    def _station_card(
        station: catalogue.Station, channel: str, requester: str, note: str = ""
    ) -> str:
        """What the station card says, in the order a reader wants it.

        The descriptor line is the point of a catalogue this size: a station is a
        *genre, a language and a mood* as well as a name, and somebody who does
        not speak the language of what is playing has no other way to find that
        out before they pick it.
        """
        kind = station.genre
        if station.language:
            kind += f" · {station.language}"
        if station.country:
            kind += f" · {station.capital or station.country}"
        mood = ", ".join(station.tags)
        if mood:
            kind += f" · {mood}"
        lines = [
            f"{station.icon} **{station.name}** is live in {channel}.",
            f"-# {kind}",
            f"-# {station.desc} · started by {requester}",
        ]
        if note:
            lines.append(f"-# {note}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- /radio #
    radio = app_commands.Group(
        name="radio",
        description=(
            "24/7 radio: every genre, every language, and a station in every "
            "country the table could verify."
        ),
    )

    @radio.command(
        name="play",
        description="Put a station on — by name, by language, or by country.",
    )
    @app_commands.describe(
        station="A station by name, genre, language or mood — start typing to search",
        language=f"Which language to pick in (default: this server's, else {policy.DEFAULT_LANGUAGE})",
        country="That country's capital-city station",
        ads="How to treat stations that carry advertising (default: ad-free)",
        stay="Hold this channel until /radio stop (default: on)",
        volume="Volume percentage, 5 to 150",
    )
    @app_commands.choices(
        ads=[
            app_commands.Choice(name=f"{key} — {how}", value=key)
            for key, how in policy.ADS_MODES.items()
        ]
    )
    async def play(
        self,
        interaction: discord.Interaction,
        station: str | None = None,
        language: str | None = None,
        country: str | None = None,
        ads: app_commands.Choice[str] | None = None,
        stay: bool | None = None,
        volume: int | None = None,
    ):
        blocked = _unplayable()
        if blocked:
            return await style.send_card(interaction, content=blocked, ephemeral=True)

        player = self.get_player(interaction.guild_id)
        asked_something = any(
            value is not None
            for value in (station, language, country, ads, stay, volume)
        )
        if not asked_something and player.current is not None:
            # The command somebody types while a station is already on is "what is
            # this", not "put something else on" — so it answers with the panel
            # rather than starting a second station over the top of the first.
            await interaction.response.defer()
            return await self._send_panel(interaction, player)

        if volume is not None and not 5 <= int(volume) <= 150:
            return await style.send_card(
                interaction,
                content="❌ Volume must be between `5%` and `150%`.",
                ephemeral=True,
            )

        # The settings this server already has, and whether they were ever set.
        stored_language = await db.get_radio_language(interaction.guild_id) or policy.DEFAULT_LANGUAGE
        stored_ads = await db.get_radio_ads(interaction.guild_id)
        seat, seat_channel = await db.get_radio_stay(interaction.guild_id)
        # No row, or a row with no channel, means this server has never chosen:
        # the default is the radio's own, which is a held seat. A row with a
        # channel and the flag off is a server that stopped the radio, and that
        # answer is honoured.
        default_stay = seat if seat_channel else policy.DEFAULT_STAY

        can_configure = bool(
            getattr(interaction.user.guild_permissions, "manage_guild", False)
            or (
                interaction.guild is not None
                and interaction.user.id == interaction.guild.owner_id
            )
        )
        saved: list[str] = []
        session_only: list[str] = []

        wanted_language = stored_language
        if language is not None:
            typed = language.strip()
            if typed.lower() in ("", policy.LANGUAGE_ANY, "none", "off", "clear"):
                wanted_language = "" if can_configure else stored_language
                if can_configure:
                    await db.set_radio_language(interaction.guild_id, "")
                    saved.append("language · any")
            else:
                resolved = policy.language_name(typed)
                if not resolved:
                    return await style.send_card(
                        interaction,
                        content=(
                            f"❌ The table has no stations in **{typed}**.\n"
                            f"-# {len(catalogue.languages())} languages are in it: "
                            + ", ".join(catalogue.languages())
                            + "."
                        ),
                        ephemeral=True,
                    )
                wanted_language = resolved
                if can_configure:
                    await db.set_radio_language(interaction.guild_id, resolved)
                    saved.append(f"language · {resolved}")
                else:
                    session_only.append(f"language · {resolved}")

        wanted_ads = stored_ads
        if ads is not None:
            wanted_ads = policy.ads_mode(ads.value)
            if can_configure:
                await db.set_radio_ads(interaction.guild_id, wanted_ads)
                saved.append(f"ads · {wanted_ads}")
            else:
                session_only.append(f"ads · {wanted_ads}")

        wanted_stay = default_stay
        if stay is not None:
            wanted_stay = bool(stay)
            if can_configure:
                saved.append(f"seat · {'held' if wanted_stay else 'leaves when empty'}")
            else:
                session_only.append(
                    f"seat · {'held' if wanted_stay else 'leaves when empty'}"
                )

        remembered = ""
        try:
            remembered = await db.get_radio_last(interaction.guild_id)
        except Exception as exc:
            print(f"[RADIO] could not read the remembered station: {exc}", flush=True)

        choice = policy.choose(
            named=station or "",
            remembered=remembered,
            language=wanted_language,
            country=country or "",
            ads=wanted_ads,
        )
        if not choice.ok:
            return await style.send_card(
                interaction, content=policy.refusal_sentence(choice), ephemeral=True
            )

        chosen = choice.station
        await interaction.response.defer()
        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        player.voice_client = vc
        player.text_channel = interaction.channel
        player.stay = wanted_stay
        if volume is not None:
            player.set_volume(int(volume) / 100.0)
        started, why = await player.start(chosen, interaction.user.display_name)
        if not started:
            return await style.send_card(
                interaction, content=f"⚠️ {why}", ephemeral=True
            )

        # What this server last chose, so `/radio play` with no argument is the one
        # it had and not a coin toss. Best-effort: a database that will not take it
        # must not stop the radio, so the failure is logged and the station plays.
        try:
            await db.set_radio_last(interaction.guild_id, chosen.key)
        except Exception as exc:
            print(
                f"[RADIO] could not remember the station for {interaction.guild_id}: {exc}",
                flush=True,
            )
        if wanted_stay:
            # The seat is armed by the start, and written down so a restart can
            # reclaim it — that is what "24/7 until stopped" needs to survive a
            # deploy, which is the only time it would otherwise be lost.
            player.cancel_alone_timer()
            try:
                await db.set_radio_stay(interaction.guild_id, getattr(vc.channel, "id", 0) or 0, True)
            except Exception as exc:
                print(f"[RADIO] could not record the seat: {exc}", flush=True)

        note = policy.reason_sentence(choice, language=wanted_language)
        if choice.note:
            note = f"{note} · {choice.note}" if note else f"-# {choice.note}"
        if saved:
            note = f"{note} · saved for this server: {', '.join(saved)}" if note else f"-# saved for this server: {', '.join(saved)}"
        if session_only:
            note = (
                f"{note} · this start only (Manage Server saves it): {', '.join(session_only)}"
                if note
                else f"-# this start only (Manage Server saves it): {', '.join(session_only)}"
            )

        await style.send_card(
            interaction,
            content=self._station_card(
                chosen, vc.channel.mention, interaction.user.display_name, note=note
            ),
            view=ControlsView(self, player),
            timeout=None,
        )

    @play.autocomplete("station")
    async def _station_autocomplete(self, interaction: discord.Interaction, current: str):
        """Every station, searchable by name, genre, language, country or mood.

        Autocomplete rather than ``app_commands.choices`` for two reasons, and the
        second is the one that matters: Discord caps a command's fixed choices at
        25, and it renders them in the order they were declared — so a menu is a
        catalogue nobody can find *Turkish* in. A search box over the words people
        actually use is what makes a list this size usable.
        """
        return [
            app_commands.Choice(name=catalogue.option_label(row), value=row.key)
            for row in catalogue.search(current)
        ]

    @play.autocomplete("language")
    async def _language_autocomplete(self, interaction: discord.Interaction, current: str):
        """Every language the table holds, plus the way to stop asking for one."""
        typed = (current or "").strip().lower()
        choices = [
            app_commands.Choice(name=f"🗣️ {name}", value=name)
            for name in catalogue.languages()
            if not typed or typed in name.lower()
        ]
        if not typed or policy.LANGUAGE_ANY.startswith(typed):
            choices.insert(0, app_commands.Choice(name="➖ any language", value=policy.LANGUAGE_ANY))
        return choices[:SUGGESTIONS]

    @play.autocomplete("country")
    async def _country_autocomplete(self, interaction: discord.Interaction, current: str):
        """Every country the table can actually play, capital first in the label."""
        typed = (current or "").strip().lower()
        out: list[app_commands.Choice] = []
        for name in catalogue.countries():
            if typed and typed not in name.lower():
                continue
            rows = catalogue.in_country(name)
            capital = rows[0].capital if rows else ""
            out.append(
                app_commands.Choice(
                    name=(f"🌍 {name} — {capital}" if capital else f"🌍 {name}")[:100],
                    value=name,
                )
            )
            if len(out) >= SUGGESTIONS:
                break
        return out

    @radio.command(name="stop", description="Stop the radio and give the channel back.")
    async def stop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if not player.voice_client or not player.voice_client.is_connected():
            return await style.send_card(
                interaction, content="❌ I'm not in a voice channel.", ephemeral=True
            )
        channel_id = getattr(player.voice_client.channel, "id", 0) or 0
        station = player.current
        player.stay = False
        await player.stop(reason="stopped by a member")
        try:
            # The seat is off until somebody starts the radio again, and that is
            # written down: a restart must not reclaim a channel this command gave
            # back.
            await db.set_radio_stay(interaction.guild_id, channel_id, False)
        except Exception as exc:
            print(f"[RADIO] could not clear the seat: {exc}", flush=True)
        await style.send_card(
            interaction,
            content=(
                f"⏹️ **{station.name}** stopped, and I've left the channel."
                if station
                else "⏹️ Stopped, and I've left the channel."
            ),
            ephemeral=True,
        )

    @radio.command(
        name="doctor",
        description="What this host can and cannot do for voice (administrator).",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def doctor(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        caps = voice_caps.probe(refresh=True)
        ok, why = voice_caps.build_source(caps)
        # The one question a capability probe cannot answer and a version string
        # never could: whether this host can *open* a stream. ffmpeg installed,
        # libopus loaded and every URL in the tree healthy is all true on a box
        # with no route to the outside world.
        probe = await asyncio.to_thread(radio_stream.self_check)
        index = catalogue.index()
        lines = [
            f"**Playback** · {'yes' if caps.playable else 'no'}",
            f"**ffmpeg** · `{caps.ffmpeg or 'missing'}` ({caps.ffmpeg_source})",
            f"**libopus** · `{caps.opus_source}` — {'live volume' if caps.live_volume else 'volume is baked into the stream'}",
            f"**DAVE** · `{caps.dave_version or 'missing'}`",
            f"**Streams** · {probe.sentence()}",
            f"**Stations** · {index.stations} across {len(index.genres)} genres and "
            f"{len(index.languages)} languages",
            f"**Countries** · {len(index.countries)} with a verified station of their own",
            "**Provider** · none — a station is a direct stream and asks nobody for anything",
        ]
        if probe.detail:
            lines.append(f"-# opened `{probe.target}` — {probe.detail}")
        if not probe.healthy:
            # The remedy differs by *kind*, which is why the two are not folded
            # into one sentence: no route out is an operator's problem at the
            # network layer, and a station refusing the request is not.
            lines.append(
                "\n-# Every station plays over the same path, so this is the host rather "
                "than the station."
                if probe.unreachable
                else "\n-# This one station refused the request; another may still play."
            )
        if why:
            lines.append(f"\n{why}")
        await style.send_card(
            interaction,
            content="🎛️ **What this host can do**\n" + "\n".join(lines),
            ephemeral=True,
        )

    # ------------------------------------------------------------- listeners #
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Two jobs: keep the seat, and leave a room once it is empty."""
        if member.id == getattr(getattr(self.bot, "user", None), "id", None):
            if before.channel is not None and after.channel is None:
                player = self.players.get(int(before.channel.guild.id))
                if player is not None and player.stay and not player.manual_stop:
                    await asyncio.sleep(3)
                    try:
                        channel = before.channel.guild.get_channel(before.channel.id) or before.channel
                        player.voice_client = await channel.connect(
                            timeout=15.0, reconnect=True, self_deaf=False
                        )
                        print(f"[RADIO] reclaimed the seat in {channel.name}", flush=True)
                    except Exception as exc:
                        print(f"[RADIO] seat reclaim failed: {exc}", flush=True)
            return

        guild = getattr(member, "guild", None)
        if guild is None:
            return
        player = self.players.get(int(guild.id))
        if player is None or not player.voice_client or not player.voice_client.channel:
            return
        channel = player.voice_client.channel
        if before.channel != channel and after.channel != channel:
            return
        humans = [
            m for m in getattr(channel, "members", ()) or () if not getattr(m, "bot", False)
        ]
        if humans:
            player.cancel_alone_timer()
        elif not player.stay:
            player.start_alone_timer()

    @commands.Cog.listener()
    async def on_ready(self):
        """Reclaim the seats this deployment promised, after a restart."""
        await self.bot.wait_until_ready()
        try:
            records = await db.radio_seats()
        except Exception as exc:
            print(f"[RADIO] could not read the seats: {exc}", flush=True)
            return
        for guild_id, channel_id in records:
            guild = self.bot.get_guild(int(guild_id))
            if guild is None or guild.voice_client is not None:
                continue
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                continue
            try:
                player = self.get_player(guild.id)
                player.voice_client = await channel.connect(
                    timeout=15.0, reconnect=True, self_deaf=False
                )
                player.stay = True
                print(f"[RADIO] reclaimed the seat in {guild.name}/#{channel.name}", flush=True)
            except Exception as exc:
                print(f"[RADIO] could not reclaim the seat in {guild.name}: {exc}", flush=True)


class ControlsView(discord.ui.View):
    """Buttons under a playing card. The same actions the commands offer.

    Legacy rather than Components V2 on purpose: an interactive view carries a
    timeout and an ``interaction_check``, and merging one into a V2 layout drops
    both — which is the rule ``utils/style`` documents and the rest of the tree
    follows.
    """

    def __init__(self, cog: Radio, player: radio_stream.RadioPlayer):
        super().__init__(timeout=None)
        self.cog = cog
        self.player = player

    def _connected(self) -> bool:
        vc = self.player.voice_client
        return bool(vc and vc.is_connected())

    @discord.ui.button(label="Pause", emoji="⏯️", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not self._connected():
            return await interaction.response.send_message(
                "❌ I'm not in a voice channel.", ephemeral=True
            )
        if vc.is_playing():
            vc.pause()
            return await interaction.response.send_message("⏸️ Paused.", ephemeral=True)
        if vc.is_paused():
            vc.resume()
            return await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Louder", emoji="🔊", style=discord.ButtonStyle.secondary)
    async def louder(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._connected():
            return await interaction.response.send_message(
                "❌ I'm not in a voice channel.", ephemeral=True
            )
        player = self.player
        stepped = min(150, int(round(player.volume * 100)) + 10)
        _live, applied = player.set_volume(stepped / 100.0)
        await interaction.response.send_message(
            f"🔊 Volume **{int(applied * 100)}%**.", ephemeral=True
        )

    @discord.ui.button(label="Quieter", emoji="🔉", style=discord.ButtonStyle.secondary)
    async def quieter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._connected():
            return await interaction.response.send_message(
                "❌ I'm not in a voice channel.", ephemeral=True
            )
        player = self.player
        stepped = max(5, int(round(player.volume * 100)) - 10)
        _live, applied = player.set_volume(stepped / 100.0)
        await interaction.response.send_message(
            f"🔉 Volume **{int(applied * 100)}%**.", ephemeral=True
        )

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._connected():
            return await interaction.response.send_message(
                "❌ I'm not in a voice channel.", ephemeral=True
            )
        player = self.player
        channel_id = getattr(player.voice_client.channel, "id", 0) or 0
        player.stay = False
        await player.stop(reason="stopped from the controls")
        try:
            await db.set_radio_stay(interaction.guild_id, channel_id, False)
        except Exception as exc:
            print(f"[RADIO] could not clear the seat: {exc}", flush=True)
        await interaction.response.send_message(
            "⏹️ Stopped, and I've left the channel.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Radio(bot))
