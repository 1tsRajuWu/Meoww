"""Music & radio: `/play`, one `/music` group, and no guesses about what was asked for.

The command surface is deliberate, and it is the reason this is a rebuild rather
than a restoration. Discord allows an application **100 top-level commands per
scope** and this bot already publishes most of that budget, so the archived cog's
twelve roots — `/play`, `/skip`, `/pause`, `/resume`, `/stop`, `/queue`,
`/nowplaying`, `/volume`, `/filter`, `/radio`, `/247`, `/songinfo`, `/lyrics` —
would not fit. `/play` keeps its own slot because it is the one name people type
from memory; everything else lives under `/music`, which costs **one** slot.

All the logic is in two modules — :mod:`utils.media_source` turns words into one
specific recording, :mod:`utils.player` plays it — so this file is commands,
cards and the two Discord-only jobs (the voice-state listener and the 24/7
reconnect). The distinction matters for a feature that was retired once for being
untestable: everything except the gateway half is exercised without one.

Three things it says out loud instead of failing quietly, because each of them
was a real complaint about the archived version:

* **"The music is short."** A stream that dies reaches the player as a finished
  track, so the queue used to move on mid-song with nothing said. A truncated
  stream now posts a card naming the track, how much of it played and what the
  provider claimed — and it goes to the operator as well, because a provider
  cutting this host off is a fact about the deployment.
* **"It plays a different song."** When the resolver cannot separate two results
  it returns both, and the member is asked with a menu instead of being given a
  confident wrong answer.
* **"Music is broken here."** Every command checks whether this host can play at
  all *before* joining a channel, and names the missing piece when it cannot.
"""
from __future__ import annotations

import asyncio
import random
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils import db, media_source, operator_log, style, voice_caps
from utils import player as player_module
from utils.card import generate_now_playing_card

#: The stations. Direct streams, so a radio session never touches the extractor
#: and keeps working on a host the media platforms have blocked — which is the
#: one thing that still works when everything else is having a bad day.
RADIO_STATIONS: dict[str, dict] = {
    "lofi": {
        "name": "24/7 Lo-Fi Chill Beats",
        "url": "http://ice1.somafm.com/groovesalad-128-mp3",
        "desc": "Smooth, relaxing lofi and ambient downtempo beats.",
        "icon": "☕",
    },
    "gaming": {
        "name": "Gaming EDM & Beats",
        "url": "https://streams.ilovemusic.de/iloveradio2.mp3",
        "desc": "High-octane electronic beats and gaming anthems.",
        "icon": "🎮",
    },
    "synthwave": {
        "name": "Nightwave Plaza",
        "url": "https://radio.plaza.one/mp3",
        "desc": "80s neon synthwave, future funk and vaporwave.",
        "icon": "🌃",
    },
    "anime": {
        "name": "Anime & J-Pop",
        "url": "https://listen.moe/stream",
        "desc": "Anime OSTs, J-Pop and vocaloid, direct from listen.moe.",
        "icon": "🌸",
    },
    "jazz": {
        "name": "Coffeehouse Jazz",
        "url": "https://stream.zeno.fm/0r0xa792kwzuv",
        "desc": "Mellow cafe jazz, acoustic piano, vintage saxophone.",
        "icon": "🎷",
    },
    "pop": {
        "name": "Top 40 Pop",
        "url": "https://streams.ilovemusic.de/iloveradio1.mp3",
        "desc": "Today's chart-toppers and dance-pop.",
        "icon": "📻",
    },
}

#: How long a member has to answer "which one did you mean?" before the question
#: is abandoned. Long enough to read three titles, short enough that a stale menu
#: is not sitting in a channel an hour later.
CHOICE_TIMEOUT = 90.0

#: One search at a time per guild: two simultaneous extractions on a free host is
#: how a five-second search becomes a thirty-second one.
_GUILD_LOCKS: dict[int, asyncio.Lock] = {}


def _lock(guild_id: int) -> asyncio.Lock:
    return _GUILD_LOCKS.setdefault(int(guild_id), asyncio.Lock())


def _mmss(seconds: float | int) -> str:
    return player_module.format_duration(int(seconds or 0))


class Music(commands.Cog):
    """Playback, radio and the honest readout of what this host can do."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        #: The bot's own view of the players; the module registry is what other
        #: cogs read (Voice Company asks it whether music owns the connection).
        self.players = player_module.PLAYERS

    # ------------------------------------------------------------------ gate #
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The module gate, where the rest of this bot keeps it."""
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "music"):
                await interaction.response.send_message(
                    "⚠️ The **Music & Radio** system is currently **turned off** on this "
                    "server.\nAn administrator can re-enable it in the "
                    "[Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True,
                )
                return False
        return True

    def get_player(self, guild_id: int) -> player_module.GuildPlayer:
        """This guild's player, made on first use.

        The fault handler is wired here rather than in ``__init__`` so that the
        player has no idea a Discord cog exists — it calls a coroutine, and this
        decides it becomes a card and an operator entry.
        """
        player = self.players.get(int(guild_id))
        if player is None:
            player = player_module.GuildPlayer(self.bot, int(guild_id))
            player.on_fault = self._on_fault
        elif player.on_fault is None:
            player.on_fault = self._on_fault
        return player

    # --------------------------------------------------------------- helpers #
    def _unplayable(self) -> str:
        """The sentence a command answers with when this host cannot play."""
        ok, why = voice_caps.build_source()
        return "" if ok else why

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
        # and wrong for a player: unmute the connection so the room can see that
        # sound is coming from the bot rather than wondering where it is from.
        if getattr(vc, "self_mute", False) or getattr(vc, "self_deaf", False):
            try:
                await guild.change_voice_state(channel=vc.channel, self_mute=False, self_deaf=False)
            except Exception as exc:
                print(f"[MUSIC] could not clear the companion's mute: {exc}", flush=True)
        return vc

    #: How each fault kind reads on the card: (emoji + headline, colour, the line
    #: under it). Written out rather than assembled, because the middle column is
    #: the one a reader trusts: the card used to say *"That stream was cut off"*
    #: whatever had happened — including when nothing had been cut off at all and
    #: the provider had simply refused to hand over a stream, which is a different
    #: problem with a different fix and sent whoever read it looking in the wrong
    #: place. ``Fault.headline`` has named the kinds all along; this is the card
    #: finally using it.
    FAULT_CARDS = {
        "truncated": (
            "✂️ That stream was cut off",
            style.WARN,
            "The queue carried on. If it keeps happening, it is the provider cutting "
            "this host off rather than one bad link.",
        ),
        "unplayable": (
            "⚠️ That track could not be played",
            style.WARN,
            "The queue carried on. Nothing was cut off — the provider would not hand "
            "over a stream at all, which is usually the host rather than the link.",
        ),
        "blocked": (
            "🚫 The provider refused this host",
            style.DANGER,
            "This is a host-level refusal rather than a bad link, so it will affect "
            "other tracks too. `/music doctor` says what the extractor can reach.",
        ),
        "source_error": (
            "🔇 The audio engine failed",
            style.DANGER,
            "ffmpeg or the voice connection stopped. The next track will tell you "
            "whether it has passed.",
        ),
        "opus_missing": (
            "🔇 This host cannot encode audio",
            style.DANGER,
            "The capability probe names the missing piece; nothing will play until "
            "it is there.",
        ),
        # The good news one, and the only entry here that is not a failure: it is
        # an informational card so that a *substituted* upload is never a silent
        # surprise — a different recording arriving unexplained is the wrong-song
        # bug wearing a different hat.
        "rescued": (
            "🔄 Playing the same track from another provider",
            style.INFO,
            "The provider that found it would not stream it, so it is playing from "
            "the other one. Nothing to do — this is the bot working around a host "
            "refusal rather than a broken request.",
        ),
    }

    async def _on_fault(self, fault: player_module.Fault) -> None:
        """A track that died is news for the room *and* for the operator.

        A *substitution* is news for the room only, and it is not a fault: the
        operator's music alert is about things that went wrong, and a track that
        is playing is not one of them.
        """
        player = self.players.get(int(getattr(fault, "guild_id", 0) or 0)) or None
        channel = getattr(player, "text_channel", None) if player else None
        emoji_headline, colour, advice = self.FAULT_CARDS.get(
            fault.kind,
            (f"⚠️ {fault.headline.capitalize()}", style.WARN, "The queue carried on."),
        )
        if fault.kind == "rescued":
            emoji_headline = "🔄 Playing the same track from another provider"
        if channel is not None:
            try:
                await style.send_card(
                    channel,
                    embed=discord.Embed(
                        title=emoji_headline,
                        description=(
                            f"**{fault.title or 'The current track'}** — {fault.detail}."
                            f"\n> {advice}"
                        ),
                        color=colour,
                    ),
                    timeout=None,
                )
            except Exception as exc:
                print(f"[MUSIC] could not report a fault in the channel: {exc}", flush=True)
        if fault.kind == "rescued":
            return
        await operator_log.music_alert(self.bot, fault, player=player)

    async def _resolve_in_executor(self, guild_id, query: str, requester: str):
        """Search off the event loop, one search at a time per guild.

        Two simultaneous extractions on a free host is how a five-second search
        becomes a thirty-second one, and it is the sort of load that gets an
        address noticed in the first place.
        """
        async with _lock(guild_id or 0):
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, lambda: media_source.resolve(query, requester=requester)
            )

    async def _enqueue_and_play(self, interaction, player, track, *, announce: bool = True):
        """Put a track in the queue, start it if nothing is playing, and say so."""
        if len(player.queue) >= player_module.MAX_QUEUE:
            return await style.send_card(
                interaction,
                content=f"📚 The queue is full ({player_module.MAX_QUEUE} tracks).",
                ephemeral=True,
            )
        player.text_channel = interaction.channel
        player.queue.append(track)
        playing = bool(
            player.voice_client
            and (player.voice_client.is_playing() or player.voice_client.is_paused())
        )
        if not playing:
            await player.play_next()
            if announce:
                await self._send_now_playing(interaction, player)
        elif announce:
            position = len(player.queue) + (1 if player.current else 0)
            embed = discord.Embed(
                title="➕ Queued",
                description=(
                    f"**{track.title}**\n"
                    f"-# {_mmss(track.duration)} · {interaction.user.display_name} · "
                    f"position #{position}"
                ),
                color=style.SUCCESS,
            )
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
            await style.send_card(interaction, embed=embed)

    # ---------------------------------------------------------------- cards #
    async def _now_playing_embed(self, player) -> tuple[discord.Embed, object | None]:
        """The playing card, with the artwork when it can be drawn.

        The picture is drawn locally from the provider's own thumbnail because a
        hotlink in a card is a broken image the moment a CDN changes its mind —
        and drawing it is already what ``utils/card`` does for the rank card.
        """
        track = player.current
        embed = discord.Embed(color=style.ACCENT)
        if track is None:
            return embed
        title = track.title
        artist = ""
        if " - " in title:
            artist, title = title.split(" - ", 1)
        try:
            buffer = await generate_now_playing_card(
                title=title,
                artist=artist or ("Radio" if track.is_radio else "Unknown artist"),
                duration_str="live" if track.is_radio else _mmss(track.duration),
                requester=track.requester,
                thumbnail_url=track.thumbnail or "",
            )
            embed.set_image(url="attachment://now_playing.png")
            embed.add_field(name="Playing", value=f"**{track.title}**", inline=False)
            embed.add_field(name="Engine", value=f"`{player.engine or 'starting'}`", inline=True)
            embed.add_field(name="Volume", value=f"`{int(player.volume * 100)}%`", inline=True)
            embed.add_field(name="Queue", value=f"`{len(player.queue)}`", inline=True)
            embed.set_footer(text="Meow · now playing")
            return embed, buffer
        except Exception as exc:
            print(f"[MUSIC] could not draw the now-playing card: {exc}", flush=True)

        embed.title = track.title
        embed.description = (
            f"-# {'live stream' if track.is_radio else _mmss(track.duration)} · "
            f"requested by {track.requester}\n"
            f"-# engine `{player.engine or 'starting'}` · volume `{int(player.volume * 100)}%` · "
            f"{len(player.queue)} queued"
        )
        if track.webpage_url:
            embed.url = track.webpage_url
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        return embed, None

    async def _send_now_playing(self, interaction, player) -> None:
        """Post the now-playing card with its controls."""
        embed, buffer = await self._now_playing_embed(player)
        kwargs = {"embed": embed, "view": ControlsView(self, player), "timeout": None}
        if buffer is not None:
            kwargs["file"] = discord.File(buffer, "now_playing.png")
        try:
            await style.send_card(interaction, **kwargs)
        except Exception as exc:
            print(f"[MUSIC] the now-playing card failed to send: {exc}", flush=True)

    # ---------------------------------------------------------------- /play #
    @app_commands.command(name="play", description="Play a song, an album, or a link — I'll search and queue it.")
    @app_commands.describe(query="A song title, an artist, or a link (YouTube, SoundCloud, Spotify)")
    async def play(self, interaction: discord.Interaction, query: str):
        blocked = self._unplayable()
        if blocked:
            return await style.send_card(interaction, content=blocked, ephemeral=True)
        await interaction.response.defer()
        vc = await self._ensure_voice(interaction)
        if vc is None:
            return
        player = self.get_player(interaction.guild_id)
        player.voice_client = vc

        try:
            resolution = await self._resolve_in_executor(
                interaction.guild_id, query, interaction.user.display_name
            )
        except media_source.MediaError as exc:
            return await style.send_card(interaction, content=f"❌ {exc.message}", ephemeral=False)
        except Exception as exc:
            print(f"[MUSIC] resolve failed for {query!r}: {type(exc).__name__}: {exc}", flush=True)
            if media_source.is_blocked(exc):
                await operator_log.music_alert(
                    self.bot,
                    player_module.Fault(
                        kind="blocked",
                        detail="the provider refused this host's address",
                        title=query,
                    ),
                    player=player,
                )
                return await style.send_card(
                    interaction,
                    content=(
                        "🚫 **The provider refused this host.** That is a fact about where "
                        "the bot runs, not about your search — a direct link or "
                        "`/music radio` still works.\n"
                        "-# An operator can add `YTDL_COOKIES` or a PO token to fix it."
                    ),
                )
            return await style.send_card(interaction, content="❌ That search failed. Try a direct link.")

        if resolution.alternatives and resolution.track is None:
            return await style.send_card(
                interaction,
                content=(
                    "🤔 **I'm not sure which of these you meant** — pick one and I'll play it."
                ),
                view=TrackChoiceView(self, resolution, requester=interaction.user.display_name),
            )
        if resolution.track is None:
            return await style.send_card(
                interaction, content=f"❌ Nothing usable matched **{query}**.", ephemeral=False
            )
        track = resolution.track

        # An album or a playlist link queues the rest of itself, matched when each
        # track's turn comes rather than all at search time.
        if media_source.is_spotify(query):
            try:
                for extra in media_source.queued_from_spotify(query, requester=interaction.user.display_name)[:49]:
                    player.queue.append(extra)
            except Exception as exc:
                print(f"[MUSIC] could not queue the rest of the playlist: {exc}", flush=True)

        await self._enqueue_and_play(interaction, player, track)

    # ------------------------------------------------------------- /music 📻 #
    music = app_commands.Group(name="music", description="Everything else the music player does.")

    @music.command(name="radio", description="Stream a 24/7 radio station — no search, no provider, just music.")
    @app_commands.describe(station="Which station")
    @app_commands.choices(
        station=[
            app_commands.Choice(name="☕ Lo-Fi Chill Beats", value="lofi"),
            app_commands.Choice(name="🎮 Gaming EDM & Beats", value="gaming"),
            app_commands.Choice(name="🌃 Nightwave Plaza (synthwave)", value="synthwave"),
            app_commands.Choice(name="🌸 Anime & J-Pop", value="anime"),
            app_commands.Choice(name="🎷 Coffeehouse Jazz", value="jazz"),
            app_commands.Choice(name="📻 Top 40 Pop", value="pop"),
        ]
    )
    async def radio(self, interaction: discord.Interaction, station: app_commands.Choice[str]):
        blocked = self._unplayable()
        if blocked:
            return await style.send_card(interaction, content=blocked, ephemeral=True)
        await interaction.response.defer()
        vc = await self._ensure_voice(interaction)
        if vc is None:
            return
        chosen = RADIO_STATIONS.get(station.value, RADIO_STATIONS["lofi"])
        player = self.get_player(interaction.guild_id)
        player.voice_client = vc
        player.text_channel = interaction.channel
        is_247, _ = await db.get_247_setting(interaction.guild_id)
        player.is_247 = is_247

        player.queue.clear()
        track = media_source.Track(
            title=f"{chosen['icon']} {chosen['name']}",
            provider="radio",
            is_radio=True,
            duration=0,
            requester=interaction.user.display_name,
        )
        # The stream URL is known already, so it is recorded as resolved *now*:
        # a radio entry never needs the extractor, which is what keeps it working
        # when the media platforms are refusing this host.
        track.stream_url = chosen["url"]
        track.resolved_at = time.monotonic()
        track.is_opus = False
        player.queue.append(track)

        # Let the stop's own after-callback do the advancing. Doing both produced
        # two advances, the second of which found an empty queue and cleared
        # ``current`` — after which /nowplaying and /skip denied the radio was on.
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        else:
            await player.play_next()

        await style.send_card(
            interaction,
            content=(
                f"{chosen['icon']} **{chosen['name']}** is live in {vc.channel.mention}.\n"
                f"-# {chosen['desc']} · started by {interaction.user.display_name}"
            ),
            view=ControlsView(self, player),
            timeout=None,
        )

    @music.command(name="skip", description="Skip to the next track.")
    async def skip(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if not player.skip():
            return await style.send_card(interaction, content="❌ Nothing is playing.", ephemeral=True)
        title = player.current.title if player.current else "that track"
        await style.send_card(interaction, content=f"⏭️ Skipped **{title}**.", ephemeral=True)

    @music.command(name="pause", description="Pause playback.")
    async def pause(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        vc = player.voice_client
        if not vc or not vc.is_connected() or not vc.is_playing():
            return await style.send_card(interaction, content="❌ Nothing is playing.", ephemeral=True)
        vc.pause()
        await style.send_card(interaction, content="⏸️ Paused.", ephemeral=True)

    @music.command(name="resume", description="Resume playback.")
    async def resume(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        vc = player.voice_client
        if not vc or not vc.is_connected() or not vc.is_paused():
            return await style.send_card(interaction, content="❌ Nothing is paused.", ephemeral=True)
        vc.resume()
        await style.send_card(interaction, content="▶️ Resumed.", ephemeral=True)

    @music.command(name="stop", description="Stop, clear the queue, and leave — unless 24/7 mode is on.")
    async def stop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if not player.voice_client or not player.voice_client.is_connected():
            return await style.send_card(interaction, content="❌ I'm not in a voice channel.", ephemeral=True)
        staying = player.is_247
        await player.stop(reason="stopped by a member")
        await style.send_card(
            interaction,
            content=(
                "⏹️ Stopped and cleared the queue.\n> 💡 Staying in voice — **24/7** is on."
                if staying
                else "⏹️ Stopped, cleared the queue, and left the channel."
            ),
            ephemeral=True,
        )

    @music.command(name="queue", description="Show what is playing and what is next.")
    async def queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if player.current is None and not player.queue:
            return await style.send_card(interaction, content="📭 Nothing is queued.", ephemeral=True)
        lines = []
        if player.current is not None:
            lines.append(f"**▶️ Now** · {player.current.title} `{_mmss(player.current.duration)}`")
        for index, track in enumerate(player.queue[:10], start=1):
            lines.append(f"`{index}.` {track.title} `{_mmss(track.duration)}`")
        if len(player.queue) > 10:
            lines.append(f"-# …and {len(player.queue) - 10} more")
        panel = style.card(
            title="📜 Queue",
            body="\n".join(lines),
            accent=style.ACCENT,
            footer=(
                f"{len(player.queue)} queued · loop {player.loop} · volume {int(player.volume * 100)}%"
            ),
            timeout=None,
        )
        await style.send_panel(interaction, panel)

    @music.command(name="nowplaying", description="Show the current track and its controls.")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if player.current is None:
            return await style.send_card(interaction, content="❌ Nothing is playing.", ephemeral=True)
        await interaction.response.defer()
        await self._send_now_playing(interaction, player)

    @music.command(name="volume", description="Set the volume (5% to 150%).")
    @app_commands.describe(percent="Volume percentage")
    async def volume(self, interaction: discord.Interaction, percent: int):
        if percent < 5 or percent > 150:
            return await style.send_card(
                interaction, content="❌ Volume must be between `5%` and `150%`.", ephemeral=True
            )
        player = self.get_player(interaction.guild_id)
        live, applied = player.set_volume(percent / 100.0)
        note = "" if live else "\n> Saved — it starts from the next track."
        await style.send_card(
            interaction, content=f"🔊 Volume set to **{int(applied * 100)}%**.{note}", ephemeral=True
        )

    @music.command(name="filter", description="Apply a real-time audio effect.")
    @app_commands.describe(effect="Which effect")
    @app_commands.choices(
        effect=[
            app_commands.Choice(name="🎵 Normal (studio clean)", value="normal"),
            app_commands.Choice(name="🔊 Bass boost", value="bassboost"),
            app_commands.Choice(name="🌙 Nightcore", value="nightcore"),
            app_commands.Choice(name="🌊 Vaporwave", value="vaporwave"),
            app_commands.Choice(name="🎧 8D spatial audio", value="8d"),
            app_commands.Choice(name="🎤 Vocal boost", value="pop"),
        ]
    )
    async def filter_cmd(self, interaction: discord.Interaction, effect: str):
        player = self.get_player(interaction.guild_id)
        if not player.voice_client or not player.voice_client.is_connected():
            return await style.send_card(interaction, content="❌ I'm not in a voice channel.", ephemeral=True)
        applied, chosen = player.set_filter(effect)
        note = "" if applied else "\n> Saved — it applies to the next track."
        await style.send_card(
            interaction,
            content=f"{chosen.emoji} **{chosen.name}** applied.\n-# {chosen.description}{note}",
        )

    @music.command(name="loop", description="Repeat the track, the queue, or nothing.")
    @app_commands.describe(mode="What to repeat")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="This track", value="track"),
            app_commands.Choice(name="The whole queue", value="queue"),
        ]
    )
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        player = self.get_player(interaction.guild_id)
        player.loop = mode.value
        await style.send_card(interaction, content=f"🔁 Loop is **{mode.name}**.", ephemeral=True)

    @music.command(name="shuffle", description="Shuffle the queue.")
    async def shuffle(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if len(player.queue) < 2:
            return await style.send_card(interaction, content="📭 Nothing to shuffle.", ephemeral=True)
        random.shuffle(player.queue)
        await style.send_card(
            interaction, content=f"🔀 Shuffled **{len(player.queue)}** tracks.", ephemeral=True
        )

    @music.command(name="seek", description="Jump to a position in the current track.")
    @app_commands.describe(seconds="How many seconds in")
    async def seek(self, interaction: discord.Interaction, seconds: int):
        player = self.get_player(interaction.guild_id)
        applied, position = player.seek(seconds)
        if not applied:
            return await style.send_card(
                interaction, content="❌ There is nothing to seek, or that is past the end.", ephemeral=True
            )
        await style.send_card(interaction, content=f"⏩ Jumped to **{_mmss(position)}**.", ephemeral=True)

    @music.command(name="247", description="Keep the bot in this voice channel permanently (server owner).")
    @app_commands.describe(channel="Channel to stay in (defaults to the current one)")
    async def mode_247(self, interaction: discord.Interaction, channel: discord.VoiceChannel | None = None):
        is_guild_owner = bool(interaction.guild and interaction.user.id == interaction.guild.owner_id)
        is_bot_owner = False
        try:
            is_bot_owner = await self.bot.is_owner(interaction.user)
        except Exception:
            is_bot_owner = False
        if not (is_guild_owner or is_bot_owner):
            return await style.send_card(
                interaction,
                content="❌ Only the **server owner** can switch 24/7 mode — it keeps the bot in a channel indefinitely.",
                ephemeral=True,
            )

        player = self.get_player(interaction.guild_id)
        enabled, saved_channel = await db.get_247_setting(interaction.guild_id)
        if enabled:
            player.is_247 = False
            await db.set_247_setting(interaction.guild_id, 0, False)
            if player.voice_client and not (
                player.voice_client.is_playing() or player.voice_client.is_paused()
            ):
                player.start_inactivity_timer()
            return await style.send_card(
                interaction,
                content=(
                    "⏹️ **24/7 mode is off.**\n"
                    "-# I'll leave after five minutes of silence or an empty channel."
                ),
            )

        target = channel
        if target is None and player.voice_client and player.voice_client.channel:
            target = player.voice_client.channel
        if target is None:
            target = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if target is None:
            return await style.send_card(
                interaction,
                content="❌ Join a voice channel first, or name one: `/music 247 channel:#room`.",
                ephemeral=True,
            )

        vc = interaction.guild.voice_client
        try:
            if vc is None or not vc.is_connected():
                vc = await target.connect(timeout=15.0, reconnect=True, self_deaf=False)
            elif getattr(vc.channel, "id", None) != target.id:
                await vc.move_to(target)
        except Exception as exc:
            return await style.send_card(
                interaction, content=f"❌ Could not connect to {target.mention} (`{type(exc).__name__}`).", ephemeral=True
            )
        player.voice_client = vc
        player.text_channel = interaction.channel
        player.is_247 = True
        player.cancel_inactivity_timer()
        player.cancel_alone_timer()
        await db.set_247_setting(interaction.guild_id, target.id, True)
        await style.send_card(
            interaction,
            content=(
                f"👑 **24/7 mode is on** — I'll stay in {target.mention} through restarts.\n"
                f"-# set by {interaction.user.display_name} · auto-disconnect disabled"
            ),
        )

    @music.command(name="songinfo", description="Look up a track's artwork, album, genre and release date.")
    @app_commands.describe(query="Song title or artist")
    async def songinfo(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        payload = await self._itunes(query)
        if not payload:
            return await style.send_card(
                interaction, content=f"❌ No tracks found for **{query}**.", ephemeral=True
            )
        embed = discord.Embed(
            title=f"🎵 {payload.get('trackName', 'Unknown')}",
            url=payload.get("trackViewUrl"),
            color=style.ACCENT,
        )
        artwork = (payload.get("artworkUrl100") or "").replace("100x100bb.jpg", "600x600bb.jpg")
        if artwork:
            embed.set_thumbnail(url=artwork)
        embed.add_field(name="Artist", value=f"`{payload.get('artistName', '—')}`", inline=True)
        embed.add_field(name="Album", value=f"`{payload.get('collectionName', '—')}`", inline=True)
        embed.add_field(name="Genre", value=f"`{payload.get('primaryGenreName', '—')}`", inline=True)
        if payload.get("previewUrl"):
            embed.add_field(name="Preview", value=f"[▶️ 30-second clip]({payload['previewUrl']})", inline=False)
        embed.set_footer(text="Meow · song info")
        await style.send_card(interaction, embed=embed)

    @music.command(name="lyrics", description="Look up the words to a song.")
    @app_commands.describe(title="Song title", artist="Artist (optional but it helps)")
    async def lyrics(self, interaction: discord.Interaction, title: str, artist: str | None = None):
        await interaction.response.defer()
        found_artist = (artist or "").strip()
        found_title = title.strip()
        if not found_artist:
            payload = await self._itunes(found_title)
            if payload:
                found_artist = payload.get("artistName", "")
                found_title = payload.get("trackName", found_title)

        text = await self._lyrics(found_artist, found_title)
        if not text:
            return await style.send_card(
                interaction,
                content=(
                    f"❌ No lyrics found for **{title}**"
                    + (f" by **{artist}**" if artist else "")
                    + ".\n-# Try both a title and an artist: `/music lyrics title:… artist:…`"
                ),
                ephemeral=True,
            )
        if len(text) > 3500:
            text = text[:3500] + "\n\n… truncated"
        embed = discord.Embed(
            title=f"📜 {found_title}",
            description=f"-# {found_artist or 'unknown artist'}\n\n{text}",
            color=style.ACCENT,
        )
        embed.set_footer(text="Meow · lyrics")
        await style.send_card(interaction, embed=embed)

    @music.command(name="doctor", description="What this host can and cannot do for voice (administrator).")
    @app_commands.checks.has_permissions(administrator=True)
    async def doctor(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        caps = voice_caps.probe(refresh=True)
        ok, why = voice_caps.build_source(caps)
        # The one line that cannot be read off an installed version: a version
        # string is not evidence, because a stale extractor stays installed,
        # importable, and reporting itself happily while answering every search
        # with a bot check. So the command asks for a recording, the same question
        # the weekly pass asks.
        probe = await asyncio.to_thread(media_source.self_check)
        lines = [
            f"**Playback** · {'yes' if caps.playable else 'no'}",
            f"**ffmpeg** · `{caps.ffmpeg or 'missing'}` ({caps.ffmpeg_source})",
            f"**libopus** · `{caps.opus_source}` — {'live volume' if caps.live_volume else 'volume is baked into the stream'}",
            f"**DAVE** · `{caps.dave_version or 'missing'}`",
            f"**yt-dlp** · `{probe.version or 'missing'}`",
            f"**Extractor** · {probe.sentence()}",
            f"**Client** · `{probe.client or 'default'}`",
        ]
        if probe.detail:
            lines.append(f"-# asked for {probe.detail}")
        if probe.note:
            # The reading that turns "working today" into a ticket worth opening:
            # a host answering on a fallback client is one provider change away
            # from answering nothing at all.
            lines.append(f"\n⚠️ {probe.note}")
        if not probe.healthy and not probe.unreachable:
            lines.append(
                "\n-# `/music radio` keeps working either way — a radio station never "
                "touches the extractor."
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
        """Two jobs: keep 24/7 seats, and leave a room once it is empty."""
        if member.id == getattr(getattr(self.bot, "user", None), "id", None):
            if before.channel is not None and after.channel is None:
                player = self.players.get(int(before.channel.guild.id))
                if player is not None and player.is_247 and not player.manual_stop:
                    await asyncio.sleep(3)
                    try:
                        channel = before.channel.guild.get_channel(before.channel.id) or before.channel
                        player.voice_client = await channel.connect(timeout=15.0, reconnect=True, self_deaf=False)
                        print(f"[MUSIC] reconnected 24/7 in {channel.name}", flush=True)
                    except Exception as exc:
                        print(f"[MUSIC] 24/7 reconnect failed: {exc}", flush=True)
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
        humans = [m for m in getattr(channel, "members", ()) or () if not getattr(m, "bot", False)]
        if humans:
            player.cancel_alone_timer()
        elif not player.is_247:
            player.start_alone_timer()

    @commands.Cog.listener()
    async def on_ready(self):
        """Reclaim the 24/7 seats this deployment promised, after a restart."""
        await self.bot.wait_until_ready()
        try:
            records = await db.get_all_247_guilds()
        except Exception as exc:
            print(f"[MUSIC] could not read the 24/7 seats: {exc}", flush=True)
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
                player.voice_client = await channel.connect(timeout=15.0, reconnect=True, self_deaf=False)
                player.is_247 = True
                print(f"[MUSIC] reclaimed 24/7 in {guild.name}/#{channel.name}", flush=True)
            except Exception as exc:
                print(f"[MUSIC] could not reclaim 24/7 in {guild.name}: {exc}", flush=True)

    # ------------------------------------------------------------- providers #
    async def _itunes(self, query: str) -> dict | None:
        """One iTunes lookup. No key, no dependency, and honest about failing."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://itunes.apple.com/search",
                    params={"term": query, "entity": "song", "limit": 1},
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as response:
                    if response.status != 200:
                        return None
                    payload = await response.json(content_type=None)
        except Exception as exc:
            print(f"[MUSIC] iTunes lookup failed: {exc}", flush=True)
            return None
        results = (payload or {}).get("results") or []
        return results[0] if results else None

    async def _lyrics(self, artist: str, title: str) -> str | None:
        """Lyrics, by artist and title. Empty means empty, not broken."""
        if not artist or not title:
            return None
        clean_artist = artist.split("feat.")[0].split("&")[0].strip()
        clean_title = title.split("(feat.")[0].split(" - ")[0].strip()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.lyrics.ovh/v1/{clean_artist}/{clean_title}",
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as response:
                    if response.status != 200:
                        return None
                    payload = await response.json(content_type=None)
        except Exception as exc:
            print(f"[MUSIC] lyrics lookup failed: {exc}", flush=True)
            return None
        text = (payload or {}).get("lyrics")
        return text.strip() if text else None


# --------------------------------------------------------------------------- #
# The two views
# --------------------------------------------------------------------------- #
class TrackChoiceView(discord.ui.View):
    """Ask which recording was meant, instead of picking one and hoping.

    The whole point of the resolver's confidence margin is this menu: when two
    results are genuinely close, a person knows in a glance which one they wanted
    and a scorer cannot. Three options, because that is what a select renders
    without becoming another search.
    """

    def __init__(self, cog: Music, resolution: media_source.Resolution, *, requester: str):
        super().__init__(timeout=CHOICE_TIMEOUT)
        self.cog = cog
        self.requester = requester
        options = []
        for index, track in enumerate(resolution.alternatives[:3]):
            options.append(
                discord.SelectOption(
                    label=(track.title or "unknown")[:100],
                    value=str(index),
                    description=(f"{_mmss(track.duration)} · {track.provider}")[:100],
                )
            )
        select = discord.ui.Select(placeholder="Which one did you mean?", options=options)
        select.callback = self._chosen
        self.add_item(select)
        self._tracks = resolution.alternatives[:3]

    async def _chosen(self, interaction: discord.Interaction):
        choice = int(interaction.data["values"][0])
        track = self._tracks[choice]
        player = self.cog.get_player(interaction.guild_id)
        await interaction.response.defer()
        await self.cog._enqueue_and_play(interaction, player, track)
        self.stop()


class ControlsView(discord.ui.View):
    """Buttons under a playing card. The same actions the commands offer.

    Legacy rather than Components V2 on purpose: an interactive view carries a
    timeout and an ``interaction_check``, and merging one into a V2 layout drops
    both — which is the rule ``utils/style`` documents and the rest of the tree
    follows.
    """

    def __init__(self, cog: Music, player: player_module.GuildPlayer):
        super().__init__(timeout=None)
        self.cog = cog
        self.player = player

    def _playing(self) -> bool:
        vc = self.player.voice_client
        return bool(vc and vc.is_connected())

    @discord.ui.button(label="Pause", emoji="⏯️", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not self._playing():
            return await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)
        if vc.is_playing():
            vc.pause()
            return await interaction.response.send_message("⏸️ Paused.", ephemeral=True)
        if vc.is_paused():
            vc.resume()
            return await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.skip():
            return await interaction.response.send_message("❌ Nothing to skip.", ephemeral=True)
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    @discord.ui.button(label="Queue", emoji="📜", style=discord.ButtonStyle.secondary)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player
        if player.current is None and not player.queue:
            return await interaction.response.send_message("📭 The queue is empty.", ephemeral=True)
        lines = []
        if player.current is not None:
            lines.append(f"**▶️ Now** · {player.current.title}")
        for index, track in enumerate(player.queue[:8], start=1):
            lines.append(f"`{index}.` {track.title}")
        panel = style.card(
            title="📜 Queue",
            body="\n".join(lines),
            accent=style.ACCENT,
            footer=f"{len(player.queue)} queued · loop {player.loop}",
            timeout=None,
        )
        await style.send_panel(interaction, panel, ephemeral=True)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._playing():
            return await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)
        staying = self.player.is_247
        await self.player.stop(reason="stopped from the controls")
        await interaction.response.send_message(
            "⏹️ Stopped — still here because 24/7 is on." if staying else "⏹️ Stopped and left the channel.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
