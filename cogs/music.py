"""Radio: one `/music` group, a catalogue, and nothing that can be refused.

**Radio is the whole of playback**, and that is a decision rather than what is
left over. This cog used to hold `/play` and the machinery behind it — yt-dlp, a
ladder of player clients, three providers, a second-provider rescue, a Spotify
half — plus `/music songinfo`, `/lyrics`, `/seek`, `/loop` and `/shuffle` around
it. All of it is gone, and the archive says why better than a summary can: every
one of those commands ended at a *platform*, and a platform that has decided a
datacenter address is a bot is not something any amount of code on this side can
change. A station URL is different in kind. It is fetched by hand and verified
before it is written down, it asks for no account, and it plays from any address
— which is why the fallback was always the thing that worked, and why it is now
the thing that exists.

The extractor and its suite are archived in ``deleted_features/``
(``utils/media_source.py``, ``tests/test_music_matching.py``, and the resolver
half of the player's suite): restoring a resolver means restoring those files and
the ``yt-dlp`` line in ``requirements.txt``, and nothing here has to move for it.

All the playback logic is in two modules — :mod:`utils.media_source` holds the
stream and the host check, :mod:`utils.player` plays it — so this file is
commands, cards, the catalogue, and the two Discord-only jobs (the voice-state
listener and the 24/7 reconnect). The distinction matters for a feature that was
retired once for being untestable: everything except the gateway half is
exercised without one.

The command surface is arithmetic rather than taste: Discord allows an
application **100 top-level commands per scope** and this bot already publishes
most of that budget, so every command here lives under `/music` — one slot — and
removing `/play` gave back the only root this cog ever had.

Two things it says out loud instead of failing quietly, because each of them was
a real complaint about the archived version:

* **"The music is short."** A station that drops reaches the player looking like
  a finished track — a closed connection is a clean EOF to everything downstream
  — so the queue used to move on in silence. A cut stream now posts a card naming
  the station, how long it played and what the engine said, and it goes to the
  operator as well, because a stream that keeps dying is a fact about the host.
* **"Music is broken here."** Every command checks whether this host can play at
  all *before* joining a channel, and names the missing piece when it cannot.
"""
from __future__ import annotations

import asyncio
import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from utils import db, media_source, operator_log, style, voice_caps
from utils import player as player_module
from utils.card import generate_now_playing_card

#: The stations. Direct streams, so a radio session touches no platform, no
#: account and no extractor — there is none left to touch — and it plays from an
#: address everything else on this host may be refused from.
#:
#: **Six was the first version, and it made the catalogue a formality.** Radio is
#: the whole of playback now, and a six-item answer is not an answer: somebody who
#: wants Turkish pop, baroque, chiptune or a language they actually speak would be
#: told no. This is the catalogue that makes the feature real, and it is why the
#: command searches rather than offering a menu of six.
#:
#: Two rules govern what may be added, and both come from a failure this project has
#: already had:
#:
#: * **Every URL here was fetched before it was written down.** A station list is a
#:   list of URLs, and an unverified URL is a station that is broken on the day
#:   somebody picks it — the exact complaint that produced
#:   :mod:`utils.media_health`. Probed with an ordinary HTTP GET and the first bytes
#:   read: a 404, a DNS failure and an HTML error page were all rejected.
#: * **The name is what the stream calls itself, not what a directory called it.**
#:   Directory tags are wrong often enough to matter: the entry filed under *baroque*
#:   in the pass that built this was a Bee Gees station, and the one under
#:   *classical* was Qur'an recitation. Both are fine stations. Neither is what the
#:   label promised, and a member who picks *baroque* and gets *Tragedy* has been
#:   lied to by a lookup table.
#:
#: ``genre`` is what the browse command groups by, ``lang`` is the language the
#: words are in (empty when the station has none worth declaring), and ``tags`` draws
#: from the closed vocabulary in :data:`RADIO_TAGS` so a filter cannot quietly stop
#: matching.
RADIO_TAGS: dict[str, str] = {
    "instrumental": "no singing — safe underneath a conversation",
    "focus": "built to be worked to: steady, low-distraction",
    "chill": "low intensity throughout",
    "energetic": "loud and fast by design",
    "oldies": "the catalogue is from before 2000",
    "seasonal": "in season or not, and obvious about which",
}

RADIO_STATIONS: dict[str, dict] = {
    # ── Chill, focus and ambient ─────────────────────────────────────────── #
    "groovesalad": {
        "name": "Groove Salad",
        "url": "https://ice1.somafm.com/groovesalad-128-mp3",
        "desc": "A nicely chilled plate of ambient beats and grooves. SomaFM's calm one.",
        "icon": "🥗",
        "genre": "Chill",
        "lang": "",
        "tags": ("instrumental", "focus", "chill"),
    },
    "beatblender": {
        "name": "Beat Blender",
        "url": "https://ice1.somafm.com/beatblender-128-mp3",
        "desc": "A late-night blend of deep house and downtempo chill.",
        "icon": "🌙",
        "genre": "Chill",
        "lang": "",
        "tags": ("instrumental", "focus", "chill"),
    },
    "lofi": {
        "name": "Lo-Fi Beats",
        "url": "https://stream.laut.fm/lofi",
        "desc": "Hazy, dusty, unhurried lo-fi. The default for homework and late nights.",
        "icon": "☕",
        "genre": "Lo-Fi",
        "lang": "",
        "tags": ("instrumental", "focus", "chill"),
    },
    "chillhop": {
        "name": "I♥CHILLHOP",
        "url": "https://streams.ilovemusic.de/iloveradio17.mp3",
        "desc": "Chillhop and lo-fi hip hop with the edges sanded off.",
        "icon": "🍃",
        "genre": "Lo-Fi",
        "lang": "",
        "tags": ("instrumental", "focus", "chill"),
    },
    "lofihiphop": {
        "name": "Lofi Hip Hop Radio",
        "url": "https://stream.zeno.fm/0r0xa792kwzuv",
        "desc": "The 24/7 lo-fi hip hop stream people leave on for hours.",
        "icon": "📻",
        "genre": "Lo-Fi",
        "lang": "",
        "tags": ("instrumental", "focus", "chill"),
    },
    "chillout": {
        "name": "Chillout",
        "url": "https://stream.laut.fm/chillout",
        "desc": "Downtempo and easy listening, at a volume that never intrudes.",
        "icon": "🛋️",
        "genre": "Chill",
        "lang": "",
        "tags": ("instrumental", "focus", "chill"),
    },
    "musicandchill": {
        "name": "I♥MUSIC & CHILL",
        "url": "https://streams.ilovemusic.de/iloveradio10.mp3",
        "desc": "Slow pop and R&B for sitting still to.",
        "icon": "💤",
        "genre": "Chill",
        "lang": "",
        "tags": ("chill",),
    },
    "ambient": {
        "name": "Ambient Radio",
        "url": "https://stream.laut.fm/ambient",
        "desc": "Textures and drones with no rhythm to follow.",
        "icon": "🌫️",
        "genre": "Ambient",
        "lang": "",
        "tags": ("instrumental", "focus", "chill"),
    },
    "dronezone": {
        "name": "Drone Zone",
        "url": "https://ice1.somafm.com/dronezone-128-mp3",
        "desc": "Atmospheric textures with barely a beat. For concentrating, or sleeping.",
        "icon": "🛰️",
        "genre": "Ambient",
        "lang": "",
        "tags": ("instrumental", "focus", "chill"),
    },
    "deepspace": {
        "name": "Deep Space One",
        "url": "https://ice1.somafm.com/deepspaceone-128-mp3",
        "desc": "Deep ambient, experimental electronics, and the odd NASA transmission.",
        "icon": "🌌",
        "genre": "Ambient",
        "lang": "",
        "tags": ("instrumental", "focus", "chill"),
    },
    "spacestation": {
        "name": "Space Station Soma",
        "url": "https://ice1.somafm.com/spacestation-128-mp3",
        "desc": "Tune in, turn on, space out. Ambient and mid-tempo electronica.",
        "icon": "🚀",
        "genre": "Ambient",
        "lang": "",
        "tags": ("instrumental", "focus", "chill"),
    },
    "fluid": {
        "name": "Fluid",
        "url": "https://ice1.somafm.com/fluid-128-mp3",
        "desc": "Instrumental hip hop, triphop and the stranger end of jazz.",
        "icon": "💧",
        "genre": "Instrumental",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    "instrumental": {
        "name": "Instrumental",
        "url": "https://stream.laut.fm/instrumental",
        "desc": "No vocals at all — the one station to pick if singing over you is the problem.",
        "icon": "🎻",
        "genre": "Instrumental",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    "digitalis": {
        "name": "Digitalis",
        "url": "https://ice1.somafm.com/digitalis-128-mp3",
        "desc": "Folk, indie and slowcore — the quiet acoustic corner of SomaFM.",
        "icon": "🌾",
        "genre": "Folk",
        "lang": "",
        "tags": ("chill", "oldies"),
    },
    # ── Jazz, soul, funk and lounge ──────────────────────────────────────── #
    "jazz": {
        "name": "FIP Jazz",
        "url": "https://icecast.radiofrance.fr/fipjazz-midfi.mp3",
        "desc": "Straight jazz from Radio France, with almost no talking in between.",
        "icon": "🎷",
        "genre": "Jazz",
        "lang": "French",
        "tags": ("instrumental", "focus"),
    },
    "jazzcafe": {
        "name": "Swiss Jazz",
        "url": "https://stream.srg-ssr.ch/m/rsj/mp3_128",
        "desc": "Mellow cafe jazz, standards and the occasional bossa.",
        "icon": "☕",
        "genre": "Jazz",
        "lang": "",
        "tags": ("instrumental", "focus", "chill"),
    },
    "jazzfm": {
        "name": "Jazz FM",
        "url": "https://stream.laut.fm/jazz",
        "desc": "A wide sweep of jazz: bebop, fusion, and everything filed between.",
        "icon": "🎺",
        "genre": "Jazz",
        "lang": "",
        "tags": ("instrumental",),
    },
    "sonicuniverse": {
        "name": "Sonic Universe",
        "url": "https://ice1.somafm.com/sonicuniverse-128-mp3",
        "desc": "Nu jazz and avant-garde Euro jazz, for when the standards are not the point.",
        "icon": "🪐",
        "genre": "Jazz",
        "lang": "",
        "tags": ("instrumental",),
    },
    "lounge": {
        "name": "Lounge",
        "url": "https://stream.laut.fm/lounge",
        "desc": "Bossa, downtempo and hotel-bar smooth.",
        "icon": "🍸",
        "genre": "Lounge",
        "lang": "",
        "tags": ("instrumental", "chill"),
    },
    "illstreet": {
        "name": "Ill Street Lounge",
        "url": "https://ice1.somafm.com/illstreet-128-mp3",
        "desc": "Classic bachelor pad, playful exotica and vintage music of tomorrow.",
        "icon": "🕶️",
        "genre": "Lounge",
        "lang": "",
        "tags": ("instrumental", "chill", "oldies"),
    },
    "secretagent": {
        "name": "Secret Agent",
        "url": "https://ice1.somafm.com/secretagent-128-mp3",
        "desc": "The soundtrack for your stylish, mysterious, dangerous life.",
        "icon": "🕵️",
        "genre": "Lounge",
        "lang": "",
        "tags": ("instrumental", "oldies"),
    },
    "swing": {
        "name": "Swing",
        "url": "https://stream.laut.fm/swing",
        "desc": "Big band, swing and jump blues.",
        "icon": "🎩",
        "genre": "Swing",
        "lang": "",
        "tags": ("oldies",),
    },
    "soul": {
        "name": "Soul",
        "url": "https://stream.laut.fm/soul",
        "desc": "Classic soul, motown and modern soul.",
        "icon": "💜",
        "genre": "Soul & Funk",
        "lang": "",
        "tags": ("oldies",),
    },
    "funk": {
        "name": "Funk",
        "url": "https://stream.laut.fm/funk",
        "desc": "Slap bass, horns and grooves built to be sampled.",
        "icon": "🕺",
        "genre": "Soul & Funk",
        "lang": "",
        "tags": ("energetic", "oldies"),
    },
    "blues": {
        "name": "Blues",
        "url": "https://stream.laut.fm/blues",
        "desc": "Delta, Chicago and electric blues.",
        "icon": "🎸",
        "genre": "Blues",
        "lang": "",
        "tags": ("oldies",),
    },
    # ── Classical, opera and soundtracks ─────────────────────────────────── #
    "classical": {
        "name": "Klassik",
        "url": "https://stream.laut.fm/klassik",
        "desc": "Orchestral and chamber works, from baroque to late romantic.",
        "icon": "🎼",
        "genre": "Classical",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    "classicalfm": {
        "name": "Classical KUSC",
        "url": "https://playerservices.streamtheworld.com/api/livestream-redirect/KUSCMP128.mp3",
        "desc": "Classical music from a public radio station, presented properly.",
        "icon": "📻",
        "genre": "Classical",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    "francemusique": {
        "name": "France Musique",
        "url": "https://icecast.radiofrance.fr/francemusique-midfi.mp3",
        "desc": "Concerts, opera and chamber music with a French accent.",
        "icon": "🇫🇷",
        "genre": "Classical",
        "lang": "French",
        "tags": ("instrumental",),
    },
    "baroquefm": {
        "name": "Venice Classic Radio",
        "url": "https://uk2.streamingpulse.com/ssl/vcr1",
        "desc": "Baroque and early music, mostly Italian, mostly instrumental.",
        "icon": "🏛️",
        "genre": "Classical",
        "lang": "Italian",
        "tags": ("instrumental", "focus", "oldies"),
    },
    "swissclassic": {
        "name": "Swiss Classic",
        "url": "https://stream.srg-ssr.ch/m/rsc_de/mp3_128",
        "desc": "A calm classical programme — the one to put on while reading.",
        "icon": "🎻",
        "genre": "Classical",
        "lang": "German",
        "tags": ("instrumental", "focus", "chill"),
    },
    "swissclassicfr": {
        "name": "Swiss Classic Français",
        "url": "https://stream.srg-ssr.ch/m/rsc_fr/mp3_128",
        "desc": "The same classical programme, introduced in French.",
        "icon": "🎻",
        "genre": "Classical",
        "lang": "French",
        "tags": ("instrumental", "focus", "chill"),
    },
    "opera": {
        "name": "France Musique Opéra",
        "url": "https://icecast.radiofrance.fr/francemusiqueopera-hifi.aac",
        "desc": "Arias, overtures and full performances, all day.",
        "icon": "🎭",
        "genre": "Opera",
        "lang": "",
        "tags": ("instrumental",),
    },
    "soundtrack": {
        "name": "Soundtrack",
        "url": "https://stream.laut.fm/soundtrack",
        "desc": "Film scores, series themes and the orchestral bits of games.",
        "icon": "🎬",
        "genre": "Soundtrack",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    # ── Electronic ───────────────────────────────────────────────────────── #
    "synthwave": {
        "name": "Nightwave Plaza",
        "url": "https://radio.plaza.one/mp3",
        "desc": "80s neon synthwave, future funk and vaporwave.",
        "icon": "🌃",
        "genre": "Synthwave",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    "vaporwave": {
        "name": "Vaporwave",
        "url": "https://stream.laut.fm/vaporwave",
        "desc": "Slowed, reverbed and nostalgic in a way you cannot place.",
        "icon": "🌴",
        "genre": "Vaporwave",
        "lang": "",
        "tags": ("instrumental", "chill"),
    },
    "edm": {
        "name": "I♥DANCE",
        "url": "https://streams.ilovemusic.de/iloveradio2.mp3",
        "desc": "Big-room dance and club edits, all day.",
        "icon": "🔊",
        "genre": "Dance",
        "lang": "",
        "tags": ("energetic",),
    },
    "techno": {
        "name": "Techno",
        "url": "https://stream.laut.fm/techno",
        "desc": "Four to the floor, no apologies.",
        "icon": "⚙️",
        "genre": "Techno",
        "lang": "",
        "tags": ("instrumental", "energetic"),
    },
    "house": {
        "name": "House",
        "url": "https://stream.laut.fm/house",
        "desc": "Classic and modern house, mixed continuously.",
        "icon": "🏠",
        "genre": "House",
        "lang": "",
        "tags": ("instrumental", "energetic"),
    },
    "deephouse": {
        "name": "Deep House",
        "url": "https://stream.laut.fm/deephouse",
        "desc": "Warmer, slower house with room to breathe.",
        "icon": "🌊",
        "genre": "House",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    "trance": {
        "name": "Trance",
        "url": "https://stream.laut.fm/trance",
        "desc": "Long builds, big drops, and the occasional classic.",
        "icon": "🌀",
        "genre": "Trance",
        "lang": "",
        "tags": ("instrumental", "energetic"),
    },
    "hardstyle": {
        "name": "I♥HARDSTYLE",
        "url": "https://streams.ilovemusic.de/iloveradio21.mp3",
        "desc": "Reverse bass and festival kicks. Not a background station.",
        "icon": "🥁",
        "genre": "Hardstyle",
        "lang": "",
        "tags": ("energetic",),
    },
    "dnb": {
        "name": "Brokenbeats",
        "url": "https://stream.brokenbeats.net/tune",
        "desc": "Drum and bass, jungle and breakbeat at broadcast quality.",
        "icon": "🔗",
        "genre": "Drum & Bass",
        "lang": "",
        "tags": ("instrumental", "energetic"),
    },
    "koolfm": {
        "name": "Kool FM London",
        "url": "https://admin.stream.rinse.fm/proxy/kool/stream",
        "desc": "The London jungle and drum & bass institution, live.",
        "icon": "🎚️",
        "genre": "Drum & Bass",
        "lang": "",
        "tags": ("energetic",),
    },
    "ukbass": {
        "name": "UK Bass Radio",
        "url": "https://www.ukbassradio.com/stream",
        "desc": "Bassline, garage and bass house from the UK.",
        "icon": "🇬🇧",
        "genre": "Drum & Bass",
        "lang": "",
        "tags": ("energetic",),
    },
    "dubstep": {
        "name": "Dub Step Beyond",
        "url": "https://ice1.somafm.com/dubstep-128-mp3",
        "desc": "Dubstep, dub and the deeper end of bass music.",
        "icon": "🔊",
        "genre": "Dubstep",
        "lang": "",
        "tags": ("instrumental", "energetic"),
    },
    "idm": {
        "name": "Cliqhop IDM",
        "url": "https://ice1.somafm.com/cliqhop-128-mp3",
        "desc": "Glitchy, brainy electronics for people who like their beats odd.",
        "icon": "🧠",
        "genre": "IDM",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    "defcon": {
        "name": "DEF CON Radio",
        "url": "https://ice1.somafm.com/defcon-128-mp3",
        "desc": "Music for hacking to, from the DEF CON conference's own station.",
        "icon": "👾",
        "genre": "Electro",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    "fipelectro": {
        "name": "FIP Électro",
        "url": "https://icecast.radiofrance.fr/fipelectro-midfi.mp3",
        "desc": "Electronic music chosen by people who take it seriously.",
        "icon": "⚡",
        "genre": "Electro",
        "lang": "French",
        "tags": ("instrumental", "energetic"),
    },
    "fipgroove": {
        "name": "FIP Groove",
        "url": "https://icecast.radiofrance.fr/fipgroove-midfi.mp3",
        "desc": "Soul, funk and groove across the decades.",
        "icon": "🎶",
        "genre": "Soul & Funk",
        "lang": "French",
        "tags": (),
    },
    "disco": {
        "name": "Disco",
        "url": "https://stream.laut.fm/disco",
        "desc": "Mirrorball disco and the boogie it turned into.",
        "icon": "🪩",
        "genre": "Disco",
        "lang": "",
        "tags": ("energetic", "oldies"),
    },
    "phonk": {
        "name": "Phonk",
        "url": "https://stream.laut.fm/phonk",
        "desc": "Distorted Memphis drums and drift music.",
        "icon": "🚗",
        "genre": "Hip-Hop",
        "lang": "",
        "tags": ("instrumental", "energetic"),
    },
    # ── Games and chip music ─────────────────────────────────────────────── #
    "gamemusic": {
        "name": "Gamesboro Radio",
        "url": "https://radio.gamesboro.org/listen/gamesboro_radio/radio.mp3",
        "desc": "Video game music, 24/7, from 8-bit to orchestral.",
        "icon": "🎮",
        "genre": "Game Music",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    "chiptune": {
        "name": "24/7 Chiptune Radio",
        "url": "https://radio.zelerk.com/listen/chiptune/stream.mp3",
        "desc": "Tracker music, demoscene and hardware chips, at 256 kbps.",
        "icon": "🕹️",
        "genre": "Chiptune",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    "retrogames": {
        "name": "CPC Scene Radio",
        "url": "https://a1.asurahosting.com/listen/cpc_scene_radio/radio.mp3",
        "desc": "8-bit and 16-bit machine music, from the scene that made it.",
        "icon": "💾",
        "genre": "Chiptune",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    "gaming": {
        "name": "Gaming",
        "url": "https://stream.laut.fm/gaming",
        "desc": "Trailer music and game scores for the middle of a session.",
        "icon": "🎯",
        "genre": "Game Music",
        "lang": "",
        "tags": ("instrumental", "energetic"),
    },
    "subspace": {
        "name": "qUAntUm RaDio",
        "url": "https://radio.erb.pw/listen/subspace/radio.mp3",
        "desc": "Demoscene, netlabel electronics and something you have not heard.",
        "icon": "🔬",
        "genre": "Chiptune",
        "lang": "",
        "tags": ("instrumental",),
    },
    # ── Pop, rock, metal and the decades ─────────────────────────────────── #
    "pop": {
        "name": "Swiss Pop",
        "url": "https://stream.srg-ssr.ch/m/rsp/mp3_128",
        "desc": "Today's pop, mostly in English, with a Swiss accent.",
        "icon": "🎤",
        "genre": "Pop",
        "lang": "",
        "tags": (),
    },
    "top40": {
        "name": "I♥RADIO Charts",
        "url": "https://streams.ilovemusic.de/iloveradio1.mp3",
        "desc": "The current chart, on a loop.",
        "icon": "📈",
        "genre": "Top 40",
        "lang": "",
        "tags": (),
    },
    "pophits": {
        "name": "I♥BIGGEST POP HITS",
        "url": "https://streams.ilovemusic.de/iloveradio11.mp3",
        "desc": "The big pop records of the last decade or so.",
        "icon": "✨",
        "genre": "Pop",
        "lang": "",
        "tags": (),
    },
    "trashpop": {
        "name": "I♥TRASHPOP",
        "url": "https://streams.ilovemusic.de/iloveradio19.mp3",
        "desc": "Guilty-pleasure pop, played without irony.",
        "icon": "🫧",
        "genre": "Pop",
        "lang": "",
        "tags": (),
    },
    "rock": {
        "name": "Rock",
        "url": "https://stream.laut.fm/rock",
        "desc": "Classic and modern rock, guitar first.",
        "icon": "🎸",
        "genre": "Rock",
        "lang": "",
        "tags": ("energetic",),
    },
    "fiprock": {
        "name": "FIP Rock",
        "url": "https://icecast.radiofrance.fr/fiprock-midfi.mp3",
        "desc": "Rock chosen with taste, from garage to post-punk.",
        "icon": "🎸",
        "genre": "Rock",
        "lang": "French",
        "tags": (),
    },
    "rockmix": {
        "name": "Radio Paradise Rock Mix",
        "url": "https://stream.radioparadise.com/rock-320",
        "desc": "Album rock at 320 kbps, hand-picked rather than playlisted.",
        "icon": "🤘",
        "genre": "Rock",
        "lang": "",
        "tags": ("energetic",),
    },
    "punk": {
        "name": "Punk",
        "url": "https://stream.laut.fm/punk",
        "desc": "Punk, hardcore and the loud end of new wave.",
        "icon": "📌",
        "genre": "Punk",
        "lang": "",
        "tags": ("energetic", "oldies"),
    },
    "rockabilly": {
        "name": "Rockabilly",
        "url": "https://stream.laut.fm/rockabilly",
        "desc": "Fifties rock and roll, rockabilly and psychobilly.",
        "icon": "🚗",
        "genre": "Rock",
        "lang": "",
        "tags": ("oldies",),
    },
    "metal": {
        "name": "Metal Detector",
        "url": "https://ice1.somafm.com/metal-128-mp3",
        "desc": "Black to doom, prog to sludge, thrash to post and back.",
        "icon": "🤘",
        "genre": "Metal",
        "lang": "",
        "tags": ("energetic",),
    },
    "heavymetal": {
        "name": "Radio Heavy Metal",
        "url": "https://manager7.streamradio.fr:1435/stream",
        "desc": "The classics and the followers, at 320 kbps.",
        "icon": "⚒️",
        "genre": "Metal",
        "lang": "",
        "tags": ("energetic",),
    },
    "metalcore": {
        "name": "Metalcore",
        "url": "https://stream.laut.fm/metalcore",
        "desc": "Metalcore, deathcore and the breakdowns in between.",
        "icon": "💥",
        "genre": "Metal",
        "lang": "",
        "tags": ("energetic",),
    },
    "fipmetal": {
        "name": "FIP Metal",
        "url": "https://icecast.radiofrance.fr/fipmetal-midfi.mp3",
        "desc": "Metal, curated the way a radio station used to.",
        "icon": "⚔️",
        "genre": "Metal",
        "lang": "French",
        "tags": ("energetic",),
    },
    "indie": {
        "name": "Indie Pop Rocks!",
        "url": "https://ice1.somafm.com/indiepop-128-mp3",
        "desc": "Indie pop, old and new, with a soft spot for the obscure.",
        "icon": "🌼",
        "genre": "Indie",
        "lang": "",
        "tags": (),
    },
    "kexp": {
        "name": "KEXP 90.3 FM",
        "url": "https://kexp-mp3-128.streamguys1.com/kexp128.mp3",
        "desc": "Seattle's listener-powered station: whatever the DJ felt like.",
        "icon": "🌲",
        "genre": "Indie",
        "lang": "",
        "tags": (),
    },
    "wfmu": {
        "name": "WFMU Freeform",
        "url": "https://stream0.wfmu.org/freeform-128k",
        "desc": "Genuinely freeform radio: one show can contain anything.",
        "icon": "🗽",
        "genre": "Eclectic",
        "lang": "",
        "tags": (),
    },
    "radioparadise": {
        "name": "Radio Paradise",
        "url": "https://stream.radioparadise.com/mp3-320",
        "desc": "An eclectic mix, hand-picked and listener-supported, at 320 kbps.",
        "icon": "🏝️",
        "genre": "Eclectic",
        "lang": "",
        "tags": (),
    },
    "paradisemellow": {
        "name": "Radio Paradise Mellow Mix",
        "url": "https://stream.radioparadise.com/mellow-320",
        "desc": "The calm half of Radio Paradise, still at 320 kbps.",
        "icon": "🌅",
        "genre": "Eclectic",
        "lang": "",
        "tags": ("chill", "focus"),
    },
    "paradiseglobal": {
        "name": "Radio Paradise Global Mix",
        "url": "https://stream.radioparadise.com/global-320",
        "desc": "Music from outside the English-language charts.",
        "icon": "🌍",
        "genre": "World",
        "lang": "Multilingual",
        "tags": (),
    },
    "country": {
        "name": "Country",
        "url": "https://stream.laut.fm/country",
        "desc": "Modern and classic country.",
        "icon": "🤠",
        "genre": "Country",
        "lang": "",
        "tags": (),
    },
    "realcountry": {
        "name": "181.FM Real Country",
        "url": "https://listen.181fm.com/181-realcountry_128k.mp3",
        "desc": "Traditional country — fiddles, steel and no apologies.",
        "icon": "🌵",
        "genre": "Country",
        "lang": "",
        "tags": ("oldies",),
    },
    "folk": {
        "name": "Folk",
        "url": "https://stream.laut.fm/folk",
        "desc": "Folk, singer-songwriter and acoustic traditions.",
        "icon": "🪕",
        "genre": "Folk",
        "lang": "",
        "tags": (),
    },
    "folkfwd": {
        "name": "Folk Forward",
        "url": "https://ice1.somafm.com/folkfwd-128-mp3",
        "desc": "Indie folk, Americana, and the new acoustic songwriters.",
        "icon": "🍂",
        "genre": "Folk",
        "lang": "",
        "tags": ("chill",),
    },
    "celtic": {
        "name": "Thistle Radio",
        "url": "https://ice1.somafm.com/thistle-128-mp3",
        "desc": "An exploration of music from Celtic roots.",
        "icon": "🍀",
        "genre": "Celtic",
        "lang": "",
        "tags": ("instrumental",),
    },
    "covers": {
        "name": "Covers",
        "url": "https://ice1.somafm.com/covers-128-mp3",
        "desc": "Songs you know, done by people you don't. Just covers, all day.",
        "icon": "🎭",
        "genre": "Eclectic",
        "lang": "",
        "tags": (),
    },
    # ── Decades ──────────────────────────────────────────────────────────── #
    "eighties": {
        "name": "80s",
        "url": "https://stream.laut.fm/80s",
        "desc": "The whole decade: synth, new wave and the chart records.",
        "icon": "📼",
        "genre": "80s",
        "lang": "",
        "tags": ("oldies",),
    },
    "underground80s": {
        "name": "Underground Eighties",
        "url": "https://ice1.somafm.com/u80s-128-mp3",
        "desc": "UK synthpop and a bit of new wave, mostly the deep cuts.",
        "icon": "🕳️",
        "genre": "80s",
        "lang": "",
        "tags": ("oldies", "energetic"),
    },
    "nineties": {
        "name": "90s Radio",
        "url": "https://stream.laut.fm/90s",
        "desc": "Nineties pop, dance and the songs you still know every word to.",
        "icon": "💿",
        "genre": "90s",
        "lang": "",
        "tags": ("oldies",),
    },
    "seventies": {
        "name": "Left Coast 70s",
        "url": "https://ice1.somafm.com/seventies-128-mp3",
        "desc": "Mellow album rock from the Seventies. Yacht friendly.",
        "icon": "⛵",
        "genre": "Oldies",
        "lang": "",
        "tags": ("oldies", "chill"),
    },
    "oldies": {
        "name": "Oldies",
        "url": "https://stream.laut.fm/oldies",
        "desc": "Fifties to seventies, in no particular order.",
        "icon": "📻",
        "genre": "Oldies",
        "lang": "",
        "tags": ("oldies",),
    },
    "hitshistory": {
        "name": "I♥HITS HISTORY",
        "url": "https://streams.ilovemusic.de/iloveradio12.mp3",
        "desc": "The records that were number one at some point.",
        "icon": "🕰️",
        "genre": "Oldies",
        "lang": "",
        "tags": ("oldies",),
    },
    # ── Hip hop and rap ──────────────────────────────────────────────────── #
    "hiphop": {
        "name": "Hip Hop",
        "url": "https://stream.laut.fm/hiphop",
        "desc": "Hip hop across the decades, old school to now.",
        "icon": "🎙️",
        "genre": "Hip-Hop",
        "lang": "",
        "tags": (),
    },
    "rap": {
        "name": "Rap",
        "url": "https://stream.laut.fm/rap",
        "desc": "Rap and trap, mostly new.",
        "icon": "🔥",
        "genre": "Rap",
        "lang": "",
        "tags": ("energetic",),
    },
    "trap": {
        "name": "Trap",
        "url": "https://stream.laut.fm/trap",
        "desc": "808s, hi-hat rolls and nothing else.",
        "icon": "💎",
        "genre": "Trap",
        "lang": "",
        "tags": ("energetic",),
    },
    "usrap": {
        "name": "I♥US ONLY RAP",
        "url": "https://streams.ilovemusic.de/iloveradio13.mp3",
        "desc": "American rap only, no exceptions.",
        "icon": "🇺🇸",
        "genre": "Rap",
        "lang": "",
        "tags": ("energetic",),
    },
    "deutschrap": {
        "name": "Deutschrap Beste",
        "url": "https://streams.ilovemusic.de/iloveradio6.mp3",
        "desc": "German rap, and a lot of it.",
        "icon": "🇩🇪",
        "genre": "Rap",
        "lang": "German",
        "tags": ("energetic",),
    },
    "fiphiphop": {
        "name": "FIP Hip-Hop",
        "url": "https://icecast.radiofrance.fr/fiphiphop-midfi.mp3",
        "desc": "Hip hop, curated with the French gift for finding the good stuff.",
        "icon": "🎧",
        "genre": "Hip-Hop",
        "lang": "French",
        "tags": (),
    },
    # ── East and South Asia ──────────────────────────────────────────────── #
    "jpop": {
        "name": "J-Pop (listen.moe)",
        "url": "https://listen.moe/stream",
        "desc": "Japanese pop, rock and anime music, at 192 kbps Opus.",
        "icon": "🌸",
        "genre": "J-Pop",
        "lang": "Japanese",
        "tags": ("energetic",),
    },
    "kpop": {
        "name": "K-Pop (listen.moe)",
        "url": "https://listen.moe/kpop/stream",
        "desc": "Korean pop and idol groups, at 192 kbps Opus.",
        "icon": "🇰🇷",
        "genre": "K-Pop",
        "lang": "Korean",
        "tags": ("energetic",),
    },
    "kpopradio": {
        "name": "0N K-Pop",
        "url": "https://0n-kpop.radionetz.de/0n-kpop.mp3",
        "desc": "K-Pop around the clock, hosted in German.",
        "icon": "🎤",
        "genre": "K-Pop",
        "lang": "Korean",
        "tags": ("energetic",),
    },
    "kpophits": {
        "name": "Kpophits",
        "url": "https://kpophits.stream.laut.fm/kpophits",
        "desc": "Nothing but the hits, which is what it says on the tin.",
        "icon": "💜",
        "genre": "K-Pop",
        "lang": "Korean",
        "tags": ("energetic",),
    },
    "cinz": {
        "name": "CINZ NET Radio",
        "url": "https://stream.cinz.net/",
        "desc": "K-pop, J-pop and J-metal from an independent net station.",
        "icon": "🎵",
        "genre": "J-Pop",
        "lang": "Multilingual",
        "tags": ("energetic",),
    },
    "anime": {
        "name": "Anime",
        "url": "https://stream.laut.fm/anime",
        "desc": "Anime openings, endings and OSTs.",
        "icon": "🍥",
        "genre": "Anime",
        "lang": "Japanese",
        "tags": ("energetic",),
    },
    "nightcore": {
        "name": "Nightcore",
        "url": "https://stream.laut.fm/nightcore",
        "desc": "Everything sped up and pitched, on purpose.",
        "icon": "⚡",
        "genre": "Nightcore",
        "lang": "",
        "tags": ("energetic",),
    },
    "bollywood": {
        "name": "Mirchi Top 20",
        "url": "https://drive.uber.radio/uber/bollywoodnow/icecast.audio",
        "desc": "Today's Hindi chart, the biggest songs in the country.",
        "icon": "🇮🇳",
        "genre": "Bollywood",
        "lang": "Hindi",
        "tags": (),
    },
    "bollywood90s": {
        "name": "Retro Bollywood 90s",
        "url": "https://stream-143.zeno.fm/fdgs82xkzhhvv",
        "desc": "Nineties Hindi film music, which is its own genre.",
        "icon": "🎬",
        "genre": "Bollywood",
        "lang": "Hindi",
        "tags": ("oldies",),
    },
    # ── Everywhere else ──────────────────────────────────────────────────── #
    "fipworld": {
        "name": "FIP Monde",
        "url": "https://icecast.radiofrance.fr/fipworld-midfi.mp3",
        "desc": "Music from everywhere that is not the Anglosphere.",
        "icon": "🌍",
        "genre": "World",
        "lang": "Multilingual",
        "tags": (),
    },
    "fip": {
        "name": "FIP",
        "url": "https://icecast.radiofrance.fr/fip-midfi.mp3",
        "desc": "Jazz, soul, world and rock in one eclectic programme.",
        "icon": "🇫🇷",
        "genre": "Eclectic",
        "lang": "French",
        "tags": (),
    },
    "fipnouveautes": {
        "name": "FIP Nouveautés",
        "url": "https://icecast.radiofrance.fr/fipnouveautes-midfi.mp3",
        "desc": "New releases only, which is the cheapest way to hear something first.",
        "icon": "🆕",
        "genre": "Eclectic",
        "lang": "French",
        "tags": (),
    },
    "arabic": {
        "name": "Monte Carlo Doualiya",
        "url": "https://montecarlodoualiya128k.ice.infomaniak.ch/mc-doualiya.mp3",
        "desc": "Arabic music and programming, from Paris to the Middle East.",
        "icon": "🕌",
        "genre": "Arabic",
        "lang": "Arabic",
        "tags": (),
    },
    "turkish": {
        "name": "Beste FM",
        "url": "https://stream.zeno.fm/tnvnovtgozstv",
        "desc": "Turkish pop and arabesque.",
        "icon": "🇹🇷",
        "genre": "Turkish",
        "lang": "Turkish",
        "tags": (),
    },
    "salsa": {
        "name": "Salsa",
        "url": "https://stream.laut.fm/salsa",
        "desc": "Salsa, merengue and the Latin dance floor.",
        "icon": "💃",
        "genre": "Salsa",
        "lang": "Spanish",
        "tags": ("energetic",),
    },
    "reggae": {
        "name": "SomaFM Reggae",
        "url": "https://ice1.somafm.com/reggae-128-mp3",
        "desc": "Roots, dub and dancehall.",
        "icon": "🇯🇲",
        "genre": "Reggae",
        "lang": "",
        "tags": ("chill",),
    },
    "reggaefm": {
        "name": "Reggae Chill Cafe",
        "url": "https://maggie.torontocast.com:2020/stream/reggaechillcafe",
        "desc": "The gentler side of reggae, for a long afternoon.",
        "icon": "🌴",
        "genre": "Reggae",
        "lang": "",
        "tags": ("chill",),
    },
    "fipreggae": {
        "name": "FIP Reggae",
        "url": "https://icecast.radiofrance.fr/fipreggae-midfi.mp3",
        "desc": "Reggae from its beginnings to now, with the history attached.",
        "icon": "🎶",
        "genre": "Reggae",
        "lang": "French",
        "tags": (),
    },
    "gospel": {
        "name": "Gospel FM Jamaica",
        "url": "https://stream-37.zeno.fm/zpksre88rm0uv",
        "desc": "Gospel and worship music from Jamaica.",
        "icon": "🙏",
        "genre": "Gospel",
        "lang": "",
        "tags": (),
    },
    "srf3": {
        "name": "SRF 3",
        "url": "https://stream.srg-ssr.ch/m/drs3/mp3_128",
        "desc": "New music and pop, presented in Swiss German.",
        "icon": "🇨🇭",
        "genre": "Pop",
        "lang": "German",
        "tags": (),
    },
    "partymode": {
        "name": "I♥PARTY HARD",
        "url": "https://streams.ilovemusic.de/iloveradio14.mp3",
        "desc": "Party records, mixed into each other, no gaps.",
        "icon": "🎉",
        "genre": "Dance",
        "lang": "",
        "tags": ("energetic",),
    },
    "dance": {
        "name": "Dance",
        "url": "https://stream.laut.fm/dance",
        "desc": "Club and dance, from the nineties onwards.",
        "icon": "💃",
        "genre": "Dance",
        "lang": "",
        "tags": ("energetic",),
    },
    "christmas": {
        "name": "Christmas",
        "url": "https://stream.laut.fm/christmas",
        "desc": "Christmas music, which is delightful in December and a war crime in June.",
        "icon": "🎄",
        "genre": "Seasonal",
        "lang": "",
        "tags": ("seasonal",),
    },
    "nasachatter": {
        "name": "Mission Control",
        "url": "https://ice1.somafm.com/missioncontrol-128-mp3",
        "desc": "Ambient music woven around NASA and space-explorer transmissions.",
        "icon": "👩‍🚀",
        "genre": "Ambient",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    "poptron": {
        "name": "PopTron",
        "url": "https://ice1.somafm.com/poptron-128-mp3",
        "desc": "Electro-pop and indie dance rock.",
        "icon": "🎹",
        "genre": "Pop",
        "lang": "",
        "tags": ("energetic",),
    },
    "bootliquor": {
        "name": "Boot Liquor",
        "url": "https://ice1.somafm.com/bootliquor-128-mp3",
        "desc": "Americana roots music for cowhands, cowpokes and cowtippers.",
        "icon": "🤠",
        "genre": "Country",
        "lang": "",
        "tags": ("oldies",),
    },
    "brfm": {
        "name": "Black Rock FM",
        "url": "https://ice1.somafm.com/brfm-128-mp3",
        "desc": "Rock, soul and hip hop from the Black Rock Coalition.",
        "icon": "🖤",
        "genre": "Eclectic",
        "lang": "",
        "tags": (),
    },
    "lush": {
        "name": "Lush",
        "url": "https://ice1.somafm.com/lush-128-mp3",
        "desc": "Mostly female vocals with an electronic influence.",
        "icon": "🌺",
        "genre": "Chill",
        "lang": "",
        "tags": ("chill",),
    },
    "thetrip": {
        "name": "The Trip",
        "url": "https://ice1.somafm.com/thetrip-128-mp3",
        "desc": "Progressive house and downtempo, from SomaFM's DJ sets.",
        "icon": "🛣️",
        "genre": "House",
        "lang": "",
        "tags": ("instrumental", "focus"),
    },
    # ── One flagship per language ─────────────────────────────────────────── #
    #: The catalogue grew by genre first, which left the thing radio is *for*
    #: half-done: somebody whose language was not French, German, Japanese,
    #: Korean, Hindi, Turkish or Arabic could search for their own words and be
    #: told no. Each entry here is one well-known station that **broadcasts in
    #: that language**, and the tag is the point of the entry rather than a
    #: detail on it — `/music stations Spanish` is a search this table has to be
    #: able to answer.
    #:
    #: Every URL was fetched before it was written down (see the rules above),
    #: and the ones that did not answer are named here rather than left to be
    #: rediscovered: **Mandarin, Indonesian, Vietnamese, Farsi, Finnish and
    #: Ukrainian** all have broadcasters, and none of the addresses this host
    #: could reach answered with audio — a 404, a DNS failure, a 503 and a
    #: timeout respectively. A language with no station is a gap in this table,
    #: not a language this bot has decided against.
    "los40": {
        "name": "LOS40",
        "url": "https://25453.live.streamtheworld.com/LOS40AAC.aac",
        "desc": "Spain's hit-music station, and the one that decides what a Spanish hit is.",
        "icon": "🇪🇸",
        "genre": "Top 40",
        "lang": "Spanish",
        "tags": ("energetic",),
    },
    "rairadio3": {
        "name": "Rai Radio 3",
        "url": "https://icestreaming.rai.it/3.mp3",
        "desc": "Italian public radio: classical concerts, jazz and long-form culture.",
        "icon": "🇮🇹",
        "genre": "Eclectic",
        "lang": "Italian",
        "tags": (),
    },
    "antena1": {
        "name": "Antena 1",
        "url": "https://radiocast.rtp.pt/antena180a.mp3",
        "desc": "Portugal's public station — news, culture and Portuguese music.",
        "icon": "🇵🇹",
        "genre": "Eclectic",
        "lang": "Portuguese",
        "tags": (),
    },
    "antena2": {
        "name": "Antena 2",
        "url": "https://radiocast.rtp.pt/antena280a.mp3",
        "desc": "The classical and jazz half of Portuguese public radio.",
        "icon": "🎻",
        "genre": "Classical",
        "lang": "Portuguese",
        "tags": (),
    },
    "nashe": {
        "name": "Nashe Radio",
        "url": "https://nashe1.hostingradio.ru/nashe-128.mp3",
        "desc": "Russian rock, the station that made it a genre rather than a scene.",
        "icon": "🇷🇺",
        "genre": "Rock",
        "lang": "Russian",
        "tags": ("energetic",),
    },
    "rmffm": {
        "name": "RMF FM",
        "url": "https://rs6-krk2.rmfstream.pl/RMFFM48",
        "desc": "Poland's biggest commercial station — pop and news, all day.",
        "icon": "🇵🇱",
        "genre": "Pop",
        "lang": "Polish",
        "tags": ("energetic",),
    },
    "nporadio1": {
        "name": "NPO Radio 1",
        "url": "https://icecast.omroep.nl/radio1-bb-mp3",
        "desc": "Dutch public radio's news and talk network.",
        "icon": "🇳🇱",
        "genre": "Eclectic",
        "lang": "Dutch",
        "tags": (),
    },
    "nporadio2": {
        "name": "NPO Radio 2",
        "url": "https://icecast.omroep.nl/radio2-bb-mp3",
        "desc": "Dutch pop, from the Dutch ones and everybody else.",
        "icon": "🌷",
        "genre": "Pop",
        "lang": "Dutch",
        "tags": ("energetic",),
    },
    "nrkp1": {
        "name": "NRK P1",
        "url": "https://lyd.nrk.no/nrk_radio_p1_ostlandssendingen_mp3_h",
        "desc": "Norwegian public radio's main channel, Oslo edition.",
        "icon": "🇳🇴",
        "genre": "Eclectic",
        "lang": "Norwegian",
        "tags": (),
    },
    "drp1": {
        "name": "DR P1",
        "url": "https://live-icy.dr.dk/A/A03H.mp3",
        "desc": "Danish public radio: news and talk for a whole small country.",
        "icon": "🇩🇰",
        "genre": "Eclectic",
        "lang": "Danish",
        "tags": (),
    },
    "srp1": {
        "name": "Sveriges Radio P1",
        "url": "https://live1.sr.se/p1-mp3-192",
        "desc": "Sweden's speech channel — news, debate and the long interview.",
        "icon": "🇸🇪",
        "genre": "Eclectic",
        "lang": "Swedish",
        "tags": (),
    },
    "rozhlas": {
        "name": "ČRo Radiožurnál",
        "url": "https://rozhlas.stream/radiozurnal_mp3_128.mp3",
        "desc": "The Czech Republic's main news station, from Czech Radio.",
        "icon": "🇨🇿",
        "genre": "Eclectic",
        "lang": "Czech",
        "tags": (),
    },
    "klubradio": {
        "name": "Klubrádió",
        "url": "https://icast.connectmedia.hu/5201/live.mp3",
        "desc": "Hungarian talk radio, and famously not shy about it.",
        "icon": "🇭🇺",
        "genre": "Eclectic",
        "lang": "Hungarian",
        "tags": (),
    },
    "rractualitati": {
        "name": "Radio România Actualități",
        "url": "https://stream4.srr.ro:8443/romania-actualitati",
        "desc": "Romania's public news channel, in Romanian all day.",
        "icon": "🇷🇴",
        "genre": "Eclectic",
        "lang": "Romanian",
        "tags": (),
    },
    "ertkosmos": {
        "name": "ERT Kosmos",
        "url": "https://radiostreaming.ert.gr/ert-kosmos",
        "desc": "Greek public radio's world-music channel, from Athens.",
        "icon": "🇬🇷",
        "genre": "World",
        "lang": "Greek",
        "tags": (),
    },
    "j1hits": {
        "name": "J1 HITS",
        "url": "https://jenny.torontocast.com:2000/stream/J1HITS",
        "desc": "Japanese chart pop, the current stuff rather than the classics.",
        "icon": "🎌",
        "genre": "J-Pop",
        "lang": "Japanese",
        "tags": ("energetic",),
    },
    "galatz": {
        "name": "Galei Tzahal",
        "url": "https://glzwizzlv.bynetcdn.com/glglz_mp3",
        "desc": "Israel's army radio — pop, news and the playlist the country argues about.",
        "icon": "🇮🇱",
        "genre": "Pop",
        "lang": "Hebrew",
        "tags": ("energetic",),
    },
    "coolfahrenheit": {
        "name": "Cool Fahrenheit",
        "url": "https://coolism-web.cdn.byteark.com/;stream.mp3",
        "desc": "Thai pop and indie, from Bangkok.",
        "icon": "🇹🇭",
        "genre": "Pop",
        "lang": "Thai",
        "tags": ("energetic",),
    },
}

def _mmss(seconds: float | int) -> str:
    return player_module.format_duration(int(seconds or 0))


# --------------------------------------------------------------------------- #
# Finding a station
# --------------------------------------------------------------------------- #
# The catalogue is a hundred-odd entries, so the command cannot be a menu: Discord
# caps a command's fixed choices at 25 and renders them in the order they were
# declared, so a picker is a list nobody can find *Turkish* in. Autocomplete over
# the words people actually have — a name, a genre, a language, a mood — is what
# makes a list this size usable, and it is the reason the catalogue could grow.

#: How many suggestions autocomplete may return. Discord's own ceiling is 25.
STATION_SUGGESTIONS = 25

#: Words people search with that the catalogue does not use, because a person who
#: wants background music for an essay types *study* and not *focus*. Deliberately
#: tiny and literal: a synonym table that grows into a thesaurus is a search whose
#: results nobody can predict, which is worse than a search that returns nothing and
#: says so. Keys are whole words, matched case- and space-insensitively.
SEARCH_SYNONYMS: dict[str, tuple[str, ...]] = {
    "study": ("focus",),
    "studying": ("focus",),
    "work": ("focus",),
    "working": ("focus",),
    "homework": ("focus",),
    "concentration": ("focus",),
    "background": ("focus", "instrumental"),
    "silence": ("instrumental",),
    "relax": ("chill",),
    "relaxing": ("chill",),
    "calm": ("chill",),
    "sleep": ("chill", "ambient"),
    "loud": ("energetic",),
    "party": ("energetic",),
    "workout": ("energetic",),
    "gym": ("energetic",),
    "retro": ("oldies",),
    "nostalgia": ("oldies",),
}


def _flat(text: str) -> str:
    """A searchable form of a name: lower case, and the punctuation nobody types.

    ``Lo-Fi Beats`` has to be findable by typing *lofi beats* — which is what the
    label looks like once the icon is in front of it, and what a person types from
    memory. A hyphen is not something anybody types, so both sides of every
    comparison go through here and ``lofi``, ``lo-fi`` and ``Lo Fi`` are one word.

    The trade is real and worth naming: stripping punctuation loses word boundaries,
    so a search can match inside a longer word. In a catalogue of a hundred entries
    where a name match is ranked above everything else, a little noise in the tail is
    a better deal than a station nobody can find.
    """
    return "".join(character for character in text.lower() if character.isalnum())


def _station_rank(text: str, key: str, station: dict) -> int | None:
    """How well one search word fits one station, or ``None`` for no fit at all.

    Ranked rather than merely filtered, because a description is long enough to
    swallow a search: somebody typing *jazz* wants the jazz stations, not the forty
    entries whose blurb mentions jazz once. So a name match beats a genre or a key
    match, which beats a language or a mood tag, which beats a mention anywhere in
    the description.
    """
    needle = _flat(text)
    if not needle:
        return None
    if needle in _flat(station["name"]):
        return 0
    if needle in _flat(station["genre"]) or needle in _flat(key):
        return 1
    if needle in _flat(station.get("lang") or "") or needle in _flat(" ".join(station.get("tags") or ())):
        return 2
    if needle in _flat(station["desc"]):
        return 3
    return None


def search_stations(query: str, *, limit: int = STATION_SUGGESTIONS) -> list[tuple[str, dict]]:
    """Stations matching a search, best first, as ``(key, station)`` pairs.

    A synonym hit counts one rank worse than the same hit on the words actually
    typed, so *study* finds the ``focus`` stations without ever outranking something
    that literally says study. Ties keep catalogue order, which is grouped by genre,
    so neighbours in the list stay neighbours in the results.
    """
    text = (query or "").strip().lower()
    if not text:
        return list(RADIO_STATIONS.items())[:limit]
    # The typed words first, then what they are called here. Order matters: it is
    # what decides which hits are discounted. The whole phrase is tried before its
    # words, so "lofi beats" is a name match rather than two word matches in
    # different fields.
    words = text.replace("-", " ").split()
    terms = [text]
    if len(words) > 1:
        terms.extend(words)
    for word in words:
        terms.extend(SEARCH_SYNONYMS.get(word, ()))
    ranked: list[tuple[int, int, str, dict]] = []
    for index, (key, station) in enumerate(RADIO_STATIONS.items()):
        best: int | None = None
        for position, term in enumerate(terms):
            found = _station_rank(term, key, station)
            if found is None:
                continue
            found = found if position == 0 else found + 1
            best = found if best is None else min(best, found)
        if best is not None:
            ranked.append((best, index, key, station))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [(key, station) for _rank, _index, key, station in ranked[:limit]]


def station_option_label(key: str, station: dict) -> str:
    """What one suggestion looks like, inside Discord's 100-character label cap."""
    label = f"{station['icon']} {station['name']} · {station['genre']}"
    if station.get("lang"):
        label += f" · {station['lang']}"
    return label[:100]


def station_for(value: str) -> tuple[str, dict] | None:
    """The station a key, a name or a search names, or ``None``.

    Deliberately forgiving: the key is what the picker sends back, but the *name* is
    what a member reads on the card and types the next time.
    """
    text = (value or "").strip()
    if not text:
        return None
    wanted = _flat(text)
    for key, station in RADIO_STATIONS.items():
        if wanted in (_flat(key), _flat(station["name"])):
            return key, station
    found = search_stations(text, limit=1)
    return found[0] if found else None


def _station_card(
    station: dict, channel: str, requester: str, *, from_memory: bool = False
) -> str:
    """What the station card says, in the order a reader wants it.

    The descriptor line is the point of a catalogue this size: a station is a
    *genre, a language and a mood* as well as a name, and somebody who does not
    speak the language of what is playing has no other way to find that out before
    they pick it.
    """
    kind = station["genre"]
    if station.get("lang"):
        kind += f" · {station['lang']}"
    mood = ", ".join(station.get("tags") or ())
    if mood:
        kind += f" · {mood}"
    lines = [
        f"{station['icon']} **{station['name']}** is live in {channel}.",
        f"-# {kind}",
        f"-# {station['desc']} · started by {requester}",
    ]
    if from_memory:
        lines.append("-# This server's last station — name one to change it")
    return "\n".join(lines)


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
            "The queue carried on. A station has no end, so a stream that stops is "
            "the connection dropping rather than the song finishing — if it keeps "
            "happening, that station is the problem and not this server.",
        ),
        "unplayable": (
            "⚠️ That station could not be played",
            style.WARN,
            "The queue carried on. Nothing was cut off — the address would not open "
            "at all, which is usually the station being down or refusing this host.",
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
    }

    async def _on_fault(self, fault: player_module.Fault) -> None:
        """A stream that died is news for the room *and* for the operator."""
        player = self.players.get(int(getattr(fault, "guild_id", 0) or 0)) or None
        channel = getattr(player, "text_channel", None) if player else None
        emoji_headline, colour, advice = self.FAULT_CARDS.get(
            fault.kind,
            (f"⚠️ {fault.headline.capitalize()}", style.WARN, "The queue carried on."),
        )
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
        await operator_log.music_alert(self.bot, fault, player=player)

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

    # ------------------------------------------------------------- /music 📻 #
    music = app_commands.Group(name="music", description="Radio: cafés, ambient, jazz, and a station in your language.")

    @music.command(name="radio", description="Stream a 24/7 radio station — no search, no provider, just music.")
    @app_commands.describe(
        station="Which station. Start typing to search every genre, language and mood (blank = the last one here)"
    )
    async def radio(self, interaction: discord.Interaction, station: str | None = None):
        blocked = self._unplayable()
        if blocked:
            return await style.send_card(interaction, content=blocked, ephemeral=True)

        chosen = station_for(station or "")
        if chosen is None and (station or "").strip():
            return await style.send_card(
                interaction,
                content=(
                    f"❌ I don't have a station called **{station}**.\n"
                    "-# `/music stations` shows every genre and language I do have."
                ),
                ephemeral=True,
            )
        remembered = await db.get_radio_station(interaction.guild_id)
        chose_it = chosen is not None
        if chosen is None:
            # No argument, so this is "put the last one back on" — which is what a
            # server does when the queue dies and everybody has come back. The first
            # time there is nothing to remember, so it picks one and says that it did,
            # rather than pretending the choice was the member's.
            chosen = station_for(remembered)
        if chosen is None:
            chosen = random.choice(list(RADIO_STATIONS.items()))
        key, chosen = chosen

        await interaction.response.defer()
        vc = await self._ensure_voice(interaction)
        if vc is None:
            return
        player = self.get_player(interaction.guild_id)
        player.voice_client = vc
        player.text_channel = interaction.channel
        is_247, _ = await db.get_247_setting(interaction.guild_id)
        player.is_247 = is_247

        player.queue.clear()
        # The address is the station, so the track is playable the moment it is
        # queued: there is no search to run, no provider to ask and nothing that
        # can refuse this host. That is the whole of why radio is what plays.
        track = media_source.Track(
            title=f"{chosen['icon']} {chosen['name']}",
            provider="radio",
            duration=0,
            requester=interaction.user.display_name,
            stream_url=chosen["url"],
            resolved_at=time.monotonic(),
        )
        player.queue.append(track)

        # What this server last chose, so `/music radio` with no argument is the one
        # it had and not a coin toss. Written on a best-effort basis: a database that
        # will not take it must not stop the music, so the failure is logged and the
        # station plays anyway.
        try:
            await db.set_radio_station(interaction.guild_id, key)
        except Exception as exc:  # noqa: BLE001 — remembering is not worth a failure
            print(f"[MUSIC] could not remember the station for {interaction.guild_id}: {exc}", flush=True)

        # Let the stop's own after-callback do the advancing. Doing both produced
        # two advances, the second of which found an empty queue and cleared
        # ``current`` — after which /nowplaying and /skip denied the radio was on.
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        else:
            await player.play_next()

        await style.send_card(
            interaction,
            content=_station_card(
                chosen,
                vc.channel.mention,
                interaction.user.display_name,
                from_memory=bool(remembered) and not chose_it,
            ),
            view=ControlsView(self, player),
            timeout=None,
        )

    @radio.autocomplete("station")
    async def _radio_station_autocomplete(self, interaction: discord.Interaction, current: str):
        """Every station, searchable by name, genre, language or mood.

        Autocomplete rather than ``app_commands.choices`` for two reasons, and the
        second is the one that matters: Discord caps a command's fixed choices at
        25, and it renders them in the order they were declared — so a menu is a
        catalogue nobody can find *Turkish* in. A search box over the words people
        actually use is what makes a list this size usable.
        """
        return [
            app_commands.Choice(name=station_option_label(key, value), value=key)
            for key, value in search_stations(current)
        ]

    @music.command(
        name="stations",
        description="Browse the radio catalogue — every genre, language and mood.",
    )
    @app_commands.describe(query="Optional word to filter by — a genre, a language, a mood, or part of a name")
    async def stations(self, interaction: discord.Interaction, query: str | None = None):
        """The catalogue as something a member can read, because a search box helps
        only once you know a word to type into it.

        With no argument it answers the question the search box cannot — *what is
        there?* — as the genres and how many stations each holds. With one it lists
        the matches, so that ``jazz`` or ``instrumental`` or ``Japanese`` is a
        twenty-second answer rather than a guess at a station name.
        """
        text = (query or "").strip()
        if not text:
            by_genre: dict[str, int] = {}
            for station in RADIO_STATIONS.values():
                by_genre[station["genre"]] = by_genre.get(station["genre"], 0) + 1
            listed = sorted(by_genre.items(), key=lambda row: (-row[1], row[0]))
            embed = discord.Embed(
                title="📻 The radio catalogue",
                description=(
                    f"**{len(RADIO_STATIONS)} stations across {len(listed)} genres**, all direct "
                    "streams — none of them goes through the audio extractor, so they keep "
                    "working on days when the platforms will not serve this host.\n\n"
                    + "\n".join(f"**{genre}** · {count}" for genre, count in listed)
                    + "\n\n-# Run this again with a word — a genre, a language, a mood — to "
                    "list the stations in it, or start typing in `/music radio`."
                ),
                color=style.SUCCESS,
            )
            return await style.send_card(interaction, embed=embed, ephemeral=True)

        found = search_stations(text, limit=STATION_SUGGESTIONS)
        if not found:
            return await style.send_card(
                interaction,
                content=(
                    f"🔎 Nothing in the catalogue matches **{text}** — which is not the same "
                    "as the station not existing. Run `/music stations` with no word to see "
                    "what there is."
                ),
                ephemeral=True,
            )
        lines = []
        for key, station in found:
            meta = station["genre"] + (f" · {station['lang']}" if station.get("lang") else "")
            # The mood is shown for the searches that can only be answered by one:
            # somebody who typed "study" and got a station tagged "focus" deserves to
            # see why it is on the list, rather than wondering what matched.
            if station.get("tags"):
                meta += f" · {', '.join(station['tags'])}"
            lines.append(f"{station['icon']} **{station['name']}** — {meta}")
        shown = len(found)
        embed = discord.Embed(
            title=f"📻 {shown} station{'s' if shown != 1 else ''} for “{text}”",
            description="\n".join(lines)
            + "\n\n-# Play one with `/music radio`, and it will be listed first in "
            "the picker next time.",
            color=style.SUCCESS,
        )
        await style.send_card(interaction, embed=embed, ephemeral=True)

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
            footer=f"{len(player.queue)} queued · volume {int(player.volume * 100)}%",
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

    @music.command(name="doctor", description="What this host can and cannot do for voice (administrator).")
    @app_commands.checks.has_permissions(administrator=True)
    async def doctor(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        caps = voice_caps.probe(refresh=True)
        ok, why = voice_caps.build_source(caps)
        # The one question a capability probe cannot answer and a version string
        # never could: whether this host can *open* a stream. ffmpeg installed,
        # libopus loaded and every URL in the tree healthy is all true on a box
        # with no route to the outside world. So the command opens a real station,
        # the same question the weekly pass asks.
        probe = await asyncio.to_thread(media_source.self_check)
        lines = [
            f"**Playback** · {'yes' if caps.playable else 'no'}",
            f"**ffmpeg** · `{caps.ffmpeg or 'missing'}` ({caps.ffmpeg_source})",
            f"**libopus** · `{caps.opus_source}` — {'live volume' if caps.live_volume else 'volume is baked into the stream'}",
            f"**DAVE** · `{caps.dave_version or 'missing'}`",
            f"**Streams** · {probe.sentence()}",
            f"**Stations** · {len(RADIO_STATIONS)} in the catalogue over "
            f"{len({station['genre'] for station in RADIO_STATIONS.values()})} genres",
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
                "than the station." if probe.unreachable else
                "\n-# This one station refused the request; another may still play."
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


# --------------------------------------------------------------------------- #
# The view
# --------------------------------------------------------------------------- #
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
            footer=f"{len(player.queue)} queued",
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
