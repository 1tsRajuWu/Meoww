"""Spotify music sharing: add tracks, libraries, listens and jam links.

No Spotify API key is needed anywhere here. Track details come from Spotify's
public oEmbed endpoint, which answers without authentication — a link pasted in
goes out, a title/artist/artwork comes back, and a card is built from it. If
oEmbed ever stops answering, ``add`` says so instead of posting a hollow card.

Three surfaces, one group:

* ``/music add`` — a Spotify link becomes a card with a persistent
  "I listened" button. The button outlives restarts (``custom_id`` carries the
  track), so old cards keep counting.
* ``/music my`` and ``/music top`` — who added what, and what the server
  actually listened to.
* ``/music jam`` / ``jamset`` / ``jamremove`` — the server's one active
  Spotify Jam link. Spotify offers no API for "what jams are live", so the
  jam is what a member declares it to be — set it, show it, remove it.
"""
import datetime
import json
import re
import urllib.parse

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import db
from utils.style import display_name, send_card, VIP_BOT_ID, VIP_INVITE_URL

DB_PATH = db.DB_PATH

SPOTIFY_LINK_RE = re.compile(
    r"https?://open\.spotify\.com/(track|album|playlist|episode|show|artist)/([A-Za-z0-9]+)"
)
JAM_LINK_RE = re.compile(r"https?://(?:open\.spotify\.com|spotify\.link)/\S+", re.IGNORECASE)
OEMBED_URL = "https://open.spotify.com/oembed?url={url}"
REQUEST_TIMEOUT = 8.0
MUSIC_ICON_GIF = "https://1tsrajowo-meow.hf.space/static/music_visualizer.gif"

#: How often the jam watcher re-checks every server's active jam, how many
#: consecutive dead reads end it, and the session cap after which it ends
#: whatever the link says. Spotify offers no "is this jam live" API, so liveness
#: is read off the link itself — and a link that answers is not proof anyone is
#: still listening, which is what the cap is for.
JAM_CHECK_MINUTES = 10
JAM_MAX_FAILS = 3
JAM_MAX_AGE_HOURS = 12

#: Final landing pages that mean "there is no jam here any more". Anything behind
#: a login wall counts as unknown, never dead — a wall is not an answer.
JAM_DEAD_PATHS = ("/", "/search", "/download", "/premium", "/about", "/legal", "/us")


async def ensure_music_schema() -> None:
    """Create the music tables and heal columns a live database predates.

    The jam watcher needs ``channel_id``/``fails``/``last_checked`` on a table
    that shipped without them; ``CREATE TABLE IF NOT EXISTS`` cannot add a
    column, so missing ones are altered in. Runs on every jam write and every
    sweep, because a live database can be replaced under a running process.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS music_jam (
            guild_id INTEGER PRIMARY KEY,
            url TEXT,
            set_by INTEGER,
            set_at TEXT,
            channel_id INTEGER,
            fails INTEGER DEFAULT 0,
            last_checked TEXT
        )''')
        async with conn.execute("PRAGMA table_info(music_jam)") as cur:
            cols = {row[1] async for row in cur}
        for column in ("channel_id", "fails", "last_checked"):
            if column not in cols:
                await conn.execute(f"ALTER TABLE music_jam ADD COLUMN {column}")
        await conn.commit()


def jam_landing_is_dead(final_url: str) -> bool:
    """Whether a resolved jam URL lands somewhere that is not a jam.

    Pure, so the dead/live line is testable without the network: gone links
    (root, search, marketing pages) read dead, everything else reads live.
    """
    try:
        parts = urllib.parse.urlparse(str(final_url or ""))
    except Exception:
        return False
    if parts.netloc not in ("open.spotify.com", "spotify.link"):
        return False
    path = (parts.path or "/").rstrip("/") or "/"
    return path in JAM_DEAD_PATHS or any(
        path == dead or path.startswith(dead + "/") for dead in JAM_DEAD_PATHS[1:])


async def fetch_jam_status(url: str, session) -> str:
    """``live`` / ``dead`` / ``unknown`` for one jam link.

    Only a definitive answer counts as dead (gone status, or a landing page
    that cannot be a jam). Timeouts, login walls and transport errors are
    ``unknown`` — a blip must never end anyone's session. Never raises.
    """
    try:
        async with session.get(url, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as response:
            final = str(getattr(response, "url", url) or url)
            status = getattr(response, "status", 0) or 0
    except Exception:
        return "unknown"
    if status in (404, 410):
        return "dead"
    if status >= 400:
        return "unknown"
    try:
        if "accounts.spotify.com" in final or "/login" in final:
            return "unknown"
    except Exception:
        return "unknown"
    try:
        return "dead" if jam_landing_is_dead(final) else "live"
    except Exception:
        return "unknown"


async def _jam_rows():
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT guild_id, url, set_by, set_at, channel_id, fails FROM music_jam"
        ) as cur:
            return [tuple(r) for r in await cur.fetchall()]


def _jam_age_hours(set_at: str | None) -> float | None:
    if not set_at:
        return None
    try:
        then = datetime.datetime.fromisoformat(str(set_at))
        if then.tzinfo is None:
            then = then.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds() / 3600
    except Exception:
        return None

KIND_LABELS = {
    "track": "Track",
    "album": "Album",
    "playlist": "Playlist",
    "episode": "Episode",
    "show": "Show",
    "artist": "Artist",
}


def parse_spotify_link(text: str | None) -> dict | None:
    """A Spotify URL into ``kind``/``entity``/``url``, or ``None``.

    Pure, so the shape of a link is testable without the network: only
    ``open.spotify.com`` entity URLs count. Anything else is not something a
    card can be built from.
    """
    match = SPOTIFY_LINK_RE.search(str(text or ""))
    if not match:
        return None
    kind, entity = match.group(1).lower(), match.group(2)
    return {"kind": kind, "entity": entity, "url": f"https://open.spotify.com/{kind}/{entity}"}


async def fetch_track_details(url: str) -> dict | None:
    """Title/artist/artwork for a Spotify URL via embed scraper & oEmbed fallback.

    Extracts genuine metadata with artist names directly from Spotify's
    embed entity payload, falling back to oEmbed if unreachable. Never raises.
    """
    parsed = parse_spotify_link(url)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    if parsed:
        try:
            embed_url = f"https://open.spotify.com/embed/{parsed['kind']}/{parsed['entity']}"
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(embed_url) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
                        if m:
                            data = json.loads(m.group(1))
                            entity = (
                                data.get("props", {})
                                .get("pageProps", {})
                                .get("state", {})
                                .get("data", {})
                                .get("entity", {})
                            )
                            title = entity.get("name") or entity.get("title")
                            artists = [
                                a.get("name")
                                for a in entity.get("artists", [])
                                if isinstance(a, dict) and a.get("name")
                            ]
                            if not artists and entity.get("subtitle"):
                                artists = [str(entity["subtitle"]).strip()]
                            artist = ", ".join(artists) if artists else ""
                            images = entity.get("visualIdentity", {}).get("image") or []
                            art = images[0].get("url") if images and isinstance(images[0], dict) else ""
                            if title:
                                return {
                                    "title": str(title).strip()[:200],
                                    "artist": (artist or "Unknown artist")[:200],
                                    "art": str(art or ""),
                                }
        except Exception:
            pass

    try:
        encoded_url = urllib.parse.quote(url, safe="")
        oembed_url = f"https://open.spotify.com/oembed?url={encoded_url}"
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(oembed_url) as response:
                if response.status == 200:
                    payload = await response.json(content_type=None)
                    if isinstance(payload, dict):
                        title = str(payload.get("title") or "").strip()
                        artist = str(payload.get("author_name") or "").strip()
                        art = str(payload.get("thumbnail_url") or "")
                        if not artist and " - " in title:
                            parts = title.split(" - ", 1)
                            artist = parts[0].strip()
                            title = parts[1].strip()
                        if title:
                            return {
                                "title": title[:200],
                                "artist": (artist or "Unknown artist")[:200],
                                "art": art,
                            }
    except Exception:
        pass
    return None


async def _track(guild_id: int, entity_id: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT kind, url, title, artist, art, added_by FROM music_tracks"
            " WHERE guild_id=? AND entity_id=?",
            (guild_id, entity_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return {
        "kind": row[0], "url": row[1], "title": row[2],
        "artist": row[3], "art": row[4], "added_by": row[5],
    }


async def _listen_stats(guild_id: int, entity_id: str) -> tuple[int, list[int]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT user_id FROM music_listens WHERE guild_id=? AND entity_id=?",
            (guild_id, entity_id),
        ) as cur:
            user_ids = [r[0] async for r in cur]
    return len(user_ids), user_ids


def build_track_embed(track: dict, listens: int, listener_names: list[str]) -> discord.Embed:
    """The beautiful card: art, details, and who actually listened."""
    kind_label = KIND_LABELS.get(track.get("kind", ""), "Music")
    track_title = track.get("title", "Unknown title")
    embed = discord.Embed(
        title=track_title,
        url=track.get("url"),
        color=0x1DB954,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(
        name=f"{track_title} • Spotify {kind_label}",
        icon_url=MUSIC_ICON_GIF,
        url=track.get("url"),
    )
    embed.add_field(name="🎤 Artist", value=track.get("artist", "Unknown artist") or "Unknown artist", inline=True)
    embed.add_field(name="👂 Listens", value=f"**{listens}**", inline=True)
    if track.get("art"):
        embed.set_thumbnail(url=track["art"])
    if listener_names:
        shown = ", ".join(listener_names[:10])
        more = f" (+{len(listener_names) - 10} more)" if len(listener_names) > 10 else ""
        embed.add_field(name="🎧 Listened by", value=f"{shown}{more}", inline=False)
    embed.set_footer(text="Click Listen below to open on Spotify • DaCat Music", icon_url=MUSIC_ICON_GIF)
    return embed


class ListenedView(discord.ui.View):
    """Persistent "Listen" button, one shared ``custom_id`` for every card.

    The track is read off the pressed message's own embed URL rather than the
    button, so one registered view answers every card the bot ever posted —
    including cards from before a restart.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Listen",
        emoji="🎧",
        style=discord.ButtonStyle.success,
        custom_id="music-listen",
    )
    async def listened(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _listened_callback(interaction)


async def _listened_callback(interaction: discord.Interaction):
    entity_id = None
    try:
        message = interaction.message
        embeds = getattr(message, "embeds", None) or []
        if embeds:
            target_url = embeds[0].url or (embeds[0].author.url if getattr(embeds[0], "author", None) else None)
            if target_url:
                parsed = parse_spotify_link(target_url)
                if parsed:
                    entity_id = parsed["entity"]
    except Exception:
        entity_id = None
    if not interaction.guild or not entity_id:
        return await interaction.response.send_message(
            "❌ This button only works in a server, on a track card.", ephemeral=True)
    track = await _track(interaction.guild.id, entity_id)
    if track is None:
        return await interaction.response.send_message(
            "❌ That track is no longer in this server's library.", ephemeral=True)

    already_listened = False
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT 1 FROM music_listens WHERE guild_id=? AND entity_id=? AND user_id=?",
            (interaction.guild.id, entity_id, interaction.user.id),
        ) as cur:
            if await cur.fetchone() is not None:
                already_listened = True

        if not already_listened:
            await conn.execute(
                "INSERT OR IGNORE INTO music_listens (guild_id, entity_id, user_id, listened_at)"
                " VALUES (?, ?, ?, ?)",
                (interaction.guild.id, entity_id, interaction.user.id,
                 discord.utils.utcnow().isoformat()),
            )
            await conn.commit()

    link_view = discord.ui.View()
    link_view.add_item(discord.ui.Button(
        label="Open in Spotify",
        style=discord.ButtonStyle.link,
        url=track["url"],
        emoji="🎶",
    ))

    if not already_listened:
        listens, user_ids = await _listen_stats(interaction.guild.id, entity_id)
        names = [await display_name(interaction.guild, uid) for uid in user_ids]
        await interaction.response.edit_message(
            embed=build_track_embed(track, listens, names))
        if hasattr(interaction, "followup") and hasattr(interaction.followup, "send"):
            try:
                await interaction.followup.send(
                    f"🎧 **Listen counted!** Click below to listen on Spotify:\n<{track['url']}>",
                    view=link_view,
                    ephemeral=True,
                )
            except Exception:
                pass
    else:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"ℹ️ You already listened to this track! Click below to open:\n<{track['url']}>",
                view=link_view,
                ephemeral=True,
            )
        elif hasattr(interaction, "followup") and hasattr(interaction.followup, "send"):
            try:
                await interaction.followup.send(
                    f"ℹ️ You already listened to this track! Click below to open:\n<{track['url']}>",
                    view=link_view,
                    ephemeral=True,
                )
            except Exception:
                pass


class Music(commands.Cog):
    """Spotify links into cards, libraries, listen counts and jam links."""

    music = app_commands.Group(
        name="music",
        description="🎵 Share Spotify music, track listens, and post jam links!",
    )

    def __init__(self, bot: commands.Bot | None):
        self.bot = bot
        # The listen button must survive restarts: a persistent view answers
        # presses on cards posted by a previous process.
        if bot is not None and hasattr(bot, "add_view"):
            bot.add_view(ListenedView())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        client = getattr(interaction, "client", None)
        user = getattr(client, "user", None)
        if user is not None and getattr(user, "id", None) != VIP_BOT_ID:
            await interaction.response.send_message(
                "👑 The **Spotify Music Engine** (`/music`) is exclusive to **DaCat VIP / Meoww Premium**!\n"
                f"Add the VIP Bot to your server to use music features: [Add VIP Bot]({VIP_INVITE_URL})",
                ephemeral=True,
            )
            return False
        if interaction.guild_id and self.bot is not None and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "music"):
                await interaction.response.send_message(
                    "⚠️ The **Music & Spotify Jams** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                    ephemeral=True,
                )
                return False
        return True

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # Never re-raise: a cog handler that raises skips the tree handler and
        # leaves the command unanswered ("the application did not respond").
        if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            await interaction.response.send_message(
                "❌ You don't have permission to do that.", ephemeral=True)

    @tasks.loop(minutes=JAM_CHECK_MINUTES)
    async def jam_watch(self):
        """Re-check every server's jam: ended links and old sessions go away.

        Nobody has to remove anything. A link that reads dead three checks in
        a row, or a session older than the cap, is deleted and announced where
        it was posted. Unknowns (timeouts, login walls) never end a session.
        """
        await self._sweep_jams()

    @jam_watch.before_loop
    async def _before_jam_watch(self):
        try:
            await self.bot.wait_until_ready()
        except (RuntimeError, AttributeError):
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        if self.bot is not None and not self.jam_watch.is_running():
            self.jam_watch.start()

    async def _sweep_jams(self, probe=None) -> dict:
        """One watch pass over every jam. Returns what happened per guild.

        ``probe`` is a test seam standing in for the network.
        """
        await ensure_music_schema()
        probe = probe or fetch_jam_status
        outcome: dict = {}
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            session = aiohttp.ClientSession(timeout=timeout)
        except Exception:
            return outcome
        try:
            for row in await _jam_rows():
                guild_id, url, _set_by, set_at, channel_id, fails = row
                if self.bot is not None and hasattr(self.bot, "is_module_enabled"):
                    try:
                        if not self.bot.is_module_enabled(guild_id, "music"):
                            continue
                    except Exception:
                        pass
                age = _jam_age_hours(set_at)
                if age is not None and age >= JAM_MAX_AGE_HOURS:
                    await self._end_jam(guild_id, channel_id, expired=True)
                    outcome[guild_id] = "expired"
                    continue
                try:
                    status = await probe(url, session)
                except Exception:
                    status = "unknown"
                if status == "live":
                    await self._mark_jam(guild_id, fails=0)
                    outcome[guild_id] = "live"
                elif status == "dead":
                    fails = int(fails or 0) + 1
                    if fails >= JAM_MAX_FAILS:
                        await self._end_jam(guild_id, channel_id, expired=False)
                        outcome[guild_id] = "ended"
                    else:
                        await self._mark_jam(guild_id, fails=fails)
                        outcome[guild_id] = f"failing-{fails}"
                else:
                    outcome[guild_id] = "unknown"
        finally:
            try:
                await session.close()
            except Exception:
                pass
        return outcome

    async def _mark_jam(self, guild_id: int, *, fails: int) -> None:
        try:
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    "UPDATE music_jam SET fails=?, last_checked=? WHERE guild_id=?",
                    (fails, datetime.datetime.now(datetime.timezone.utc).isoformat(), guild_id),
                )
                await conn.commit()
        except Exception:
            pass

    async def _end_jam(self, guild_id: int, channel_id: int | None, *, expired: bool) -> None:
        try:
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute("DELETE FROM music_jam WHERE guild_id=?", (guild_id,))
                await conn.commit()
        except Exception:
            return
        if self.bot is None or channel_id is None:
            return
        try:
            channel = self.bot.get_channel(channel_id)
        except Exception:
            channel = None
        if channel is None:
            return
        reason = (f"hit the {JAM_MAX_AGE_HOURS}h session cap"
                  if expired else "the link went quiet")
        try:
            await channel.send(embed=discord.Embed(
                title="🎧 Spotify Jam Ended",
                description=f"The jam {reason} — cleared automatically, post a fresh one with `/music jamset`! 🎶",
                color=0x95A5A6,
                timestamp=discord.utils.utcnow(),
            ))
        except Exception:
            pass

    @music.command(name="add", description="➕ Add a Spotify link to this server's music library!")
    @app_commands.describe(link="A Spotify track, album, playlist or episode link")
    async def music_add(self, interaction: discord.Interaction, link: str):
        parsed = parse_spotify_link(link)
        if parsed is None:
            return await interaction.response.send_message(
                "❌ That doesn't look like a Spotify link! Copy one from Spotify's Share button.",
                ephemeral=True,
            )
        existing = await _track(interaction.guild.id, parsed["entity"])
        if existing is not None:
            adder = await display_name(interaction.guild, existing["added_by"])
            return await interaction.response.send_message(
                f"ℹ️ Already in this server's library (added by **{adder}**)!",
                ephemeral=True,
            )
        details = await fetch_track_details(parsed["url"])
        if details is None:
            return await interaction.response.send_message(
                "❌ Spotify wouldn't tell me about that link right now — check it opens in Spotify and try again.",
                ephemeral=True,
            )
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO music_tracks"
                " (guild_id, entity_id, kind, url, title, artist, art, added_by, added_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (interaction.guild.id, parsed["entity"], parsed["kind"], parsed["url"],
                 details["title"], details["artist"], details["art"],
                 interaction.user.id, discord.utils.utcnow().isoformat()),
            )
            await conn.commit()
        track = {**parsed, **details, "added_by": interaction.user.id}
        embed = build_track_embed(track, 0, [])
        embed.set_footer(
            text=f"Added by {interaction.user.display_name} • Click Listen below to open & count",
            icon_url=MUSIC_ICON_GIF,
        )
        await interaction.response.send_message(embed=embed, view=ListenedView())

    @music.command(name="my", description="Show the songs you added and who listened!")
    @app_commands.describe(member="Whose library to look up — defaults to you")
    async def music_my(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT entity_id, kind, url, title, artist FROM music_tracks"
                " WHERE guild_id=? AND added_by=? ORDER BY added_at DESC",
                (interaction.guild.id, member.id),
            ) as cur:
                rows = list(await cur.fetchall())
        if not rows:
            whose = "You haven't" if member.id == interaction.user.id else f"{member.display_name} hasn't"
            return await send_card(interaction, embed=discord.Embed(
                title="🎵 Music Library",
                description=f"{whose} added any songs yet! Add one with `/music add`. 🎧",
                color=0x1DB954,
                timestamp=discord.utils.utcnow(),
            ))
        lines = []
        total_listens = 0
        for entity_id, kind, url, title, artist in rows[:10]:
            listens, _ = await _listen_stats(interaction.guild.id, entity_id)
            total_listens += listens
            label = KIND_LABELS.get(kind, "Music")
            lines.append(f"🎵 [{title}]({url}) — *{artist}* [{label}] • 👂 **{listens}**")
        if len(rows) > 10:
            lines.append(f"*...and {len(rows) - 10} more*")
        await send_card(interaction, embed=discord.Embed(
            title=f"🎵 {member.display_name}'s Music ({len(rows)} songs, 👂 {total_listens} listens)",
            description="\n".join(lines),
            color=0x1DB954,
            timestamp=discord.utils.utcnow(),
        ))

    @music.command(name="top", description="This server's most-listened songs!")
    async def music_top(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT t.title, t.artist, t.url, COUNT(l.user_id) AS c FROM music_tracks t"
                " LEFT JOIN music_listens l ON l.guild_id=t.guild_id AND l.entity_id=t.entity_id"
                " WHERE t.guild_id=? GROUP BY t.entity_id ORDER BY c DESC, t.added_at DESC LIMIT 10",
                (interaction.guild.id,),
            ) as cur:
                rows = list(await cur.fetchall())
        if not rows:
            return await send_card(interaction, embed=discord.Embed(
                title="🏆 Most Listened",
                description="Nobody added any songs yet! Start with `/music add`. 🎧",
                color=0x1DB954,
                timestamp=discord.utils.utcnow(),
            ))
        lines = [
            f"**{i}.** [{title}]({url}) — *{artist}* • 👂 **{count}**"
            for i, (title, artist, url, count) in enumerate(rows, 1)
        ]
        await send_card(interaction, embed=discord.Embed(
            title="🏆 Most Listened in This Server",
            description="\n".join(lines),
            color=0xF1C40F,
            timestamp=discord.utils.utcnow(),
        ))

    @music.command(name="jam", description="🎧 Show this server's active Spotify Jam!")
    async def music_jam(self, interaction: discord.Interaction):
        await ensure_music_schema()
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT url, set_by, set_at, last_checked FROM music_jam WHERE guild_id=?",
                (interaction.guild.id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return await send_card(interaction, embed=discord.Embed(
                title="🎧 Spotify Jam",
                description="No active jam right now! Start one in Spotify and post it with `/music jamset`. 🎶",
                color=0x1DB954,
                timestamp=discord.utils.utcnow(),
            ))
        url, set_by, set_at, last_checked = row
        setter = await display_name(interaction.guild, set_by)
        watched = ("\n\n✅ Live — bot watching, auto-removes when it ends"
                   + (f" (checked `{str(last_checked)[:16].replace('T', ' ')}`)" if last_checked else ""))
        await send_card(interaction, embed=discord.Embed(
            title="🎧 Active Spotify Jam",
            description=f"[**Tap to join the jam!**]({url})\n\nSet by **{setter}** • `{set_at or 'recently'}`{watched}",
            color=0x1DB954,
            timestamp=discord.utils.utcnow(),
        ))

    @music.command(name="jamset", description="📢 Post this server's active Spotify Jam link!")
    @app_commands.describe(link="The Spotify Jam invite link")
    async def music_jamset(self, interaction: discord.Interaction, link: str):
        link = str(link or "").strip()
        if not JAM_LINK_RE.match(link):
            return await interaction.response.send_message(
                "❌ That doesn't look like a Spotify Jam link! In Spotify: play something → Devices → Start a Jam → Invite → Copy link.",
                ephemeral=True,
            )
        await ensure_music_schema()
        now = discord.utils.utcnow().isoformat()
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO music_jam"
                " (guild_id, url, set_by, set_at, channel_id, fails, last_checked)"
                " VALUES (?, ?, ?, ?, ?, 0, ?)",
                (interaction.guild.id, link, interaction.user.id, now,
                 interaction.channel_id, now),
            )
            await conn.commit()
        await send_card(interaction, embed=discord.Embed(
            title="🎧 Spotify Jam Live!",
            description=f"[**Tap to join the jam!**]({link})\n\nPosted by {interaction.user.mention} — everyone in, same song, same moment! 🎶\n\n👀 I'll keep watch and clear it automatically when the jam ends (or after {JAM_MAX_AGE_HOURS}h).",
            color=0x1DB954,
            timestamp=discord.utils.utcnow(),
        ))

    @music.command(name="jamremove", description="Remove this server's active Spotify Jam!")
    async def music_jamremove(self, interaction: discord.Interaction):
        await ensure_music_schema()
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT url FROM music_jam WHERE guild_id=?", (interaction.guild.id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return await interaction.response.send_message(
                    "ℹ️ There's no active jam to remove!", ephemeral=True)
            await conn.execute("DELETE FROM music_jam WHERE guild_id=?", (interaction.guild.id,))
            await conn.commit()
        await interaction.response.send_message("✅ Jam removed — the session's over! 🎧", ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
